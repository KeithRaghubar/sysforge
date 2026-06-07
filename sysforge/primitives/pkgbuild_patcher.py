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
from sysforge import log
_log = log.get_logger("PATCH")
import tomllib
from pathlib import Path
from sysforge.primitives.profile import CONF_KEY_MAP


# ---------------------------------------------------------------------------
# Target keys for flag extraction
#
# Derived from the authoritative CONF_KEY_MAP in profile.py — single source
# of truth; no manual sync required.
# ---------------------------------------------------------------------------

_EXTRACTABLE_KEYS = frozenset().union(*CONF_KEY_MAP.values())

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
    after the pkgname assignment (which may span multiple lines).
    Returns the path to the patched copy (PKGBUILD.sysforge).
    """
    patched_path = pkgbuild_path.parent / "PKGBUILD.sysforge"
    groups_line = "groups=(" + " ".join(f'"{g}"' for g in groups) + ")"

    text = pkgbuild_path.read_text()

    # Replace existing groups=(...) — [^)]* matches across newlines in char classes
    new_text, count = re.subn(
        r"^groups=\([^)]*\)",
        groups_line,
        text,
        flags=re.MULTILINE,
    )

    if count == 0:
        # Insert after the complete pkgname assignment, tracking paren depth
        # so multi-line pkgname=(\n  pkg1\n  pkg2\n) is handled correctly.
        lines = text.splitlines(keepends=True)
        result = []
        inserted = False
        j = 0
        while j < len(lines):
            line = lines[j]
            result.append(line)
            if not inserted and re.match(r"^pkgname=", line):
                depth = line.count("(") - line.count(")")
                while depth > 0 and j + 1 < len(lines):
                    j += 1
                    result.append(lines[j])
                    depth += lines[j].count("(") - lines[j].count(")")
                result.append(groups_line + "\n")
                inserted = True
            j += 1
        new_text = "".join(result)

    patched_path.write_text(new_text)
    _log.info(f"Wrote patched PKGBUILD: {patched_path}")
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
            _log.info(f"[{pkgname}] Skipped (complex expression, not extractable): {key}{op}... — line will still be removed")
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

        _log.info(f"[{pkgname}] Extracted {key}{op} tokens: {tokens}")

    # Log inline make/cmake patterns (not extracted, logged, removed by apply_patch_pkgbuild)
    for pat, label in ((_INLINE_MAKE_RE, "inline make"), (_INLINE_CMAKE_RE, "inline cmake")):
        for m in pat.finditer(function_body):
            _log.info(f"[{pkgname}] Skipped ({label}): {m.group(0).strip()!r}")

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
            _log.info(f"[{pkgname}] Removing entire conditional block in {func_name!r} (contains {keys}): {preview!r}...")

        # Phase 2: extract assignments
        for key, tokens in _extract_flag_assignments(body, pkgname).items():
            if key not in accumulated:
                accumulated[key] = list(tokens)
            else:
                accumulated[key].extend(tokens)

    if not accumulated:
        _log.info(f"[{pkgname}] No extractable flags found in function bodies")
        return {}

    profile = {k: " ".join(v) for k, v in accumulated.items()}

    for key, val in profile.items():
        _log.info(f"[{pkgname}] Extracted profile key: {key} = {val!r}")

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

    _log.debug(f"Extracted profile TOML:\n{chr(10).join(lines)}")
    out_path.write_text("\n".join(lines) + "\n")
    _log.info(f"Wrote extracted profile: {out_path}")
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
    _log.info(f"Loaded extracted profile from: {toml_path}")
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
                _log.info(f"[{pkgname}] Removing conditional block at line {line_no} (keys: {sorted(keys_found)}): {preview!r}")
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
            _log.info(f"[{pkgname}] Removed assignment line {line_no}: {line.rstrip()!r}")
            continue  # drop this line

        # Inline make/cmake lines — only strip if the key is one we manage
        m_make = _INLINE_MAKE_RE.match(line)
        if m_make and m_make.group("key") in _EXTRACTABLE_KEYS:
            _log.info(f"[{pkgname}] Removed inline make line {line_no}: {line.rstrip()!r}")
            continue
        m_cmake = _INLINE_CMAKE_RE.match(line)
        if m_cmake and m_cmake.group("key") in _EXTRACTABLE_KEYS:
            _log.info(f"[{pkgname}] Removed inline cmake line {line_no}: {line.rstrip()!r}")
            continue

        result_lines.append(line)

    patched_text = "".join(result_lines)
    patched_path.write_text(patched_text)
    _log.info(f"[{pkgname}] Wrote patched PKGBUILD: {patched_path}")

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
                f"Replaced interactive kconfig target {target!r} with olddefconfig: "
                f"{original!r} → {replaced!r}",
            )
    else:
        _log.info("No interactive kconfig targets found — nothing replaced")


# ---------------------------------------------------------------------------
# Subshell toolchain env reset
# ---------------------------------------------------------------------------

# Matches subshell function definitions: funcname() (
# Group 1 captures everything up to and including the opening '('.
_SUBSHELL_FUNC_RE = re.compile(
    r"^([ \t]*\w+\s*\(\)\s*\()[ \t]*$",
    re.MULTILINE,
)


def patch_subshell_env_reset(patched_path, toolchain_env, inherited_env=None):
    """
    Inject ``unset CC CXX LD ...`` at the top of every subshell function body
    in an already-written PKGBUILD.sysforge.

    Subshell functions — ``funcname() (...)`` — are isolated helper builds
    (musl bootstrap, embedded grub, dietlibc, etc.) that should use the
    system-default compiler and linker, not the sysforge profile toolchain
    or inherited shell overrides.  Without this reset, CC/CXX/LD leak into
    sub-builds and produce broken toolchains or linker failures.

    Considers two sources of non-default toolchain vars:
      - *toolchain_env*: CC/CXX from the resolved sysforge profile
      - *inherited_env*: vars from the parent shell (e.g. LD=ld.lld)

    Only injects the reset when at least one key differs from the system
    default (gcc/g++/ld).  Modifies patched_path in place.  Returns the
    number of functions patched.
    """
    # System defaults — if all values match these, no reset needed.
    _DEFAULTS = {"CC": "gcc", "CXX": "g++", "LD": "ld"}

    keys_to_reset = set()
    for k, v in toolchain_env.items():
        if k in _DEFAULTS and v != _DEFAULTS[k]:
            keys_to_reset.add(k)
    if inherited_env:
        for k in _DEFAULTS:
            if k in inherited_env and inherited_env[k] != _DEFAULTS[k]:
                keys_to_reset.add(k)
    if not keys_to_reset:
        return 0

    patched_path = Path(patched_path)
    text = patched_path.read_text()

    unset_line = "  unset " + " ".join(sorted(keys_to_reset))
    count = 0

    def _inject(m):
        nonlocal count
        count += 1
        return m.group(1) + "\n" + unset_line
    new_text = _SUBSHELL_FUNC_RE.sub(_inject, text)

    if count:
        patched_path.write_text(new_text)
        _log.info(
            f"Injected toolchain env reset into {count} subshell function(s): "
            f"{unset_line!r}"
        )

    return count


# ---------------------------------------------------------------------------
# LLVM_TARGETS_TO_BUILD injection (LLVM PKGBUILDs only)
# ---------------------------------------------------------------------------

# pkgbase prefixes / exact names that flag a PKGBUILD as part of the LLVM
# toolchain. The match is anchored at the start of pkgbase: "llvm",
# "llvm-git", "lib32-llvm", "clang", "compiler-rt", "lld" all match;
# "rust" / "ocaml-llvm" / etc. do not.
_LLVM_PKGBASE_PATTERNS = ("llvm", "lib32-llvm", "clang", "lib32-clang",
                          "compiler-rt", "lib32-compiler-rt",
                          "lld", "lib32-lld")

# Matches `-DLLVM_TARGETS_TO_BUILD=...` (with optional :STRING type tag and
# optional surrounding quotes) anywhere on a line. The value side is greedy
# until whitespace, line continuation `\`, or closing quote.
_LLVM_TARGETS_RE = re.compile(
    r'-DLLVM_TARGETS_TO_BUILD(?::[A-Z]+)?=(?:"[^"]*"|\'[^\']*\'|\S+)'
)

# A line that opens or continues a cmake invocation. We use this as the
# anchor for inserting -DLLVM_TARGETS_TO_BUILD when the upstream PKGBUILD
# does not already set it.
_CMAKE_INVOCATION_RE = re.compile(r"^([ \t]*)cmake\b", re.MULTILINE)


def is_llvm_pkgbase(pkgbase: str | None) -> bool:
    """Return True if pkgbase looks like an LLVM-toolchain package."""
    if not pkgbase:
        return False
    return any(pkgbase == p or pkgbase.startswith(p + "-")
               for p in _LLVM_PKGBASE_PATTERNS)


def patch_llvm_targets(patched_path, targets: list[str]) -> bool:
    """Inject or replace `-DLLVM_TARGETS_TO_BUILD="<targets>"` in the
    cmake invocation of an already-written PKGBUILD.sysforge.

    Idempotent: re-running on a PKGBUILD that already carries the same
    targets value is a no-op. On a no-cmake-found PKGBUILD, logs a warning
    and returns False — upstream may have switched build systems.

    Returns True when the file was modified.
    """
    if not targets:
        return False
    value = ";".join(targets)
    replacement = f'-DLLVM_TARGETS_TO_BUILD="{value}"'

    patched_path = Path(patched_path)
    text = patched_path.read_text()

    # (1) Already present? — replace if value differs, no-op if same.
    existing = _LLVM_TARGETS_RE.search(text)
    if existing:
        if existing.group(0) == replacement:
            return False
        new_text = _LLVM_TARGETS_RE.sub(replacement, text, count=1)
        patched_path.write_text(new_text)
        _log.info(f"Replaced LLVM_TARGETS_TO_BUILD: {existing.group(0)!r} → {replacement!r}")
        return True

    # (2) Insert after the first cmake invocation line. We append the new
    # arg as a continuation: indentation matched, trailing backslash so
    # the next line stays part of the same shell statement.
    cmake_match = _CMAKE_INVOCATION_RE.search(text)
    if not cmake_match:
        _log.warn("LLVM target filtering requested but no cmake invocation "
                  "found in PKGBUILD — leaving unmodified")
        return False

    indent = cmake_match.group(1)
    line_end = text.find("\n", cmake_match.end())
    if line_end == -1:
        line_end = len(text)
    insertion = f" \\\n{indent}    {replacement}"
    new_text = text[:line_end] + insertion + text[line_end:]
    patched_path.write_text(new_text)
    _log.info(f"Injected {replacement} after cmake invocation")
    return True


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
            _log.info(f"Removed artifact: {target}")


def warn_artifacts_left(has_extracted_profile: bool) -> None:
    """Log (under the PATCH tag) that patch artifacts are being kept after a
    failed build for diagnosis.

    The failure-side counterpart to :func:`cleanup_patch_artifacts`: this module
    owns the artifact names, so the build orchestrator delegates the message
    here instead of re-spelling ``PKGBUILD.sysforge`` itself.
    """
    artifacts = "PKGBUILD.sysforge"
    if has_extracted_profile:
        artifacts += " and pkgbuild_extracted_profile.toml"
    _log.warn(f"Build failed — leaving {artifacts} in place for diagnosis")
