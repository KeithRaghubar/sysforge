"""
pkgbuild_patcher.py — PKGBUILD mutation and flag extraction

Responsible for all PKGBUILD modification: group injection, flag extraction
from function bodies, conditional block removal, and writing patched copies.
The parser (pkgbuild_meta.py) is read-only; this module owns all writes.

Public API:
    patch_pkgbuild_groups(pkgbuild_path, groups) -> Path
    extract_pkgbuild_profile(pkgmeta, pkgbuild_path) -> dict
    write_extracted_profile(profile, pkgbuild_path) -> Path
    load_extracted_profile(pkgbuild_path) -> dict
    apply_patch_pkgbuild(pkgbuild_path, pkgmeta) -> Path
    cleanup_patch_artifacts(pkgbuild_path)
"""
import re
import sysforge.log as _log
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Target keys for flag extraction
#
# Union of all _CONF_KEY_MAP key sets from profile.py. Duplicated here to
# avoid a circular import — profile.py should not depend on the patcher.
# Keep in sync with profile._CONF_KEY_MAP.
# ---------------------------------------------------------------------------

_EXTRACTABLE_KEYS = {
    # makepkg.conf
    "CC", "CXX", "AR", "NM", "RANLIB", "STRIP",
    "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
    "DEBUG_CFLAGS", "DEBUG_CXXFLAGS", "DEBUG_LDFLAGS",
    "MAKEFLAGS",
    "BUILDENV", "OPTIONS", "INTEGRITY_CHECK",
    "PKGEXT", "SRCEXT",
    # rust (conf) + env pass keys
    "RUSTFLAGS",
    "CARGO_PROFILE_RELEASE_LTO",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
    "CARGO_PROFILE_RELEASE_OPT_LEVEL",
    "CARGO_INCREMENTAL",
    "RUSTC_WRAPPER",              # env pass
    "CCACHE_DIR",                 # env pass
    "SCCACHE_DIR",                # env pass
    "CARGO_HOME",                 # env pass
    "CARGO_NET_GIT_FETCH_WITH_CLI",  # env pass
    "PKG_CONFIG_PATH",            # env pass
    # cmake
    "CMAKE_BUILD_TYPE",
    "CMAKE_C_FLAGS",
    "CMAKE_CXX_FLAGS",
    "CMAKE_EXE_LINKER_FLAGS",
    "CMAKE_SHARED_LINKER_FLAGS",
    # meson
    "MESON_ARGS",
    "CC_LD",            # meson: linker override for CC
    "CXX_LD",           # meson: linker override for CXX
}

# Bare/export assignments, including += variants.
# Groups: export (optional), key, op (= or +=), value (quoted or bare token)
_ASSIGNMENT_RE = re.compile(
    r"""^[ \t]*(?P<export>export\s+)?(?P<key>\w+)(?P<op>\+?=)(?P<value>"[^"]*"|'[^']*'|\S+)""",
    re.MULTILINE,
)

# Any remaining $VAR or ${...} reference after self-ref stripping.
# Used to detect complex bash expressions that can't be safely extracted.
_VARREF_RE = re.compile(r"\$")

# if ... fi conditional block (bash), DOTALL so . matches newlines.
# Correctly handles nested if blocks via depth tracking in
# _extract_conditional_blocks — this regex is used only for initial detection.
_IF_RE = re.compile(r"^[ \t]*if\b", re.MULTILINE)
_FI_RE = re.compile(r"^[ \t]*fi\b", re.MULTILINE)

# -Wl,... packed linker token
_WL_RE = re.compile(r"^-Wl,(.+)$")

# Inline make/cmake invocations that carry flag-like arguments — only removed
# when the key is in _EXTRACTABLE_KEYS (i.e. a flag we manage), so that
# make invocations like `make LOCALVERSION=... all` are not accidentally stripped.
_INLINE_MAKE_RE = re.compile(r"^\s*make\s+(?P<key>\w+)=", re.MULTILINE)
_INLINE_CMAKE_RE = re.compile(r"^\s*cmake\b.*-D(?P<key>[A-Z_]+)=", re.MULTILINE)

# Interactive kconfig targets — require terminal input or a TUI.
# Replaced with `make olddefconfig` in noninteractive mode (kernel stage).
# Groups: (1) leading whitespace + make + optional VAR=val args + space
#         (2) the interactive target name
#         (3) optional trailing whitespace / comment
_INTERACTIVE_KCONFIG_RE = re.compile(
    r"^(\s*make(?:\s+\w+=\S*)*\s+)(oldconfig|nconfig|menuconfig|xconfig|gconfig)(\s*(?:#.*)?)$",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Group patching (moved from pkgbuild_meta.py)
# ---------------------------------------------------------------------------

def patch_pkgbuild_groups(pkgbuild_path, groups):
    """
    Write a patched copy of the PKGBUILD with the resolved groups list injected.
    If a groups=(...) array exists it is replaced; if absent it is inserted
    after the pkgname line.
    Returns the path to the patched copy (PKGBUILD.sysforge).
    """
    patched_path = pkgbuild_path.parent / "PKGBUILD.sysforge"
    groups_line = "groups=(" + " ".join(f'"{g}"' for g in groups) + ")"

    text = pkgbuild_path.read_text()

    new_text, count = re.subn(
        r"^groups=\([^)]*\)",
        groups_line,
        text,
        flags=re.MULTILINE,
    )

    if count == 0:
        new_text = re.sub(
            r"^(pkgname=.*)$",
            rf"\1\n{groups_line}",
            text,
            count=1,
            flags=re.MULTILINE,
        )

    patched_path.write_text(new_text)
    _log.info("[BUILD]", f"Wrote patched PKGBUILD: {patched_path}")
    return patched_path


# ---------------------------------------------------------------------------
# Flag extraction
# ---------------------------------------------------------------------------

def _strip_var_refs(value):
    """
    Strip all shell variable references from a flag value string.

    Removes:
      ${...}   — any brace-expansion form (including substitutions)
      $WORD    — bare uppercase variable references

    Returns the stripped string with extra whitespace collapsed.
    Callers should check for remaining '$' using _VARREF_RE to detect
    complex expressions that could not be fully resolved.
    """
    # Strip ${...} first (greedy inner match)
    value = re.sub(r"\$\{[^}]*\}", "", value)
    # Strip bare $WORD references (uppercase env var names)
    value = re.sub(r"\$[A-Z_][A-Z0-9_]*", "", value)
    return value.strip()


def _expand_wl_token(token):
    """
    Expand a packed -Wl,a,b,c token into individual sub-tokens.
    Returns a list with one entry per comma-separated sub-flag.
    Non -Wl tokens are returned as a single-element list unchanged.

    Splitting -Wl,-z,relro into ["-Wl,-z", "-Wl,relro"] is semantically
    equivalent: the compiler driver passes each as a separate linker arg.

    Examples:
        "-Wl,-O1,--sort-common,--as-needed"
            -> ["-Wl,-O1", "-Wl,--sort-common", "-Wl,--as-needed"]
        "-Wl,-z,relro"
            -> ["-Wl,-z", "-Wl,relro"]
        "-pipe"
            -> ["-pipe"]
    """
    m = _WL_RE.match(token)
    if not m:
        return [token]

    parts = m.group(1).split(",")
    return [f"-Wl,{part}" for part in parts if part]


def _tokenize_flag_value(value):
    """
    Tokenize a flag string into individual flag tokens, expanding -Wl,...
    packed tokens into their sub-tokens.

    Returns a list of string tokens.

    Example:
        "-march=native -O2 -Wl,-O1,--as-needed -pipe"
        -> ["-march=native", "-O2", "-Wl,-O1", "-Wl,--as-needed", "-pipe"]
    """
    tokens = []
    for raw in value.split():
        tokens.extend(_expand_wl_token(raw))
    return tokens


def _extract_flag_assignments(function_body, pkgname="unknown"):
    """
    Extract flag assignments from a single function body string.

    Handles:
      - Bare assignments:     CFLAGS="-O2 -pipe"
      - Append assignments:   CFLAGS+="-fuse-ld=mold"
      - Exported assignments: export CFLAGS="..."
      - All $VAR/${...} refs stripped; skipped if any $ remains after strip

    Skips and logs (does NOT extract):
      - Assignments where value contains unresolvable $-expressions
      - make VAR=... inline invocations   [PATCH] Skipped (inline make)
      - cmake -DKEY=... invocations       [PATCH] Skipped (inline cmake)

    Returns:
        dict mapping key -> list of extracted flag tokens
        Keys with no extractable tokens are omitted.
    """
    accumulated: dict[str, list[str]] = {}  # key -> token list

    for m in _ASSIGNMENT_RE.finditer(function_body):
        key = m.group("key")
        op = m.group("op")
        raw_value = m.group("value")

        if key not in _EXTRACTABLE_KEYS:
            continue

        # Strip surrounding quotes
        if (raw_value.startswith('"') and raw_value.endswith('"')) or \
           (raw_value.startswith("'") and raw_value.endswith("'")):
            raw_value = raw_value[1:-1]

        stripped = _strip_var_refs(raw_value)

        # Skip if complex bash expression remains (e.g. ${VAR/-g /-g1 })
        if _VARREF_RE.search(stripped):
            _log.info("[PATCH]", f"[{pkgname}] Skipped (complex expression, not extractable): {key}{op}... — line will still be removed")
            continue

        tokens = _tokenize_flag_value(stripped)
        if not tokens:
            # e.g. export CXXFLAGS="$CFLAGS" after stripping → empty; remove silently
            continue

        if op == "=" or key not in accumulated:
            accumulated[key] = tokens if op == "=" else []
            if op == "+=":
                accumulated[key].extend(tokens)
        else:
            accumulated[key].extend(tokens)

        _log.info("[PATCH]", f"[{pkgname}] Extracted {key}{op} tokens: {tokens}")

    # Log inline make/cmake patterns (not extracted, logged, removed by apply_patch_pkgbuild)
    for pat, label in ((_INLINE_MAKE_RE, "inline make"), (_INLINE_CMAKE_RE, "inline cmake")):
        for m in pat.finditer(function_body):
            _log.info("[PATCH]", f"[{pkgname}] Skipped ({label}): {m.group(0).strip()!r}")

    return {k: v for k, v in accumulated.items() if v}


def _extract_conditional_blocks(function_body, pkgname="unknown"):
    """
    Find all if...fi conditional blocks in a function body that contain
    at least one assignment to an extractable key.

    Uses depth tracking to correctly handle nested if blocks.

    Returns list of (start, end, keys_found, block_text) tuples where:
      start, end  — character positions in function_body
      keys_found  — sorted list of extractable keys found in the block
      block_text  — the full block text (for logging)

    Entire blocks are returned regardless of whether they contain non-flag
    logic — the caller is responsible for logging and removing them.
    """
    blocks = []
    lines = function_body.splitlines(keepends=True)
    pos = 0  # character position of current line start
    line_positions = []
    for line in lines:
        line_positions.append(pos)
        pos += len(line)

    i = 0
    while i < len(lines):
        line = lines[i]
        if _IF_RE.match(line):
            # Found the start of a conditional block; track depth
            block_start_char = line_positions[i]
            depth = 1
            j = i + 1
            while j < len(lines) and depth > 0:
                if _IF_RE.match(lines[j]):
                    depth += 1
                elif _FI_RE.match(lines[j]):
                    depth -= 1
                j += 1
            # j now points one past the fi line
            block_end_char = line_positions[j - 1] + len(lines[j - 1])
            block_text = function_body[block_start_char:block_end_char]

            # Check if block contains any extractable key assignments
            keys_found = set()
            for m in _ASSIGNMENT_RE.finditer(block_text):
                if m.group("key") in _EXTRACTABLE_KEYS:
                    keys_found.add(m.group("key"))

            if keys_found:
                blocks.append((
                    block_start_char,
                    block_end_char,
                    sorted(keys_found),
                    block_text,
                ))

            i = j
        else:
            i += 1

    return blocks


def extract_pkgbuild_profile(pkgmeta, pkgbuild_path):
    """
    Scan all PKGBUILD function bodies and extract flag assignments into a
    synthetic profile dict for use as the implicit chain root in merge_extends.

    Processing per function:
      1. Detect conditional blocks containing extractable keys → log + schedule removal
      2. Extract assignments outside conditional blocks
      3. Strip var refs; skip if complex expression remains
      4. Tokenize (with -Wl expansion)

    Tokens from multiple functions are accumulated in function-body order.
    += accumulates; = resets the token list for that key.

    Returns:
        dict  e.g. {"CFLAGS": "-fno-stack-protector -m32", "LDFLAGS": "-Wl,--gc-sections"}
        Empty dict if no extractable flags are found.
    """
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    functions = pkgmeta.get("functions", {})

    accumulated: dict[str, list[str]] = {}

    for func_name, body in functions.items():
        # Phase 1: log conditional blocks that will be removed by apply_patch_pkgbuild
        for start, end, keys, block_text in _extract_conditional_blocks(body, pkgname):
            preview = block_text.splitlines()[0][:60]
            _log.info("[PATCH]", f"[{pkgname}] Removing entire conditional block in {func_name!r} (contains {keys}): {preview!r}...")

        # Phase 2: extract assignments
        for key, tokens in _extract_flag_assignments(body, pkgname).items():
            if key not in accumulated:
                accumulated[key] = list(tokens)
            else:
                accumulated[key].extend(tokens)

    if not accumulated:
        _log.info("[PATCH]", f"[{pkgname}] No extractable flags found in function bodies")
        return {}

    profile = {k: " ".join(v) for k, v in accumulated.items()}

    for key, val in profile.items():
        _log.info("[PATCH]", f"[{pkgname}] Extracted profile key: {key} = {val!r}")

    return profile


# ---------------------------------------------------------------------------
# Extracted profile persistence
# ---------------------------------------------------------------------------

def write_extracted_profile(profile, pkgbuild_path):
    """
    Write the extracted profile dict to pkgbuild_extracted_profile.toml
    in the PKGBUILD's directory.

    Written for transparency and diagnosis. Cleaned up on successful build;
    persists on failure. Returns the path to the written file.
    """
    out_path = Path(pkgbuild_path).parent / "pkgbuild_extracted_profile.toml"

    # Filter out internal metadata keys before writing
    clean = {k: v for k, v in profile.items() if not k.startswith("__")}

    lines = [
        "# Generated by SysForge — do not edit manually",
        f"# Source: {pkgbuild_path}",
        "# Flags extracted from PKGBUILD function bodies.",
        "# Forms the lowest-priority root of the profile inheritance chain.",
        "",
        "[profiles.pkgbuild_extracted]",
    ]
    for key, val in sorted(clean.items()):
        # Escape any backslashes and double-quotes for TOML string safety
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key} = "{escaped}"')

    _log.debug("[PATCH]", f"Extracted profile TOML:\n{chr(10).join(lines)}")
    out_path.write_text("\n".join(lines) + "\n")
    _log.info("[PATCH]", f"Wrote extracted profile: {out_path}")
    return out_path


def load_extracted_profile(pkgbuild_path):
    """
    Load a previously written pkgbuild_extracted_profile.toml from the
    PKGBUILD's directory, if it exists.

    Returns the profile dict, or an empty dict if the file is absent.
    Used to re-attach the extracted profile on retry after a failed build.
    """
    toml_path = Path(pkgbuild_path).parent / "pkgbuild_extracted_profile.toml"
    if not toml_path.exists():
        return {}

    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    profile = data.get("profiles", {}).get("pkgbuild_extracted", {})
    _log.info("[PATCH]", f"Loaded extracted profile from: {toml_path}")
    return profile


# ---------------------------------------------------------------------------
# Full patching pass
# ---------------------------------------------------------------------------

def apply_patch_pkgbuild(pkgbuild_path, pkgmeta):
    """
    Write a patched copy of the PKGBUILD (PKGBUILD.sysforge) with all
    managed flag assignments and conditional blocks removed.

    Removal targets (in a single pass over the original text):
      1. Entire if...fi blocks that contained extractable key assignments
         — logged with line number and keys found; entire block removed
      2. Any line matching an assignment to an extractable key
         (bare, export, +=, complex bash forms, empty-after-strip)
         — whether extraction succeeded or not
      3. Inline make VAR=... and cmake -DKEY=... lines
         — logged as skipped; removed

    Does not modify the original PKGBUILD.
    Returns the path to PKGBUILD.sysforge.
    """
    pkgbuild_path = Path(pkgbuild_path)
    patched_path = pkgbuild_path.parent / "PKGBUILD.sysforge"

    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    original_text = pkgbuild_path.read_text()

    # --- Step 1: collect conditional block spans to remove ---
    # We need spans in the *original file*, not the function body substring.
    # Recompute from the original text using the same depth-tracking approach.
    conditional_spans: list[tuple[int, int, list[str]]] = []
    lines = original_text.splitlines(keepends=True)
    pos = 0
    line_positions = []
    for line in lines:
        line_positions.append(pos)
        pos += len(line)

    i = 0
    while i < len(lines):
        line = lines[i]
        if _IF_RE.match(line):
            block_start = line_positions[i]
            depth = 1
            j = i + 1
            while j < len(lines) and depth > 0:
                if _IF_RE.match(lines[j]):
                    depth += 1
                elif _FI_RE.match(lines[j]):
                    depth -= 1
                j += 1
            block_end = line_positions[j - 1] + len(lines[j - 1])
            block_text = original_text[block_start:block_end]

            keys_found = set()
            for m in _ASSIGNMENT_RE.finditer(block_text):
                if m.group("key") in _EXTRACTABLE_KEYS:
                    keys_found.add(m.group("key"))

            if keys_found:
                # Find line number for the log
                line_no = i + 1
                preview = lines[i].rstrip()[:60]
                _log.info("[PATCH]", f"[{pkgname}] Removing conditional block at line {line_no} (keys: {sorted(keys_found)}): {preview!r}")
                conditional_spans.append((block_start, block_end, sorted(keys_found)))
            i = j
        else:
            i += 1

    # --- Step 2: remove conditional blocks from text (reverse order to preserve offsets) ---
    working = original_text
    offset = 0
    for start, end, _ in conditional_spans:
        adj_start = start - offset
        adj_end = end - offset
        working = working[:adj_start] + working[adj_end:]
        offset += end - start

    # --- Step 3: remove flag assignment lines line-by-line ---
    result_lines = []
    for line_no, line in enumerate(working.splitlines(keepends=True), start=1):
        m = _ASSIGNMENT_RE.match(line)
        if m and m.group("key") in _EXTRACTABLE_KEYS:
            _log.info("[PATCH]", f"[{pkgname}] Removed assignment line {line_no}: {line.rstrip()!r}")
            continue  # drop this line

        # Inline make/cmake lines — only strip if the key is one we manage
        m_make = _INLINE_MAKE_RE.match(line)
        if m_make and m_make.group("key") in _EXTRACTABLE_KEYS:
            _log.info("[PATCH]", f"[{pkgname}] Removed inline make line {line_no}: {line.rstrip()!r}")
            continue
        m_cmake = _INLINE_CMAKE_RE.match(line)
        if m_cmake and m_cmake.group("key") in _EXTRACTABLE_KEYS:
            _log.info("[PATCH]", f"[{pkgname}] Removed inline cmake line {line_no}: {line.rstrip()!r}")
            continue

        result_lines.append(line)

    patched_text = "".join(result_lines)
    patched_path.write_text(patched_text)
    _log.info("[PATCH]", f"[{pkgname}] Wrote patched PKGBUILD: {patched_path}")
    return patched_path


# ---------------------------------------------------------------------------
# Noninteractive kconfig patching
# ---------------------------------------------------------------------------

def patch_noninteractive_kconfig(patched_path):
    """
    Replace interactive kconfig targets in a (already-patched) PKGBUILD file
    with `make olddefconfig`, which applies defaults non-interactively.

    Handles: oldconfig, nconfig, menuconfig, xconfig, gconfig.
    Preserves any VAR=val arguments before the target (e.g. ARCH=x86_64).
    Logs each replacement.

    Called by the kernel stage on PKGBUILD.sysforge after normal patching.
    Does not create a new file — modifies patched_path in place.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text()
    replacements = []

    def _replace(m):
        original = m.group(0)
        replaced = m.group(1) + "olddefconfig" + m.group(3)
        replacements.append((m.group(2), original.strip(), replaced.strip()))
        return replaced

    new_text = _INTERACTIVE_KCONFIG_RE.sub(_replace, text)

    if replacements:
        patched_path.write_text(new_text)
        for target, original, replaced in replacements:
            _log.info(
                "[PATCH]",
                f"Replaced interactive kconfig target {target!r} with olddefconfig: "
                f"{original!r} → {replaced!r}",
            )
    else:
        _log.info("[PATCH]", "No interactive kconfig targets found — nothing replaced")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_patch_artifacts(pkgbuild_path):
    """
    Remove PKGBUILD.sysforge and pkgbuild_extracted_profile.toml from the
    PKGBUILD's directory if they exist.

    Called on successful build. On failure, artifacts persist for diagnosis.
    """
    pkgbuild_path = Path(pkgbuild_path)
    build_dir = pkgbuild_path.parent

    for name in ("PKGBUILD.sysforge", "pkgbuild_extracted_profile.toml"):
        target = build_dir / name
        if target.exists():
            target.unlink()
            _log.info("[PATCH]", f"Removed artifact: {target}")
