# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
from sysforge.primitives.hardware_tables import (
    MESA_MANDATORY_GALLIUM,
    MESA_MANDATORY_VULKAN,
)
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

# Interactive kconfig targets — require terminal input or a TUI (bare
# `config` line-prompts for every symbol; the rest are full-screen UIs).
# Replaced with `make olddefconfig` in noninteractive mode (kernel stage).
# `config` sits last in the alternation so `oldconfig` can't be half-matched
# (the preceding \s+ prevents mid-word matches either way).
# Groups: (1) leading whitespace + make + optional VAR=val args + space
#         (2) the interactive target name
#         (3) optional trailing whitespace / comment
_INTERACTIVE_KCONFIG_RE = re.compile(
    r"^(\s*make(?:\s+\w+=\S*)*\s+)(oldconfig|nconfig|menuconfig|xconfig|gconfig|config)"
    r"(\s*(?:#.*)?)$",
    re.MULTILINE,
)

# Review-only kconfig targets — the ncurses/X menus that constitute an operator
# review. `oldconfig` is deliberately EXCLUDED: it is a non-interactive *resolve*
# step (after the fragment merge it prompts for nothing), so it must NOT suppress
# sysforge's injected `make nconfig` review (B17). Distinct from
# _INTERACTIVE_KCONFIG_RE, which keeps `oldconfig` because its other consumer,
# patch_noninteractive_kconfig, rewrites `oldconfig` → `olddefconfig`.
_REVIEW_KCONFIG_RE = re.compile(
    r"^(\s*make(?:\s+\w+=\S*)*\s+)(nconfig|menuconfig|xconfig|gconfig|config)"
    r"(\s*(?:#.*)?)$",
    re.MULTILINE,
)

# Anchors for injecting the sysforge.config fragment merge into a stock kernel
# PKGBUILD's prepare(). Primary: a non-interactive kconfig-resolve line (the
# point where .config is established) — group 1 captures its indentation.
_KCONFIG_SETUP_RE = re.compile(
    r"^([ \t]*)make(?:\s+\w+=\S*)*\s+"
    r"(?:olddefconfig|oldconfig|defconfig|alldefconfig|localmodconfig|"
    r"localyesconfig|mod2yesconfig|allmodconfig)\b.*$",
    re.MULTILINE,
)
# Secondary anchor: the line that creates .config (cp/cat into .config), used
# when the PKGBUILD seeds .config without a make-config resolve step.
_CONFIG_WRITE_RE = re.compile(
    r"^([ \t]*)(?:cp\s+\S+\s+\.config|cat\b.*>\s*\.config)\b.*$",
    re.MULTILINE,
)


def _package_func_re(pkgname: str) -> re.Pattern:
    """Match the opening of the kernel's package function up to its ``{``.

    Prefers the split-package ``package_<pkgname>()`` (the main image
    subpackage, which conventionally installs /boot files) and falls back to a
    bare ``package()``. Group 1 captures the function's leading indentation;
    ``re.escape`` handles pkgnames with ``-``/``.`` (e.g. ``linux-sysforge``).
    """
    return re.compile(
        r"^([ \t]*)package(?:_" + re.escape(pkgname) + r")?\s*\(\)\s*\{",
        re.MULTILINE,
    )


# Detect a PKGBUILD that already installs a kernel config to /boot — both a
# native ``install … /boot/config-…`` and a prior sysforge injection — so the
# injection stays idempotent.
_BOOT_CONFIG_RE = re.compile(r"/boot/config\b")

# Standard Arch kernel PKGBUILDs don't define ``package_<pkgname>()`` literally;
# they define helper functions (``_package``, ``_package-headers``,
# ``_package-docs``) and synthesize the real ``package_$_p()`` via an ``eval``
# loop, which ``_package_func_re`` can't see statically. The base image helper
# is the unsuffixed ``_package()`` — inject the /boot config install there. The
# ``-headers``/``-docs`` helpers don't match (text sits between ``_package`` and
# ``()``).
_EVAL_LOOP_BASE_PACKAGE_RE = re.compile(
    r"^([ \t]*)_package\s*\(\)\s*\{", re.MULTILINE)

# bpftool's ``vmlinux.h`` target/artifact requires a ``.BTF`` section in
# ``vmlinux``, which only exists when ``CONFIG_DEBUG_INFO_BTF=y``. A lean
# (BTF-off) resolved ``.config`` — e.g. ``base_config="running"`` seeded from a
# debug-info-free kernel — makes the stock PKGBUILD's unconditional
# ``make … vmlinux.h`` build step and its ``package()`` install hard-fail. These
# anchors gate both behind a runtime ``CONFIG_DEBUG_INFO_BTF`` check (the idiom
# the kernel PKGBUILD already uses for ``CONFIG_DEBUG_INFO_BTF_MODULES``). The
# build-step pattern requires ``make`` immediately after the indent, so a
# commented-out step (``#  make …``) is naturally skipped. The install pattern
# spans backslash-continuations so it captures the one multi-line ``install``
# statement that lists ``vmlinux.h`` without overrunning into the next command.
_BPFTOOL_VMLINUX_H_BUILD_RE = re.compile(
    r"^([ \t]*)(make[ \t]+-C[ \t]+tools/bpf/bpftool\b[^\n]*\bvmlinux\.h\b[^\n]*)$",
    re.MULTILINE,
)
_VMLINUX_H_INSTALL_STMT_RE = re.compile(
    r"^([ \t]*)install\b(?:[^\n]*\\\n)*[^\n]*tools/bpf/bpftool/vmlinux\.h[^\n]*$",
    re.MULTILINE,
)
_BPFTOOL_VMLINUX_H_INSTALL_TOKEN_RE = re.compile(
    r"[ \t]+tools/bpf/bpftool/vmlinux\.h\b"
)
# Sentinel marking a prior BTF-guard injection (present in both the wrapped
# build step and the guarded install) — keeps the patch idempotent.
_BTF_GUARD_SENTINEL = "# sysforge: BTF guard"


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
            _log.info(
                f"[{pkgname}] Skipped (complex expression, not extractable): "
                f"{key}{op}... — line will still be removed"
            )
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
        for _start, _end, keys, block_text in _extract_conditional_blocks(body, pkgname):
            preview = block_text.splitlines()[0][:60]
            _log.info(
                f"[{pkgname}] Removing entire conditional block in {func_name!r} "
                f"(contains {keys}): {preview!r}..."
            )

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

    with toml_path.open("rb") as f:
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
                _log.info(
                    f"[{pkgname}] Removing conditional block at line {line_no} "
                    f"(keys: {sorted(keys_found)}): {preview!r}"
                )
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

    Handles: oldconfig, nconfig, menuconfig, xconfig, gconfig, config.
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


# All targets this patcher recognizes as "a kconfig generation invocation" —
# the full set from `make help`'s config-targets section (kernel.toml
# ``kconfig_targets`` validates against the same set; see
# resolve_kconfig_targets).
_ALL_KCONFIG_TARGETS = (
    "config|nconfig|menuconfig|xconfig|gconfig|oldconfig|olddefconfig|"
    "localmodconfig|localyesconfig|mod2yesconfig|defconfig|allmodconfig|"
    "alldefconfig|savedefconfig|listnewconfig|randconfig"
)
_ANY_KCONFIG_RE = re.compile(
    # Trailer is same-line only ([ \t], not \s): a multi-line \s* would let a
    # match swallow a following comment line and misclassify the invocation.
    r"^([ \t]*)(make(?:\s+\w+=\S*)*[ \t]+)(?:" + _ALL_KCONFIG_TARGETS
    + r")([ \t]*(?:#.*)?)$",
    re.MULTILINE,
)

# Sentinel comment on kconfig-resolve lines injected by
# patch_kernel_kconfig_apply (inside the base-seed / merge_config.sh guard
# blocks). patch_kconfig_targets must not treat sysforge-injected lines as the
# PKGBUILD's own kconfig steps: they are neither removed nor used as the
# anchor, and the configured sequence is inserted *after* the guard block
# carrying the last one, so the fragment merge stays intact and the configured
# targets (UI review included) operate on the fully seeded+merged .config.
_KCONFIG_RESOLVE_SENTINEL = "# sysforge: kconfig-resolve"


def patch_kconfig_targets(patched_path, targets: list[str]) -> None:
    """Replace a kernel PKGBUILD's kconfig-generation invocation(s) with the
    configured target sequence (kernel.toml ``kconfig_targets``, already
    validated/ordered by the kernel stage).

    Every PKGBUILD-owned ``make <kconfig-target>`` line is removed — the
    configured list is the sole authority for kconfig generation. Lines
    carrying a ``# sysforge:`` comment are exempt: they belong to
    ``patch_kernel_kconfig_apply``'s base-seed / fragment-merge guard blocks
    (tagged with ``_KCONFIG_RESOLVE_SENTINEL``) and must survive so the merge
    stays resolved. The configured block reuses the first removed line's
    indentation and ``VAR=val`` arguments (ARCH=, LLVM=, …) and is inserted
    *after* the injected merge block when one exists (so the configured
    sequence — including any UI review target, always last — operates on the
    fully seeded+merged ``.config``); with no sentinel block present it takes
    the first removed line's position. Raises RuntimeError when no
    PKGBUILD-owned kconfig invocation exists (exotic PKGBUILD): never build
    with a half-patched config step.

    Removals are spliced last-to-first so earlier offsets stay valid.
    Mirrors the sibling kernel patchers: edits patched_path
    (PKGBUILD.sysforge) in place.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text()
    matches = [
        m for m in _ANY_KCONFIG_RE.finditer(text)
        if "# sysforge:" not in m.group(0)
    ]
    if not matches:
        raise RuntimeError(
            f"kconfig_targets: no kconfig make invocation found in "
            f"{patched_path} — cannot anchor the configured target sequence"
        )

    first = matches[0]
    indent, make_prefix, trailer = first.group(1), first.group(2), first.group(3)
    # Remove every PKGBUILD-owned match, last-to-first, so earlier offsets
    # (including first.start()) stay valid. Also swallow the newline that
    # terminated each removed line so no blank line is left behind.
    for m in reversed(matches):
        start = m.start()
        end = m.end()
        if end < len(text) and text[end] == "\n":
            end += 1
        elif start > 0 and text[start - 1] == "\n":
            start -= 1
        text = text[:start] + text[end:]

    # Composition with patch_kernel_kconfig_apply: when its sentinel-tagged
    # resolve lines are present, insert after the guard block carrying the
    # last one (its closing `fi`), so the configured sequence runs on the
    # merged .config. Otherwise the first removed line's position anchors.
    insert_at = first.start()
    s_idx = text.rfind(_KCONFIG_RESOLVE_SENTINEL)
    if s_idx != -1:
        line_end = text.find("\n", s_idx)
        insert_at = line_end + 1 if line_end != -1 else len(text)
        next_nl = text.find("\n", insert_at)
        if next_nl != -1 and text[insert_at:next_nl].strip() == "fi":
            insert_at = next_nl + 1

    block = "\n".join(f"{indent}{make_prefix}{t}" for t in targets) + trailer + "\n"
    text = text[:insert_at] + block + text[insert_at:]

    patched_path.write_text(text)
    _log.info(
        f"Patched kconfig invocation to configured target sequence: {targets!r}",
    )


_HOTPLUG_FRAGMENT = "sysforge.hotplug.config"


def patch_hotplug_fragment_merge(patched_path, *, fragment=_HOTPLUG_FRAGMENT):
    """Inject a post-minimization merge of the hotplug kconfig fragment (F2).

    The fragment (written by the kernel stage when ``keep_hotplug_drivers`` is on)
    re-enables hotplug driver classes as modules. It MUST merge after any
    minimizer (``make localmodconfig`` strips drivers for hardware not currently
    present) and before any UI review target, so the operator reviews — and the
    build ships — the re-enabled modules.

    Placement: immediately before the LAST review target (``_REVIEW_KCONFIG_RE``)
    when one exists (the configured UI tail, or the injected ``make nconfig``);
    otherwise after the last kconfig target line of any kind (``_ANY_KCONFIG_RE``
    — covers both the sysforge resolve/merge lines and any PKGBUILD-owned/
    configured invocation such as a minimizing ``make localmodconfig`` with no
    UI target), so a minimizer can never run after the merge. File-existence
    guarded, so it is inert when the stage wrote no fragment. Idempotent: a
    second call finds the fragment reference and returns. Edits
    ``patched_path`` in place.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    if fragment in text:
        return  # already injected

    # Determine insertion offset + indentation.
    review = list(_REVIEW_KCONFIG_RE.finditer(text))
    if review:
        last = review[-1]
        insert_at = last.start()
        _indent_m = re.match(r"[ \t]*", last.group(0))
        indent = _indent_m.group(0) if _indent_m else ""
    else:
        any_matches = list(_ANY_KCONFIG_RE.finditer(text))
        if any_matches:
            last = any_matches[-1]
            line_end = text.find("\n", last.end())
            insert_at = line_end + 1 if line_end != -1 else len(text)
            # Skip a trailing guard-block `fi`, mirroring patch_kconfig_targets.
            next_nl = text.find("\n", insert_at)
            if next_nl != -1 and text[insert_at:next_nl].strip() == "fi":
                insert_at = next_nl + 1
            _indent_m = re.match(r"[ \t]*", text[insert_at:])
            indent = _indent_m.group(0) if _indent_m else ""
        else:
            # Neither a review target nor a sysforge resolve block exists yet
            # (e.g. a stock PKGBUILD before patch_kernel_kconfig_apply runs):
            # fall back to the same kconfig-setup anchor patch_kernel_kconfig_apply
            # uses (the `make olddefconfig`/`.config` seed line).
            anchor = _find_kconfig_anchor(text)
            if anchor is None:
                _log.warn(
                    "No kconfig resolve/review anchor found — cannot inject the "
                    "hotplug fragment merge; hotplug modules will not be re-enabled."
                )
                return
            insert_at, indent = anchor

    block = "\n".join([
        f"{indent}# sysforge: re-enable hotplug drivers as modules after minimization (F2)",
        f'{indent}if [ -f "$startdir/{fragment}" ]; then',
        f'{indent}  ./scripts/kconfig/merge_config.sh -m .config "$startdir/{fragment}"',
        f"{indent}  make olddefconfig  {_KCONFIG_RESOLVE_SENTINEL}",
        f"{indent}fi",
    ]) + "\n"

    patched_path.write_text(text[:insert_at] + block + text[insert_at:], encoding="utf-8")
    _log.info("Injected post-minimization hotplug fragment merge into kernel PKGBUILD")


def _find_kconfig_anchor(text: str):
    """Locate where to inject the fragment-merge in prepare().

    Returns ``(insert_index, indent)`` where ``insert_index`` is the offset just
    past the anchor line's content (before its newline) and ``indent`` is the
    anchor's leading whitespace, or ``None`` when no anchor is found. Prefers a
    non-interactive kconfig-resolve line (``make olddefconfig`` etc.); falls back
    to the ``.config`` creation line.
    """
    m = _KCONFIG_SETUP_RE.search(text) or _CONFIG_WRITE_RE.search(text)
    if m is None:
        return None
    return m.end(), m.group(1)


def patch_kernel_kconfig_apply(patched_path, *, interactive,
                               fragment="sysforge.config"):
    """Inject the base-config seed + sysforge.config fragment merge (and, when
    interactive, an interactive ``make nconfig`` review) into a stock kernel
    PKGBUILD.

    sysforge writes an optional base config to ``$startdir/sysforge.base.config``
    (when ``base_config`` is ``"running"``/``<path>``) and the merged kconfig
    fragment to ``$startdir/<fragment>``, but a stock PKGBUILD consumes neither.
    This patches ``prepare()``, right after the PKGBUILD establishes its base
    ``.config``, to:

      1. copy ``sysforge.base.config`` over ``.config`` (then ``make
         olddefconfig``) **when that file exists** — so ``base_config="running"``
         actually seeds the build instead of being a silent no-op; and
      2. merge ``<fragment>`` into ``.config`` (re-resolving with ``make
         olddefconfig``) — so the operator's hardware/device kconfig lands.

    Both steps are file-existence guarded, so the default ``base_config =
    "pkgbuild"`` (which writes no base file) is unaffected. When ``interactive``
    and the PKGBUILD has no interactive target of its own, a TTY-guarded
    "press Enter" pause followed by ``make nconfig`` is appended — the pause
    lands *after* the merges assembled the final ``.config`` and immediately
    before ``nconfig`` opens (B6), so the operator reviews the merged config.

    Skips PKGBUILDs that already cooperate (reference ``merge_config.sh`` or the
    fragment) to avoid double-injection. Modifies ``patched_path`` in place.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    if "merge_config.sh" in text or fragment in text:
        _log.info(
            "PKGBUILD already applies the sysforge kconfig fragment "
            "(merge_config.sh present) — skipping kconfig injection",
        )
        return

    anchor = _find_kconfig_anchor(text)
    if anchor is None:
        _log.warn(
            "No kconfig-setup anchor (make olddefconfig / .config seed) found in "
            "the kernel PKGBUILD prepare() — cannot inject the sysforge.config "
            "merge, so the hardware/device fragment will be ignored. Add a "
            "`make olddefconfig` step (or call merge_config.sh) in prepare().",
        )
        return

    insert_at, indent = anchor
    add_nconfig = interactive and not _REVIEW_KCONFIG_RE.search(text)
    block = [
        f"{indent}# sysforge: seed the base config (when provided), then merge the fragment",
        f'{indent}if [ -f "$startdir/sysforge.base.config" ]; then',
        f'{indent}  cp "$startdir/sysforge.base.config" .config',
        f"{indent}  make olddefconfig  {_KCONFIG_RESOLVE_SENTINEL}",
        f"{indent}fi",
        f'{indent}if [ -f "$startdir/{fragment}" ]; then',
        f'{indent}  ./scripts/kconfig/merge_config.sh -m .config "$startdir/{fragment}"',
        f"{indent}  make olddefconfig  {_KCONFIG_RESOLVE_SENTINEL}",
        f"{indent}fi",
    ]
    if add_nconfig:
        # B6: pause *here* — after the base seed + fragment merge have assembled
        # the final .config, immediately before nconfig opens — so the operator
        # reviews what was actually merged, not a pre-merge plan. A stage-level
        # pause before makepkg necessarily fires before these in-prepare() merges;
        # this is the only point that is genuinely "after all merges, before
        # nconfig". Guarded so it is inert off a TTY (pipeline / captured stdin)
        # and errexit-safe under makepkg's `set -e` (read returns non-zero on EOF,
        # swallowed by `|| true`; the `if` yields 0).
        block.extend([
            f"{indent}if [ -t 0 ]; then",
            f"{indent}  read -rp 'sysforge: merged kernel .config assembled — press "
            f"Enter to review it in nconfig (Ctrl-C aborts)… ' _sf_kconfig_ack || true",
            f"{indent}fi",
        ])
        block.append(f"{indent}make nconfig  # sysforge: interactive kconfig review")
    injected = "\n" + "\n".join(block)
    patched_path.write_text(
        text[:insert_at] + injected + text[insert_at:], encoding="utf-8")
    _log.info(
        "Injected sysforge.config fragment merge into kernel PKGBUILD prepare()"
        + (" + interactive `make nconfig`" if add_nconfig else ""),
    )


def patch_kernel_config_install(patched_path, *, pkgname):
    """Inject a ``/boot/config-<release>`` install into the kernel ``package()``.

    A stock kernel PKGBUILD does not always ship the resolved ``.config`` to
    ``/boot``. This patches the main package function (``package_<pkgname>()``
    for split kernels, else ``package()``) to install the built ``.config`` as
    ``$pkgdir/boot/config-<kernelrelease>`` so the running config is recoverable
    and pacman-tracked.

    The injected block is CWD-/layout-independent: it locates the kernel build
    tree via ``include/config/kernel.release`` (the same release source the
    stage's ``_built_kernel_release`` reads) anywhere under ``$srcdir``, then
    installs the sibling ``.config``. ``package()`` runs inside ``$srcdir`` with
    ``$pkgdir`` available (PKGBUILD(5)).

    When neither a literal ``package_<pkgname>()`` nor a bare ``package()`` is
    present, falls back to the unsuffixed ``_package()`` helper used by the
    standard Arch kernel PKGBUILD's ``eval``-loop split-package idiom (which
    synthesizes the real package functions at runtime, invisible to a static
    parser).

    Skips PKGBUILDs that already install to ``/boot/config`` (native or a prior
    injection) for idempotency. Modifies ``patched_path`` in place.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    if _BOOT_CONFIG_RE.search(text):
        _log.info(
            "PKGBUILD already installs a kernel config to /boot — skipping "
            "/boot config-install injection",
        )
        return

    m = _package_func_re(pkgname).search(text)
    if m is None:
        # Eval-loop split kernel (upstream Arch ``linux`` layout): inject into
        # the base image helper ``_package()``.
        m = _EVAL_LOOP_BASE_PACKAGE_RE.search(text)
    if m is None:
        _log.warn(
            f"No package() / package_{pkgname}() / _package() function found in "
            "the kernel PKGBUILD — cannot inject the /boot config install, so the "
            "resolved .config will not be shipped to /boot.",
        )
        return

    indent = m.group(1) + "  "
    insert_at = m.end()  # just past the opening brace
    block = [
        "",
        f"{indent}# sysforge: install the resolved kernel config to /boot",
        (
            f'{indent}_sf_rel=$(find "$srcdir" '
            r"-path '*/include/config/kernel.release' -type f 2>/dev/null | head -n1)"
        ),
        (
            f'{indent}if [ -n "$_sf_rel" ] && '
            f'[ -f "${{_sf_rel%/include/config/kernel.release}}/.config" ]; then'
        ),
        f'{indent}  install -Dm644 "${{_sf_rel%/include/config/kernel.release}}/.config" \\',
        f'{indent}    "$pkgdir/boot/config-$(<"$_sf_rel")"',
        f"{indent}fi",
    ]
    injected = "\n" + "\n".join(block)
    patched_path.write_text(
        text[:insert_at] + injected + text[insert_at:], encoding="utf-8")
    _log.info(
        f"Injected /boot config install into kernel PKGBUILD {m.group(0).strip()}",
    )


def patch_kernel_btf_guard(patched_path):
    """Gate the kernel PKGBUILD's bpftool ``vmlinux.h`` build + install on
    ``CONFIG_DEBUG_INFO_BTF``.

    A stock kernel PKGBUILD runs ``make -C tools/bpf/bpftool vmlinux.h`` in
    ``build()`` and installs the produced ``vmlinux.h`` in ``package()``, both
    unconditionally. Generating/installing ``vmlinux.h`` requires a ``.BTF``
    section in ``vmlinux``, which only exists when ``CONFIG_DEBUG_INFO_BTF=y``.
    When the resolved ``.config`` has BTF off (e.g. ``base_config="running"`` on
    a lean, debug-info-free kernel), the build hard-fails at the bpftool step
    with ``failed to find '.BTF' ELF section``.

    This wraps the build step in — and reduces the ``vmlinux.h`` install to — a
    runtime ``if [[ $(scripts/config -s CONFIG_DEBUG_INFO_BTF) = y ]]`` guard
    (the same idiom the PKGBUILD already uses for
    ``CONFIG_DEBUG_INFO_BTF_MODULES``), so a BTF-on build keeps both steps and a
    BTF-off build skips them. The guard is evaluated against the *real* resolved
    config at build time, so sysforge needs no BTF prediction.

    Mirrors the sibling kernel patchers: modifies ``patched_path`` in place,
    no-op when the build step is absent (commented out / already removed) and
    idempotent via the ``# sysforge: BTF guard`` sentinel. The operator's
    tracked PKGBUILD is untouched — this only edits the generated
    ``PKGBUILD.sysforge`` copy.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    if _BTF_GUARD_SENTINEL in text:
        _log.info(
            "PKGBUILD already carries the sysforge BTF guard — skipping "
            "vmlinux.h BTF-guard injection",
        )
        return

    changed = False

    # 1. Wrap the build() step so vmlinux.h is only generated when BTF is on.
    mb = _BPFTOOL_VMLINUX_H_BUILD_RE.search(text)
    if mb:
        indent = mb.group(1)
        wrapped = (
            f"{indent}if [[ $(scripts/config -s CONFIG_DEBUG_INFO_BTF) = y ]]; then  "
            f"{_BTF_GUARD_SENTINEL}\n"
            f"{indent}  {mb.group(2)}\n"
            f"{indent}fi"
        )
        text = text[:mb.start()] + wrapped + text[mb.end():]
        changed = True

    # 2. Pull vmlinux.h out of the unconditional package() install and re-add it
    #    behind the same guard (so a BTF-off build doesn't fail installing a file
    #    it never built). Strip only the vmlinux.h token; sibling files stay on
    #    the original install line.
    mi = _VMLINUX_H_INSTALL_STMT_RE.search(text)
    if mi:
        indent = mi.group(1)
        stmt_stripped = _BPFTOOL_VMLINUX_H_INSTALL_TOKEN_RE.sub("", mi.group(0))
        guarded = (
            f"\n{indent}if [[ $(scripts/config -s CONFIG_DEBUG_INFO_BTF) = y ]]; then  "
            f"{_BTF_GUARD_SENTINEL}\n"
            f'{indent}  install -Dt "$builddir" -m644 tools/bpf/bpftool/vmlinux.h\n'
            f"{indent}fi"
        )
        text = text[:mi.start()] + stmt_stripped + guarded + text[mi.end():]
        changed = True

    if changed:
        patched_path.write_text(text, encoding="utf-8")
        _log.info(
            "Gated bpftool vmlinux.h build/install on CONFIG_DEBUG_INFO_BTF "
            "(BTF-off configs skip it)",
        )
    else:
        _log.info(
            "No bpftool vmlinux.h build/install found in the kernel PKGBUILD — "
            "nothing to BTF-guard",
        )


# A standalone `make <…>docs` line in build()/prepare() — the kernel's
# Documentation targets all end in `docs` (htmldocs, pdfdocs, infodocs, mandocs,
# psdocs, xmldocs, texinfodocs, latexdocs, epubdocs, linkcheckdocs). Anchored so
# the make goals are *exclusively* doc targets (optional VAR=val/flag tokens
# allowed), never a mixed line like `make all htmldocs` where commenting out the
# whole line would also drop the real build.
_KERNEL_DOC_MAKE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<cmd>make(?:[ \t]+[A-Za-z_][\w]*=\S+|[ \t]+-\S+)*"
    r"(?:[ \t]+\w*docs\b)+(?:[ \t]+[A-Za-z_][\w]*=\S+|[ \t]+-\S+)*[ \t]*)$",
    re.MULTILINE,
)

_DOC_DISABLED_PREFIX = "# sysforge(docs off): "

# Mixed doc-build lines (`make all htmldocs`): a whole-line comment would also
# drop the real build, so instead we strip *only* the doc goals. Matches any
# make line with args; the callback decides whether a doc goal is present and
# whether a real goal survives. A bare `*docs` word is a doc goal; VAR=val and
# -flag tokens are never goals.
_KERNEL_DOC_MIXED_RE = re.compile(
    r"^(?P<indent>[ \t]*)make(?P<args>(?:[ \t]+\S+)+)[ \t]*$",
    re.MULTILINE,
)
_DOCS_GOAL_RE = re.compile(r"^\w*docs$")


def _neutralize_kernel_doc_build(patched_path) -> None:
    """Comment out standalone kernel doc-build make lines (``make htmldocs`` …).

    Editing ``pkgname=(...)`` stops the ``-docs`` subpackage from being packaged
    but not from being *built* — stock Arch ``linux`` runs ``make htmldocs`` in
    ``build()``. When docs are disabled we prefix any line whose make goals are
    exclusively ``*docs`` targets with a ``# sysforge(docs off):`` marker, so the
    Sphinx build never runs. Mixed lines (``make all htmldocs``) are deliberately
    not matched. Naturally idempotent: a commented line no longer starts with
    ``make``, so a re-run finds nothing to neutralize.
    """
    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    def _comment(m: "re.Match") -> str:
        return f"{m.group('indent')}{_DOC_DISABLED_PREFIX}{m.group('cmd').rstrip()}"

    def _strip_docs_goals(m: "re.Match") -> str:
        """Drop only the ``*docs`` goals from a mixed make line, keeping the real
        goals; leave exclusive-doc lines for the whole-line comment pass."""
        indent = m.group("indent")
        args = m.group("args").split()
        if not any(_DOCS_GOAL_RE.match(t) for t in args):
            return m.group(0)  # no doc goal — untouched
        other_goals = [
            t for t in args
            if not _DOCS_GOAL_RE.match(t) and "=" not in t and not t.startswith("-")
        ]
        if not other_goals:
            return m.group(0)  # exclusive-docs — handled by _KERNEL_DOC_MAKE_RE
        kept = [t for t in args if not _DOCS_GOAL_RE.match(t)]
        return f"{indent}make " + " ".join(kept)

    # Mixed lines first (strip the doc goal in place), then exclusive-doc lines
    # (comment the whole line). Order matters: the mixed pass leaves exclusive
    # lines untouched so the comment pass still catches them.
    new_text = _KERNEL_DOC_MIXED_RE.sub(_strip_docs_goals, text)
    new_text = _KERNEL_DOC_MAKE_RE.sub(_comment, new_text)
    if new_text != text:
        patched_path.write_text(new_text, encoding="utf-8")
        _log.info(
            "Disabled kernel documentation build (make *docs commented out) "
            "(docs disabled via kernel.toml/CLI)",
        )


def patch_kernel_subpackages(patched_path, *, headers: bool, docs: bool):
    """Drop the ``-headers`` and/or ``-docs`` subpackages from a kernel
    PKGBUILD's ``pkgname=(...)`` array.

    Standard Arch kernel PKGBUILDs list the image plus optional subpackages —
    ``pkgname=("$pkgbase" "$pkgbase-headers" "$pkgbase-docs")`` — and synthesize
    each ``package_$_p()`` via ``for _p in "${pkgname[@]}"; do eval …``. An entry
    absent from the array is therefore never packaged, so removing it is the
    cleanest way to *not build* a subpackage: the ``_package-headers()`` /
    ``_package-docs()`` helper bodies can stay defined and untouched.

    When ``headers`` is False, every array token whose dequoted value ends with
    ``-headers`` is dropped; likewise ``docs`` → ``-docs``. The suffix test (not
    a literal-name match) handles every token form — ``linux-custom-headers``,
    ``"$pkgbase-headers"``, ``${pkgbase}-headers`` all end the same way.

    Mirrors the sibling kernel patchers: edits ``patched_path``
    (``PKGBUILD.sysforge``) in place, leaves the operator's tracked PKGBUILD
    alone. No-op fast path when both subpackages are kept; a PKGBUILD lacking the
    targeted subpackage is a no-op; naturally idempotent (a re-run finds nothing
    left to drop). Both the single-line and one-token-per-line array layouts are
    preserved.
    """
    if headers and docs:
        return  # nothing to drop

    patched_path = Path(patched_path)

    # Stock Arch `linux` builds the HTML/PDF/man documentation inside build()
    # (`make htmldocs SPHINXOPTS=-QT`), not only in _package-docs(). Dropping the
    # -docs token from pkgname stops *packaging* the docs but not the (often
    # multi-minute Sphinx) *build* — so the resolution summary's "docs=off" would
    # be a lie. Comment out the standalone doc-target make line(s) too. Done
    # independently of the pkgname edit so a PKGBUILD that builds docs without a
    # -docs subpackage is still handled.
    if not docs:
        _neutralize_kernel_doc_build(patched_path)

    text = patched_path.read_text(encoding="utf-8")
    original = text

    drop_suffixes = []
    if not headers:
        drop_suffixes.append("-headers")
    if not docs:
        drop_suffixes.append("-docs")

    def _should_drop(token: str) -> bool:
        val = token.strip().strip('"').strip("'")
        return any(val.endswith(suffix) for suffix in drop_suffixes)

    def _match_close(s: str, open_idx: int):
        """Return the index of the ')' matching the '(' at ``open_idx``, or None."""
        depth = 0
        for j in range(open_idx, len(s)):
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
                if depth == 0:
                    return j
        return None

    # Rewrite every `pkgname=(...)` literal AND every `pkgname+=(...)` append —
    # modern Arch kernel PKGBUILDs add optional subpackages via
    # `pkgname+=("$pkgbase-docs")`, which a single-array walk misses, so `-docs`
    # survives and the subpackage is still built/installed (B7). A rewrite shifts
    # offsets, so re-scan the fresh text after each edit rather than trusting a
    # stale finditer cursor.
    changed = True
    while changed:
        changed = False
        for m in re.finditer(r"^pkgname(\+?=)\(", text, re.MULTILINE):
            is_append = m.group(1) == "+="
            open_idx = text.index("(", m.start())
            close_idx = _match_close(text, open_idx)
            if close_idx is None:
                continue  # unbalanced — leave this statement untouched
            inner = text[open_idx + 1:close_idx]
            tokens = re.findall(r"\S+", inner)
            kept = [t for t in tokens if not _should_drop(t)]
            if len(kept) == len(tokens):
                continue  # nothing to drop in this array (idempotent re-runs)

            if not kept and is_append:
                # The append existed solely to add now-dropped subpackage(s):
                # remove the whole `pkgname+=(...)` statement line rather than
                # leave an empty `pkgname+=()`.
                line_start = text.rfind("\n", 0, m.start()) + 1
                end = close_idx + 1
                if end < len(text) and text[end] == "\n":
                    end += 1
                text = text[:line_start] + text[end:]
            else:
                if "\n" in inner:
                    # One token per line: recover the array's indentation from the
                    # first indented line so the rewrite preserves the layout.
                    indent_match = re.search(r"\n([ \t]+)\S", inner)
                    indent = indent_match.group(1) if indent_match else "  "
                    new_inner = "\n" + "\n".join(indent + t for t in kept) + "\n"
                else:
                    new_inner = " ".join(kept)
                text = text[:open_idx + 1] + new_inner + text[close_idx:]
            changed = True
            break  # offsets shifted — restart the scan on the fresh text

    if text == original:
        return  # nothing matched anywhere — no-op (idempotent re-runs land here)

    patched_path.write_text(text, encoding="utf-8")
    dropped = [s.lstrip("-") for s in drop_suffixes]
    _log.info(
        f"Dropped kernel subpackage(s) from pkgname: {', '.join(dropped)} "
        "(disabled via kernel.toml/CLI)",
    )


# ---------------------------------------------------------------------------
# -sysforge package rename (optimization provenance)
# ---------------------------------------------------------------------------

# A pkgname/pkgbase token that *references* pkgbase (``$pkgbase`` /
# ``${pkgbase}``) cascades automatically once pkgbase is renamed, so the suffix
# logic must skip it — re-suffixing would double-apply.
_PKGBASE_REF_RE = re.compile(r"\$\{?pkgbase\}?")


def _dequote(token: str) -> tuple[str, str]:
    """Split a bash array token into ``(quote_char, value)``.

    ``"llvm"`` -> ``('"', 'llvm')``; ``llvm`` -> ``('', 'llvm')``.
    """
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return (token[0], token[1:-1])
    return ("", token)


def _rename_package_functions(text: str, origin_pkgnames: list[str], map_val) -> str:
    """Rename ``package_<name>()`` split-package functions per ``map_val``.

    makepkg's split-package contract requires a ``package_<pkgname>()`` function
    for every ``pkgname`` member, so renaming the ``pkgname`` tokens without the
    matching functions makes makepkg abort ("missing package function") deep into
    the build. For each renamed literal name, rewrite ``package_<orig>()`` →
    ``package_<mapped>()`` so the function name tracks the pkgname.

    Bare ``package()`` (single-package PKGBUILDs) is left untouched — makepkg uses
    it regardless of the renamed ``pkgname``. ``$pkgbase``-reference pkgname tokens
    have no literal function to rename (function names must be literal
    identifiers), so they are skipped here and validated downstream. Names are
    processed longest-first so a shorter name never partially matches a longer
    function's body; the trailing ``()`` anchor already prevents prefix collisions.
    """
    for origname in sorted(origin_pkgnames, key=len, reverse=True):
        _, val = _dequote(origname)
        if _PKGBASE_REF_RE.search(val):
            continue
        new = map_val(val)
        if new == val:
            continue
        text = re.sub(
            rf"(?m)^(\s*)package_{re.escape(val)}(\s*\(\s*\))",
            rf"\g<1>package_{new}\g<2>",
            text,
        )
    return text


def _rename_name_token(token: str, map_val) -> str:
    """Apply ``map_val`` to a literal pkgname/pkgbase token, preserving quotes.

    Pkgbase-reference tokens (``$pkgbase``…) are returned unchanged (renaming
    pkgbase cascades through them); so are tokens the mapping leaves alone
    (idempotency / not-embedding-the-old-base).
    """
    quote, val = _dequote(token)
    if _PKGBASE_REF_RE.search(val):
        return token
    new = map_val(val)
    if new == val:
        return token
    return f"{quote}{new}{quote}"


def _find_array_span(text: str, key: str) -> tuple[int, int] | None:
    """Return ``(open_paren_idx, close_paren_idx)`` for a ``key=(...)`` array.

    Walks paren depth so a multi-line array is captured whole. Returns None when
    the key is absent or the array is unbalanced (caller leaves it untouched).
    """
    m = re.search(rf"^{re.escape(key)}=\(", text, re.MULTILINE)
    if not m:
        return None
    open_idx = text.index("(", m.start())
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return (open_idx, i)
        i += 1
    return None


def _metadata_block_end(text: str, fallback: int) -> int:
    """Offset just past the last top-level ``pkgver=``/``pkgrel=``/``epoch=``
    assignment line, or ``fallback`` if none is found (or all precede it).

    Used to anchor a freshly-created ``provides`` array below ``pkgver=`` so its
    ``$pkgver`` references expand non-empty at bash assignment time. Indented
    (in-function) assignments are excluded by the leading ``^`` with no
    whitespace.
    """
    end = fallback
    for m in re.finditer(r"^(?:pkgver|pkgrel|epoch)=\S.*$", text, re.MULTILINE):
        end = max(end, m.end())
    return end


def _merge_or_create_array(text: str, key: str, values: list[str], anchor_end: int) -> str:
    """Append ``values`` to an existing ``key=(...)`` array, or create one.

    A second top-level ``key=(...)`` would clobber the first in bash (last
    assignment wins), so an existing array is *merged into* rather than
    duplicated. A newly-created array is inserted at ``anchor_end`` (the end of
    the pkgname line/array). Values already present (quoted or bare) are skipped.
    """
    span = _find_array_span(text, key)
    quoted = [f'"{v}"' for v in values]
    if span is None:
        line = f"\n{key}=(" + " ".join(quoted) + ")"
        return text[:anchor_end] + line + text[anchor_end:]
    open_idx, close_idx = span
    inner = text[open_idx + 1:close_idx]
    existing = set(re.findall(r"\S+", inner))
    add = [
        q for q, v in zip(quoted, values, strict=True)
        if q not in existing and v not in existing
    ]
    if not add:
        return text
    pad = "" if (not inner or inner.endswith((" ", "\n", "\t"))) else " "
    return text[:close_idx] + pad + " ".join(add) + text[close_idx:]


def _find_func_body_span(text: str, funcname: str) -> tuple[int, int] | None:
    """Return ``(open_brace_idx, close_brace_idx)`` for ``funcname() { ... }``.

    Walks brace depth so a body with nested ``${…}`` / blocks is captured whole.
    Returns None when the function is absent or its braces are unbalanced. The
    walk is naive about braces inside comments/strings — acceptable here (the same
    tradeoff as :func:`_find_array_span`'s paren walk) since package functions
    keep ``{}`` balanced.
    """
    m = re.search(rf"(?m)^[ \t]*{re.escape(funcname)}\s*\(\s*\)\s*\{{", text)
    if not m:
        return None
    open_idx = text.index("{", m.start())
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (open_idx, i)
        i += 1
    return None


def _find_indented_array_span(text: str, key: str) -> tuple[int, int] | None:
    """Like :func:`_find_array_span` but matches an *indented* ``key=(`` — i.e. an
    array assigned inside a ``package_<name>()`` function body."""
    m = re.search(rf"(?m)^[ \t]*{re.escape(key)}=\(", text)
    if not m:
        return None
    open_idx = text.index("(", m.start())
    depth = 0
    i = open_idx
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return (open_idx, i)
        i += 1
    return None


def _has_package_func(text: str, name: str) -> bool:
    return re.search(rf"(?m)^[ \t]*package_{re.escape(name)}\s*\(\s*\)", text) is not None


def _inject_member_conflict(text: str, member: str) -> str:
    """Ensure a split member's ``package_<member>()`` body declares
    ``provides``/``conflicts``/``replaces`` for its *own* stock name.

    The B1 root cause: a single top-level (global) injection is shadowed by any
    ``package_<name>()`` body that reassigns these arrays (bash: last assignment
    in the function wins), so the renamed member silently drops its drop-in
    conflict — and members that *don't* reassign inherit the over-broad global
    listing every sibling. Attribution must be per-member, inside the body, where
    it survives the body's own assignment. Merges into an existing in-body array,
    or creates one right after the opening brace (no later top-level assignment in
    the body can then shadow it). ``$pkgver`` is always set inside a package
    function, so the provides version expands correctly regardless of field order.
    Returns ``text`` unchanged if the member has no literal package function.
    """
    # replaces/conflicts carry the bare name; provides carries name=$pkgver.
    wanted = {
        "replaces": member,
        "conflicts": member,
        "provides": f"{member}=$pkgver",
    }
    for key, value in wanted.items():
        span = _find_func_body_span(text, f"package_{member}")
        if span is None:
            return text
        open_brace, close_brace = span
        body = text[open_brace + 1:close_brace]
        arr = _find_indented_array_span(body, key)
        quoted = f'"{value}"'
        if arr is not None:
            a_open, a_close = arr
            inner = body[a_open + 1:a_close]
            existing = set(re.findall(r"\S+", inner))
            if quoted in existing or value in existing:
                continue
            pad = "" if (not inner or inner.endswith((" ", "\n", "\t"))) else " "
            abs_close = open_brace + 1 + a_close
            text = text[:abs_close] + pad + quoted + text[abs_close:]
        else:
            insert_at = open_brace + 1
            text = text[:insert_at] + f"\n  {key}=({quoted})" + text[insert_at:]
    return text


def patch_package_suffix(patched_path, suffix: str, *, mode: str = "conflict") -> dict | None:
    """Rename a PKGBUILD's package(s) with a ``-suffix`` and (conflict mode) make
    them drop-in replacements for the stock names. The one home for the
    ``-sysforge`` optimization-provenance rename — a thin wrapper over
    :func:`patch_pkgbase_rename`'s shared machinery with an *append-suffix* token
    mapping (``llvm-libs`` → ``llvm-libs-sysforge``: every literal member gets the
    suffix, whether or not it embeds the pkgbase).

    Idempotent: a pkgbase already carrying ``-suffix`` is a no-op (this collapse
    is load-bearing — a local ``pkgname`` that already ends in ``-sysforge`` never
    gets a second suffix from an optimized build). Returns the rename-info dict
    (plus ``"suffix"``) or None if nothing was renamed.
    """
    def _map(val: str) -> str:
        if val.endswith(f"-{suffix}"):
            return val
        return f"{val}-{suffix}"

    info = _patch_rename(
        patched_path,
        mode=mode,
        map_val=_map,
        is_renamed=lambda base: bool(base) and base.endswith(f"-{suffix}"),
        label=f"-{suffix} suffix",
    )
    if info is not None:
        info["suffix"] = suffix
    return info


def patch_pkgbase_rename(patched_path, new_pkgbase: str, *, mode: str = "coexist") -> dict | None:
    """Rename a PKGBUILD's pkgbase to an arbitrary ``new_pkgbase``.

    The generalized rename primitive behind :func:`patch_package_suffix` (F40):
    the kernel stage uses it to patch a cloned upstream's pkgbase (e.g.
    ``linux-zen``) to the local ``pkgname`` (``linux-mine``) so the build
    coexists with the official package. Token mapping is *base replacement*:
    literal ``pkgname`` members embedding the old base are rewritten in place
    (``linux-zen-headers`` → ``linux-mine-headers``, boundary-safe so
    ``linux-zenith-docs`` survives); ``$pkgbase`` references cascade untouched;
    members not embedding the old base are left alone.

    Idempotent: pkgbase already equal to ``new_pkgbase`` is a no-op. Returns the
    rename-info dict (``origin_pkgbase``/``origin_pkgnames``/``renamed_*``/
    ``mode``) for ``build_state`` threading, or None if nothing was renamed.
    """
    patched_path = Path(patched_path)

    from sysforge.primitives import pkgbuild_meta
    globals_ = pkgbuild_meta.parse_pkgbuild(patched_path).get("globals", {})
    origin_pkgbase = globals_.get("pkgbase") or (
        (globals_.get("pkgname") or [None])[0]
        if isinstance(globals_.get("pkgname"), list)
        else globals_.get("pkgname")
    )
    if not origin_pkgbase:
        return None
    # Boundary-safe base replacement: the old base must not be followed (or
    # preceded) by an alphanumeric, so a member merely sharing a prefix
    # (linux-zenith…) is never corrupted. '-'/'.'/'_' are valid separators
    # inside package names and correctly count as boundaries.
    pat = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(origin_pkgbase)}(?![A-Za-z0-9])"
    )

    return _patch_rename(
        patched_path,
        mode=mode,
        map_val=lambda val: pat.sub(new_pkgbase, val),
        is_renamed=lambda base: base == new_pkgbase,
        label=f"pkgbase rename → {new_pkgbase}",
    )


def _patch_rename(patched_path, *, mode: str, map_val, is_renamed, label: str) -> dict | None:
    """Shared rename machinery for :func:`patch_package_suffix` and
    :func:`patch_pkgbase_rename` — the single home for the PKGBUILD rename.

    Two modes, because the runtime semantics differ by target:

    * ``mode="conflict"`` (llvm, mesa): the renamed build is a *mutually
      exclusive* drop-in — it must not coexist with the stock package (same
      file paths → runtime confusion). Injects ``provides=(orig=$pkgver …)`` +
      ``conflicts=(orig …)`` + ``replaces=(orig …)`` covering every original
      pkgname so pacman cleanly swaps the stock package out.
    * ``mode="coexist"`` (kernel): the renamed build installs *alongside* the
      stock one (version-suffixed files, bootloader fallback). Rename only — no
      conflicts/replaces.

    Renames the ``pkgbase=`` string and every *literal* token of the
    ``pkgname=(...)`` array (or a single ``pkgname=`` string) via ``map_val``.
    ``$pkgbase`` references are left alone — renaming pkgbase cascades through
    them. Edits ``patched_path`` (PKGBUILD.sysforge) in place; ``is_renamed``
    supplies the idempotency check on the parsed origin pkgbase. Returns
    ``{"origin_pkgbase", "origin_pkgnames", "renamed_*", "mode"}`` for the
    caller to thread into ``build_state`` (origin_pkgbase) and the rename-aware
    validator, or None if there was nothing to rename.

    .. note:: Top-level ``provides``/``conflicts``/``replaces`` are package-level
       defaults; a split subpackage that *reassigns* them in its
       ``package_<name>()`` body overrides the injected global. Wiring a specific
       split package (llvm/mesa) must verify the subpackage functions against
       this — the primitive supplies the mechanism and the static guarantee
       (validated below); per-subpackage overrides are a wiring concern.
    """
    if mode not in ("conflict", "coexist"):
        raise ValueError(f"unknown rename mode {mode!r}; expected 'conflict' or 'coexist'")

    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")

    from sysforge.primitives import pkgbuild_meta
    globals_ = pkgbuild_meta.parse_pkgbuild(patched_path).get("globals", {})
    origin_pkgbase = globals_.get("pkgbase")
    origin_pkgnames = globals_.get("pkgname")
    if isinstance(origin_pkgnames, str):
        origin_pkgnames = [origin_pkgnames]
    origin_pkgnames = list(origin_pkgnames or [])
    if not origin_pkgbase and origin_pkgnames:
        origin_pkgbase = origin_pkgnames[0]

    # Idempotency: a re-run would read the already-renamed names as "originals"
    # and double-inject conflicts. If the rename already landed (per the
    # caller's is_renamed check on the parsed pkgbase), there is nothing to do.
    if origin_pkgbase and is_renamed(origin_pkgbase):
        return None

    # --- rename pkgbase= (string field) ---
    def _sub_pkgbase(m: re.Match) -> str:
        return m.group(1) + _rename_name_token(m.group(2), map_val)

    text = re.sub(
        r"^(pkgbase=)(\S+)", _sub_pkgbase, text, count=1, flags=re.MULTILINE
    )

    # --- rename pkgname tokens (array or single string) ---
    anchor_end: int | None = None
    span = _find_array_span(text, "pkgname")
    if span is not None:
        open_idx, close_idx = span
        inner = text[open_idx + 1:close_idx]
        # Preserve token layout (whitespace runs) by renaming in place.
        def _sub_token(m: re.Match) -> str:
            return _rename_name_token(m.group(0), map_val)
        new_inner = re.sub(r"\S+", _sub_token, inner)
        text = text[:open_idx + 1] + new_inner + text[close_idx:]
        anchor_end = open_idx + 1 + len(new_inner) + 1  # past the ')'
    else:
        m = re.search(r"^(pkgname=)(\S+)", text, re.MULTILINE)
        if m:
            renamed = _rename_name_token(m.group(2), map_val)
            text = text[:m.start()] + m.group(1) + renamed + text[m.end():]
            anchor_end = m.start() + len(m.group(1)) + len(renamed)

    if anchor_end is None:
        return None  # no pkgname to rename

    # --- conflict mode: provides/conflicts/replaces over the stock names ---
    if mode == "conflict" and origin_pkgnames:
        # Attribution must be *per member*: a split ``package_<name>()`` body that
        # reassigns provides/conflicts/replaces shadows any global array (bash:
        # last assignment in the function wins). Members that own a literal
        # package function get their stock name injected *inside* that body (B1);
        # the rest (a single bare ``package()``, or ``$pkgbase``-cascaded members
        # with no literal function) are covered by the global injection, which is
        # the effective array for them.
        with_func, without_func = [], []
        for n in origin_pkgnames:
            _, val = _dequote(n)
            if not _PKGBASE_REF_RE.search(val) and _has_package_func(text, val):
                with_func.append(val)
            else:
                without_func.append(n)
        if without_func:
            text = _merge_or_create_array(
                text, "replaces", list(without_func), anchor_end
            )
            text = _merge_or_create_array(
                text, "conflicts", list(without_func), anchor_end
            )
            # provides entries reference ``$pkgver``, which bash expands at array
            # *assignment* time. PKGBUILDs that declare pkgname before pkgver
            # (mesa, llvm) would otherwise get the created array inserted above
            # ``pkgver=``, leaving every entry as ``name=`` (makepkg: "pkgver in
            # provides is not allowed to be empty"). Anchor a freshly-created
            # array below the pkgver assignment instead. Recomputed on the
            # current text so the replaces/conflicts insertions don't stale it.
            provides_anchor = _metadata_block_end(text, anchor_end)
            text = _merge_or_create_array(
                text, "provides", [f"{n}=$pkgver" for n in without_func], provides_anchor
            )
        for val in with_func:
            text = _inject_member_conflict(text, val)

    # Rename split-package functions to match the renamed pkgnames (both modes —
    # the kernel coexist rename needs it too). Must run for split packages or
    # makepkg can't find the packaging function for a renamed member.
    text = _rename_package_functions(text, origin_pkgnames, map_val)

    patched_path.write_text(text, encoding="utf-8")
    _log.info(
        f"Renamed package(s) ({label}, {mode} mode): "
        f"{', '.join(origin_pkgnames)}",
    )
    # The renamed names are what landed on disk (and so what build_state must
    # record); ``origin_*`` is what ``sysforge update`` correlates back to the
    # upstream source tree. Computed via the same ``map_val`` the rewrite used
    # so split-package / lib32 prefixing stays consistent.
    return {
        "origin_pkgbase": origin_pkgbase,
        "origin_pkgnames": origin_pkgnames,
        "renamed_pkgbase": _rename_name_token(origin_pkgbase, map_val)
        if origin_pkgbase else None,
        "renamed_pkgnames": [_rename_name_token(n, map_val) for n in origin_pkgnames],
        "mode": mode,
    }


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
# Build-linker token rewrite  (-fuse-ld=X  →  -fuse-ld=target_linker)
# ---------------------------------------------------------------------------

# Matches a bare `-fuse-ld=X` LDFLAGS token (X = linker name; stops at
# whitespace, quotes, or other shell delimiters).
_FUSE_LD_RE = re.compile(r"-fuse-ld=([\w.-]+)")
# Matches the RUSTFLAGS `-C link-arg=-fuse-ld=X` (two-token) and
# `-Clink-arg=-fuse-ld=X` (one-token) forms, for the strip path.
_RUST_FUSE_LD_TWO_RE = re.compile(r"-C\s+link-arg=-fuse-ld=[\w.-]+")
_RUST_FUSE_LD_ONE_RE = re.compile(r"-Clink-arg=-fuse-ld=[\w.-]+")


def patch_build_linker(patched_path, target_linker):
    """Rewrite ``-fuse-ld=X`` tokens in build-time flag appends to
    ``target_linker``, in place.

    Handles both the LDFLAGS form (``-fuse-ld=X``) and the RUSTFLAGS forms
    (``-C link-arg=-fuse-ld=X`` / ``-Clink-arg=-fuse-ld=X``) with a single
    in-place ``-fuse-ld=`` token sub, so indentation, surrounding quotes, and
    newlines are preserved. When ``target_linker`` is ``"ld"`` (the system
    default), the token is **stripped** rather than written as an invalid
    ``-fuse-ld=ld``; the now-empty ``-C link-arg=`` wrapper is removed too.

    Returns ``{"old", "new", "count"}`` (``old`` = the first linker replaced) or
    ``None`` if nothing changed.
    """

    patched_path = Path(patched_path)
    text = patched_path.read_text()
    out_lines = []
    count = 0
    first_old = None

    for line in text.splitlines(keepends=True):
        if "-fuse-ld=" not in line:
            out_lines.append(line)
            continue

        if first_old is None:
            m = _FUSE_LD_RE.search(line)
            if m:
                first_old = m.group(1)

        if target_linker == "ld":
            # Strip the whole token, including the rust link-arg wrapper.
            new_line = _RUST_FUSE_LD_TWO_RE.sub("", line)
            new_line = _RUST_FUSE_LD_ONE_RE.sub("", new_line)
            new_line = _FUSE_LD_RE.sub("", new_line)
        else:
            # _FUSE_LD_RE matches the `-fuse-ld=X` token inside any wrapper
            # (bare LDFLAGS, `-C link-arg=…`, or `-Clink-arg=…`), so a single
            # in-place regex sub rewrites the linker name while preserving the
            # line's indentation, surrounding quotes, and newline. Splitting the
            # whole line through _replace_rustflags_linker dropped the closing
            # quote and collapsed whitespace, emitting unbalanced bash (1.2.0-B3).
            new_line = _FUSE_LD_RE.sub(f"-fuse-ld={target_linker}", line)

        if new_line != line:
            count += line.count("-fuse-ld=")
        out_lines.append(new_line)

    if count == 0:
        return None

    patched_path.write_text("".join(out_lines))
    _log.info(
        f"Rewrote {count} hardcoded -fuse-ld= token(s) "
        f"({first_old} -> {target_linker}) in PKGBUILD build body "
        f"(effective linker is {target_linker})"
    )
    return {"old": first_old, "new": target_linker, "count": count}


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

# A cmake *command* invocation: leading indent, the `cmake` word, then
# horizontal whitespace and at least one same-line argument. We anchor `-D…`
# injections on this.
#
# `[ \t]+` (not `\s+`) is load-bearing: `\s` spans newlines, so the old bare
# `^([ \t]*)cmake\b` form matched a lone `cmake` element inside a *multi-line*
# `makedepends=(...)` array (e.g. spirv-llvm-translator) — the injector then
# spliced `-DLLVM_DIR=…` into the dependency array (makepkg exit 12). Requiring a
# same-line argument means a bare dep-array `cmake` token is correctly skipped,
# while real invocations (`cmake ..`, `cmake -S … -B build`) still match.
# (`cmake_options=(` was already safe — `\b` / the required space both stop at `_`.)
_CMAKE_COMMAND_RE = re.compile(r"^([ \t]*)cmake[ \t]+(?P<rest>\S.*)$", re.MULTILINE)

# cmake action-mode invocations that ignore -D cache args (build / install /
# script / query). A `-D…` injection must anchor on the *configure* call, not
# `cmake --build build`, so these are skipped when picking the anchor.
_CMAKE_ACTION_MODE = ("--build", "--install", "--open", "--version", "-E", "-P")

# Matches `-DLLVM_DIR=...` (optional :PATH/:STRING type tag and optional
# surrounding quotes). Anchored on the full `LLVM_DIR=` token so it never
# matches `-DLLVM_DISTRIBUTION_COMPONENTS=` and friends.
_LLVM_DIR_RE = re.compile(
    r'-DLLVM_DIR(?::[A-Z]+)?=(?:"[^"]*"|\'[^\']*\'|\S+)'
)

# --- mesa meson driver options ----------------------------------------------
#
# Mesa is a meson build: drivers are array elements of `meson_options=( … )`,
# written `-D gallium-drivers=all` / `-D vulkan-drivers=amd,intel,…` (the `-D`
# and key may or may not have a space between them). Each regex captures the
# key-prefix (group 1) so a rewrite preserves the original `-D `/`-D` spacing,
# and matches the unquoted comma-list value to end of element. This is the
# meson analogue of _LLVM_TARGETS_RE — sysforge has no other meson injector.
_MESA_GALLIUM_RE = re.compile(
    r'(-D[ \t]?gallium-drivers=)(?:"[^"]*"|\'[^\']*\'|\S+)'
)
_MESA_VULKAN_RE = re.compile(
    r'(-D[ \t]?vulkan-drivers=)(?:"[^"]*"|\'[^\']*\'|\S+)'
)
# rusticl drivers must be a SUBSET of the built gallium drivers, else meson
# configure aborts — so a gallium reduction must intersect this too.
_MESA_RUSTICL_RE = re.compile(
    r'(-D[ \t]?gallium-rusticl-enable-drivers=)(?:"[^"]*"|\'[^\']*\'|\S+)'
)

# Valid meson driver tokens (the option enums) — used to reject a typo'd
# explicit `[mesa]` override before it reaches `arch-meson` and aborts the build
# hours in. Kept deliberately broad (every upstream driver); the point is to
# catch garbage, not to police which drivers a host may request.
_MESA_GALLIUM_TOKENS = frozenset({
    "asahi", "crocus", "d3d12", "etnaviv", "freedreno", "i915", "iris", "lima",
    "llvmpipe", "nouveau", "panfrost", "r300", "r600", "radeonsi", "softpipe",
    "svga", "tegra", "v3d", "vc4", "virgl", "zink",
})
_MESA_VULKAN_TOKENS = frozenset({
    "amd", "asahi", "broadcom", "freedreno", "gfxstream", "imagination",
    "intel", "intel_hasvk", "microsoft-experimental", "nouveau", "panfrost",
    "swrast", "virtio",
})


def is_llvm_pkgbase(pkgbase: str | None) -> bool:
    """Return True if pkgbase looks like an LLVM-toolchain package."""
    if not pkgbase:
        return False
    return any(pkgbase == p or pkgbase.startswith(p + "-")
               for p in _LLVM_PKGBASE_PATTERNS)


def _line_is_continued(line: str) -> bool:
    """True if ``line`` ends in a bash line-continuation (an *odd* run of ``\\``).

    ``foo \\`` continues; ``foo \\\\`` (an escaped backslash) does not. Counting
    trailing backslashes and testing parity is the correct test.
    """
    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1


def _cmake_statement_end(text: str, search_from: int) -> int:
    """Index of the newline that terminates the (possibly ``\\``-continued) cmake
    statement beginning at/after ``search_from``.

    A cmake invocation may already span several lines via ``\\`` continuations —
    either in the upstream PKGBUILD or because an earlier injection appended an
    arg. Inserting a new ``-D`` arg at the *first* newline after ``cmake`` would
    splice it into the middle of that chain and orphan everything below it (bash
    then runs the orphaned ``-D…`` as a command → "command not found"). So we walk
    forward over continued lines and return the first newline whose line does
    **not** continue — the true end of the statement, where a new continuation
    appends cleanly. Returns ``len(text)`` when the statement runs to EOF.
    """
    pos = search_from
    while True:
        nl = text.find("\n", pos)
        if nl == -1:
            return len(text)
        if _line_is_continued(text[pos:nl]):
            pos = nl + 1
            continue
        return nl


def _find_cmake_configure_anchor(text: str):
    """Return the regex match for the cmake *configure* invocation to anchor a
    ``-D…`` injection on, or ``None`` if the PKGBUILD has no such command.

    Only matches ``cmake`` used as a command with same-line arguments — so a bare
    ``cmake`` element inside a multi-line ``makedepends=(...)`` array is never
    chosen (the spirv-llvm-translator exit-12 brick) — and skips cmake
    action-mode invocations (``cmake --build`` / ``--install`` / ``-E`` …) which
    ignore the ``-D`` cache args. Returns the first surviving match: the
    configure call.

    The match's ``group(1)`` is the indent. Callers must pass ``match.start()``
    (not ``.end()``) to :func:`_cmake_statement_end` so that a trailing ``\\`` on
    the cmake line *itself* is seen as a continuation.
    """
    for m in _CMAKE_COMMAND_RE.finditer(text):
        rest = m.group("rest").lstrip()
        if any(rest == flag or rest.startswith(flag + " ")
               for flag in _CMAKE_ACTION_MODE):
            continue
        return m
    return None


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

    # (2) Insert after the cmake *configure* invocation line. We append the new
    # arg as a continuation: indentation matched, trailing backslash so
    # the next line stays part of the same shell statement.
    cmake_match = _find_cmake_configure_anchor(text)
    if not cmake_match:
        _log.warn("LLVM target filtering requested but no cmake invocation "
                  "found in PKGBUILD — leaving unmodified")
        return False

    indent = cmake_match.group(1)
    # Append at the *true* end of the (possibly already \-continued) cmake
    # statement — not the first newline — so a second injection composes with the
    # first instead of splitting its continuation and orphaning an arg. Scan from
    # the start of the cmake line so a trailing \ on that line is itself seen.
    line_end = _cmake_statement_end(text, cmake_match.start())
    insertion = f" \\\n{indent}    {replacement}"
    new_text = text[:line_end] + insertion + text[line_end:]
    patched_path.write_text(new_text)
    _log.info(f"Injected {replacement} after cmake invocation")
    return True


def patch_llvm_dir(patched_path, llvm_dir: str) -> bool:
    """Inject or replace ``-DLLVM_DIR="<dir>"`` in the cmake invocation of an
    already-written PKGBUILD.sysforge.

    Used by the toolchain stage's staged PGO passes (1b/3b/3c) to force
    ``find_package(LLVM CONFIG)`` at the staged libLLVM prefix
    (``<staging>/usr/lib/cmake/llvm``) instead of the live ``/usr`` one.
    ``LLVM_DIR`` is the highest-precedence config-mode override — checked before
    any ``CMAKE_PREFIX_PATH`` / system search — so it bypasses the env-var search
    that otherwise silently resolves /usr and makes clang/lld link the wrong
    libLLVM (the Gate-3 ``_ZNSt*@LLVM_*`` brick). As a cmake **cache** variable
    it persists in CMakeCache.txt across the PKGBUILD's repeated ``cmake ..``
    configure calls, so injecting after the first invocation is sufficient
    (mirrors :func:`patch_llvm_targets`).

    Idempotent: re-running with the same dir is a no-op. On a no-cmake-found
    PKGBUILD, logs a warning and returns False. Returns True when modified.
    """
    if not llvm_dir:
        return False
    replacement = f'-DLLVM_DIR="{llvm_dir}"'

    patched_path = Path(patched_path)
    text = patched_path.read_text()

    # (1) Already present? — replace if value differs, no-op if same.
    existing = _LLVM_DIR_RE.search(text)
    if existing:
        if existing.group(0) == replacement:
            return False
        new_text = _LLVM_DIR_RE.sub(replacement, text, count=1)
        patched_path.write_text(new_text)
        _log.info(f"Replaced LLVM_DIR: {existing.group(0)!r} → {replacement!r}")
        return True

    # (2) Insert after the cmake *configure* invocation line (continuation form).
    cmake_match = _find_cmake_configure_anchor(text)
    if not cmake_match:
        _log.warn("LLVM_DIR steering requested but no cmake invocation found "
                  "in PKGBUILD — leaving unmodified")
        return False

    indent = cmake_match.group(1)
    # Append at the *true* end of the (possibly already \-continued) cmake
    # statement — not the first newline — so a second injection composes with the
    # first instead of splitting its continuation and orphaning an arg. Scan from
    # the start of the cmake line so a trailing \ on that line is itself seen.
    line_end = _cmake_statement_end(text, cmake_match.start())
    insertion = f" \\\n{indent}    {replacement}"
    new_text = text[:line_end] + insertion + text[line_end:]
    patched_path.write_text(new_text)
    _log.info(f"Injected {replacement} after cmake invocation")
    return True


def _validate_mesa_tokens(drivers: list[str], allowed: frozenset[str], axis: str) -> bool:
    """True if every token in ``drivers`` is a recognised meson ``axis`` driver.

    A bad token (typically a typo in an explicit ``[mesa]`` override) makes
    ``arch-meson`` abort the configure step hours before compile. The caller
    treats False as "skip the rewrite, leave upstream's driver list untouched"
    so a fat-fingered override degrades to a full build, never a hard failure.
    """
    unknown = [d for d in drivers if d not in allowed]
    if unknown:
        _log.warn(
            f"mesa {axis} driver filtering requested with unrecognised "
            f"token(s) {', '.join(unknown)} — skipping the {axis} rewrite "
            "(building upstream's full driver set instead)"
        )
        return False
    return True


def patch_mesa_drivers(
    patched_path,
    gallium: list[str] | None,
    vulkan: list[str] | None,
) -> bool:
    """Rewrite mesa's ``-D gallium-drivers=`` / ``-D vulkan-drivers=`` meson
    options to the resolved per-host driver lists, in an already-written
    PKGBUILD.sysforge.

    The meson counterpart of :func:`patch_llvm_targets`. Differences forced by
    mesa being meson, not cmake:

    * Drivers are *array elements* of ``meson_options=( … )``, not args on a
      ``cmake`` configure line — so this rewrites the value in place (matched by
      :data:`_MESA_GALLIUM_RE` / :data:`_MESA_VULKAN_RE`) rather than appending a
      ``\\``-continuation. No anchor/statement-end logic is needed.
    * A gallium reduction also rewrites ``gallium-rusticl-enable-drivers`` to the
      intersection with the new gallium set (rusticl drivers must be a subset of
      built gallium drivers), falling back to meson's own ``auto`` when the
      intersection is empty — so ``gallium-rusticl=true`` stays satisfiable.
      ``auto`` (not a concrete driver like ``llvmpipe``, which is a gallium
      driver but is **not** rusticl-capable and is rejected by meson) resolves
      to the rusticl-capable subset of the built drivers, version-independently.

    Each axis is independent: pass ``None`` to leave that option untouched. A
    token that isn't a recognised meson driver (:func:`_validate_mesa_tokens`)
    skips that axis's rewrite rather than injecting a value that aborts the
    build. Idempotent — returns False (no write) when nothing changed.

    Returns True when the file was modified.
    """
    if not gallium and not vulkan:
        return False

    patched_path = Path(patched_path)
    text = patched_path.read_text(encoding="utf-8")
    new_text = text
    modified = False

    if gallium and _validate_mesa_tokens(gallium, _MESA_GALLIUM_TOKENS, "gallium"):
        new_text, changed = _rewrite_mesa_option(
            new_text, _MESA_GALLIUM_RE, gallium, "gallium-drivers"
        )
        modified = modified or changed
        # rusticl drivers ⊆ gallium drivers — keep them consistent with the
        # reduced set so meson doesn't reject an enabled-but-unbuilt driver.
        new_text, r_changed = _rewrite_mesa_rusticl(new_text, gallium)
        modified = modified or r_changed

    if vulkan and _validate_mesa_tokens(vulkan, _MESA_VULKAN_TOKENS, "vulkan"):
        new_text, changed = _rewrite_mesa_option(
            new_text, _MESA_VULKAN_RE, vulkan, "vulkan-drivers"
        )
        modified = modified or changed

    # A reduced driver set means some per-driver libs are never built. mesa's
    # package_*() functions relocate every upstream driver unconditionally
    # (`_pick … libvulkan_<drv>.so`, then `mv <tag>/* "$pkgdir"` in the split
    # package), so packaging aborts on the missing artifacts. Make those steps
    # self-healing rather than mapping driver→pkgname (fragile, version-drifts).
    if modified:
        new_text = _harden_mesa_packaging(new_text)
        patched_path.write_text(new_text, encoding="utf-8")
    return modified


# A `_pick` relocation whose source file wasn't built must be skipped, not
# `mv`'d (which aborts package_mesa()). Inserted right after the loop header.
_MESA_PICK_LOOP_RE = re.compile(r"^([ \t]*)for f; do[ \t]*$", re.MULTILINE)
_MESA_PICK_GUARD = '[ -e "$f" ] || continue'

# A split package stages its files under `<tag>/` via `_pick`; an unbuilt
# driver leaves that dir empty/absent, so the bare `mv <tag>/* "$pkgdir"` fails
# on an unmatched literal glob. `compgen -G` makes it a no-op when empty.
_MESA_SPLIT_MV_RE = re.compile(
    r'^([ \t]*)mv (?P<tag>[A-Za-z0-9_]+)/\* "\$pkgdir"[ \t]*$', re.MULTILINE
)


def _harden_mesa_packaging(text: str) -> str:
    """Make mesa's split-package relocation tolerant of drivers that a reduced
    ``-D *-drivers=`` set never built. Idempotent; a no-op on a PKGBUILD that
    has no ``_pick`` loop / split ``mv`` (upstream restructure)."""
    if _MESA_PICK_GUARD not in text:
        def _guard_pick(m: re.Match) -> str:
            return f"{m.group(0)}\n{m.group(1)}  {_MESA_PICK_GUARD}"

        text = _MESA_PICK_LOOP_RE.sub(_guard_pick, text, count=1)

    def _guard_mv(m: re.Match) -> str:
        indent, tag = m.group(1), m.group("tag")
        return (
            f'{indent}if compgen -G "{tag}/*" > /dev/null; then '
            f'mv {tag}/* "$pkgdir"; fi'
        )

    return _MESA_SPLIT_MV_RE.sub(_guard_mv, text)


def _rewrite_mesa_option(text, regex, drivers, label):
    """Replace the value of a single ``-D <opt>=`` meson element with the
    comma-joined ``drivers`` list. Returns ``(new_text, changed)``.

    Idempotent: a no-op (``changed=False``) when the option already carries the
    target value. Logs a warning and leaves the text unchanged when the option
    isn't present (upstream may have renamed/dropped it)."""
    existing = regex.search(text)
    if not existing:
        _log.warn(
            f"mesa driver filtering requested but no `-D {label}=` meson "
            "option found in PKGBUILD — leaving unmodified"
        )
        return (text, False)
    value = ",".join(drivers)
    replacement = existing.group(1) + value
    if existing.group(0) == replacement:
        return (text, False)
    new_text = text[:existing.start()] + replacement + text[existing.end():]
    _log.info(f"Set mesa {label}: {existing.group(0)!r} → {replacement!r}")
    return (new_text, True)


def _rewrite_mesa_rusticl(text, gallium):
    """Intersect ``gallium-rusticl-enable-drivers`` with the new ``gallium`` set
    (fallback ``auto`` when empty). Returns ``(new_text, changed)``; a no-op
    when the option is absent or already consistent.

    The fallback is meson's own ``auto`` — NOT a concrete driver. ``auto``
    enables rusticl on exactly the rusticl-capable subset of the *built* gallium
    drivers, so it is always a valid, subset-safe value regardless of mesa
    version. (The previous ``llvmpipe`` fallback bricked the build: llvmpipe is
    a gallium driver but is **not** rusticl-capable, so meson rejects it —
    "not in allowed choices: auto, asahi, freedreno, radeonsi".)"""
    existing = _MESA_RUSTICL_RE.search(text)
    if not existing:
        return (text, False)
    current_val = existing.group(0)[len(existing.group(1)):].strip("\"'")
    current = [d for d in current_val.split(",") if d]
    gallium_set = set(gallium)
    kept = [d for d in current if d in gallium_set] or ["auto"]
    replacement = existing.group(1) + ",".join(kept)
    if existing.group(0) == replacement:
        return (text, False)
    new_text = text[:existing.start()] + replacement + text[existing.end():]
    _log.info(
        f"Reduced mesa gallium-rusticl-enable-drivers: "
        f"{existing.group(0)!r} → {replacement!r}"
    )
    return (new_text, True)


# ---------------------------------------------------------------------------
# Post-patch validation gate
#
# Both recent multi-hour PGO bricks were *patched-PKGBUILD* defects that only
# surfaced deep into a makepkg build: a `-D…` arg spliced into a `makedepends=()`
# array (spirv-llvm-translator, makepkg exit 12) and a `-D…` arg orphaned as its
# own command (clang composition, build() exit 4). validate_patched_pkgbuild runs
# in milliseconds after patching and turns both classes into an up-front abort.
# ---------------------------------------------------------------------------

class PkgbuildPatchError(Exception):
    """A patched PKGBUILD failed post-patch validation — applying it would
    corrupt the build (raised *before* makepkg runs, so nothing is built)."""


# Globals that sysforge patching must never alter. `groups` is intentionally
# excluded: patch_pkgbuild_groups rewrites it by design.
_INVARIANT_GLOBALS = (
    "pkgname", "pkgbase", "pkgver", "pkgrel", "epoch",
    "depends", "makedepends", "checkdepends", "optdepends",
    "provides", "conflicts", "replaces",
)

# cmake-arg tokens sysforge injects. Each must end up attached to a cmake
# command; checking only these (never injected into arrays) means legitimate
# `-D …` *array elements* like `cmake_options=(-D FOO=ON)` are not flagged.
_MANAGED_CMAKE_TOKENS = ("-DLLVM_TARGETS_TO_BUILD=", "-DLLVM_DIR=")

# Identity globals that a ``-sysforge`` rename is *allowed* to mutate (and which
# ``_validate_rename`` then checks). Every other ``_INVARIANT_GLOBALS`` key stays
# strictly invariant even under a rename.
_RENAME_MUTABLE_GLOBALS = frozenset(
    {"pkgname", "pkgbase", "provides", "conflicts", "replaces"}
)


def _array_from_body(body: str | None, key: str) -> set[str] | None:
    """Bare-name set of an array assigned inside a ``package_<name>()`` body, or
    None when the body does not reassign it (caller falls back to the global).

    Returns an *empty set* when the body assigns an empty array — distinct from
    None (not assigned) so the validator can tell "overridden to nothing" from
    "inherits global".
    """
    if not body:
        return None
    span = _find_indented_array_span(body, key)
    if span is None:
        return None
    open_idx, close_idx = span
    inner = body[open_idx + 1:close_idx]
    return {_dequote(tok)[1] for tok in re.findall(r"\S+", inner)}


def _validate_rename(orig: dict, patched: dict, rename: dict,
                     patched_functions: dict | None = None) -> None:
    """Assert a :func:`patch_package_suffix` rename is well-formed.

    Verifies the suffix landed on pkgbase and every pkgname, and — in conflict
    mode — that the *effective* ``provides``/``conflicts``/``replaces`` for every
    member cover its stock name so no dependency edge is silently dropped and the
    renamed build can never install *next to* the stock one. "Effective" means a
    split ``package_<name>()`` body that reassigns an array overrides the global
    (the B1 mis-attribution the global-only check missed). Raises
    :class:`PkgbuildPatchError`.
    """
    suffix = rename.get("suffix")
    mode = rename["mode"]

    def _as_list(v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else list(v)

    patched_names = _as_list(patched.get("pkgname"))
    orig_base = orig.get("pkgbase")
    patched_base = patched.get("pkgbase")

    if suffix is not None:
        sfx = f"-{suffix}"
        # Every renamed name must *carry* the suffix. A substring (not endswith)
        # test is deliberate: a ``$pkgbase``-cascaded subpackage like
        # ``$pkgbase-headers`` becomes ``linux-custom-sysforge-headers`` — the
        # suffix lands mid-name, which is still a valid, collision-free rename.
        for n in patched_names:
            if sfx not in n:
                msg = (
                    f"rename produced pkgname {n!r} which does not carry the "
                    f"{sfx!r} suffix — it could still collide with a stock name"
                )
                _log.error(msg)
                raise PkgbuildPatchError(msg)

        if orig_base and (not patched_base or sfx not in patched_base):
            msg = (
                f"rename left pkgbase as {patched_base!r}; expected it to carry "
                f"the {sfx!r} suffix"
            )
            _log.error(msg)
            raise PkgbuildPatchError(msg)
    else:
        # Arbitrary pkgbase rename (patch_pkgbase_rename): the landed names must
        # match exactly what the rename computed — pkgbase first (its cascade
        # covers ``$pkgbase`` members), then each literal member.
        expected_base = rename.get("renamed_pkgbase")
        if expected_base and patched_base != expected_base:
            msg = (
                f"pkgbase rename left pkgbase as {patched_base!r}; expected "
                f"{expected_base!r}"
            )
            _log.error(msg)
            raise PkgbuildPatchError(msg)
        expected_names = set(_as_list(rename.get("renamed_pkgnames")))
        if expected_names and set(patched_names) != expected_names:
            msg = (
                f"pkgbase rename produced pkgname set {sorted(patched_names)!r}; "
                f"expected {sorted(expected_names)!r}"
            )
            _log.error(msg)
            raise PkgbuildPatchError(msg)

    if mode != "conflict":
        return

    # Global arrays — the effective values for any member that does not reassign
    # them in its own ``package_<name>()`` body.
    g_provided = {p.split("=", 1)[0] for p in _as_list(patched.get("provides"))}
    g_conflicts = set(_as_list(patched.get("conflicts")))
    g_replaces = set(_as_list(patched.get("replaces")))
    patched_functions = patched_functions or {}

    # Pair each stock name with the renamed member so we can read its body.
    orig_names = _as_list(rename.get("origin_pkgnames"))
    renamed = _as_list(rename.get("renamed_pkgnames"))
    if not orig_names:
        # Synthetic/legacy caller without per-member info — fall back to the
        # global arrays as the effective values for every original name.
        orig_names = _as_list(orig.get("pkgname")) or _as_list(orig.get("pkgbase"))
        renamed = [None] * len(orig_names)

    for stock, new in zip(orig_names, renamed, strict=True):
        _, stock_val = _dequote(stock)
        body = patched_functions.get(f"package_{new}") if new else None
        # Body reassignment (if any) overrides the global; else inherit global.
        b_provides = _array_from_body(body, "provides")
        provided = ({p.split("=", 1)[0] for p in b_provides}
                    if b_provides is not None else g_provided)
        conflicts = _array_from_body(body, "conflicts")
        conflicts = conflicts if conflicts is not None else g_conflicts
        replaces = _array_from_body(body, "replaces")
        replaces = replaces if replaces is not None else g_replaces
        for label, have in (("provides", provided), ("conflicts", conflicts),
                            ("replaces", replaces)):
            if stock_val not in have:
                msg = (
                    f"conflict-mode rename does not {label} the stock name "
                    f"{stock_val!r} — the renamed build could install beside, or "
                    f"silently drop a dependency edge to, the stock package"
                )
                _log.error(msg)
                raise PkgbuildPatchError(msg)


def validate_patched_pkgbuild(original_path, patched_path, *, rename=None) -> None:
    """Fast, build-free structural validation of a fully-patched PKGBUILD.sysforge.

    Raises :class:`PkgbuildPatchError` if a patch corrupted the file in a way that
    would otherwise only surface hours into a makepkg build. Two checks:

    **G1 — dependency/identity arrays are invariant.** Re-parses both files via
    :func:`pkgbuild_meta.parse_pkgbuild` and requires every key in
    ``_INVARIANT_GLOBALS`` to be unchanged. Catches a ``-D…`` injection that
    landed inside a ``makedepends=(...)`` array (the spirv-llvm-translator
    exit-12 brick), and any future patcher that mangles a dependency array.

    **G2 — managed ``-D…`` args ride a cmake command.** Joins ``\\``-continuations
    and, for each managed token actually present, requires its logical line to
    begin with ``cmake``. Catches a ``-D…`` arg orphaned as its own command (the
    clang composition exit-4 brick).

    ``rename`` (the dict returned by :func:`patch_package_suffix`) relaxes G1 for
    the five identity globals a rename intentionally mutates
    (``_RENAME_MUTABLE_GLOBALS``) and instead routes them through
    :func:`_validate_rename` — so the rename stays a *validated* transformation
    with one validator home, while every non-rename patch keeps the strict
    invariant. Default ``None`` leaves existing callers unchanged.

    Intended to be called only when a cmake-arg injection actually ran (the
    toolchain LLVM path), where the dependency-array invariant strictly holds.
    """
    from sysforge.primitives import pkgbuild_meta
    orig = pkgbuild_meta.parse_pkgbuild(original_path).get("globals", {})
    parsed_patched = pkgbuild_meta.parse_pkgbuild(patched_path)
    patched = parsed_patched.get("globals", {})
    patched_functions = parsed_patched.get("functions", {})
    for key in _INVARIANT_GLOBALS:
        if rename is not None and key in _RENAME_MUTABLE_GLOBALS:
            continue
        if orig.get(key) != patched.get(key):
            msg = (
                f"patch altered '{key}' — a dependency/identity field that must "
                f"never change: {orig.get(key)!r} -> {patched.get(key)!r}. A cmake "
                f"arg injection most likely anchored on a dependency-array entry."
            )
            _log.error(msg)
            raise PkgbuildPatchError(msg)
    if rename is not None:
        _validate_rename(orig, patched, rename, patched_functions)

    # G3 (rename only): a rename that updated ``pkgname`` but left the matching
    # ``package_<orig>()`` function un-renamed is the split-package brick — makepkg
    # aborts at packaging time looking for ``package_<new>()``. Fire precisely on
    # that case: the renamed function is absent *and* the stale original lingers.
    # When the PKGBUILD has no per-member function at all (a single bare
    # ``package()``, or a fixture), there is nothing for the rename to have
    # missed, so stay silent.
    if rename is not None:
        patched_text = Path(patched_path).read_text(encoding="utf-8")
        for orig, new in zip(
            rename.get("origin_pkgnames", []), rename.get("renamed_pkgnames", []), strict=True
        ):
            if orig == new:
                continue
            has_new = re.search(rf"(?m)^\s*package_{re.escape(new)}\s*\(\s*\)", patched_text)
            has_orig = re.search(rf"(?m)^\s*package_{re.escape(orig)}\s*\(\s*\)", patched_text)
            if not has_new and has_orig:
                msg = (
                    f"rename updated pkgname {orig!r} → {new!r} but left its "
                    f"`package_{orig}()` function un-renamed — makepkg would abort "
                    "at packaging time looking for the renamed function"
                )
                _log.error(msg)
                raise PkgbuildPatchError(msg)

    # G2: join `\`-continuations so an injected arg shares its cmake's logical line.
    joined = Path(patched_path).read_text(encoding="utf-8").replace("\\\n", " ")
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("cmake"):
            continue
        for token in _MANAGED_CMAKE_TOKENS:
            if token in stripped:
                msg = (
                    f"injected cmake arg is not attached to a cmake command: "
                    f"{stripped[:80]!r}. Anchor/continuation bug in the injector."
                )
                _log.error(msg)
                raise PkgbuildPatchError(msg)


def validate_patched_meson_pkgbuild(
    original_path,
    patched_path,
    gallium: list[str] | None,
    vulkan: list[str] | None,
) -> None:
    """Structural validation of a mesa PKGBUILD.sysforge after a
    :func:`patch_mesa_drivers` rewrite. Raises :class:`PkgbuildPatchError` on a
    botched rewrite — the meson counterpart of :func:`validate_patched_pkgbuild`.

    Reuses G1 (identity/dependency globals unchanged) via
    :func:`validate_patched_pkgbuild` — meson_options is a ``local`` inside
    ``build()`` so a driver rewrite must never touch a global, and G2's
    cmake-token loop is a harmless no-op here. Then, for each axis that was
    filtered, asserts the rewritten ``-D <opt>=`` value:

      * still exists and is not the upstream ``all`` sentinel, and
      * carries the mandatory software baseline
        (``MESA_MANDATORY_*`` — the inverse-AMDGPU invariant: a mesa with no
        software renderer bricks headless/VM/recovery).
    """
    validate_patched_pkgbuild(original_path, patched_path)
    text = Path(patched_path).read_text(encoding="utf-8")

    def _check(axis, regex, requested, mandatory):
        if not requested:
            return
        m = regex.search(text)
        if not m:
            msg = f"mesa {axis} rewrite reported success but no `-D {axis}=` option remains"
            _log.error(msg)
            raise PkgbuildPatchError(msg)
        value = m.group(0)[len(m.group(1)):].strip("\"'")
        tokens = {t for t in value.split(",") if t}
        if "all" in tokens:
            msg = f"mesa {axis} rewrite left the upstream 'all' sentinel: {value!r}"
            _log.error(msg)
            raise PkgbuildPatchError(msg)
        missing = [d for d in mandatory if d not in tokens]
        if missing:
            msg = (
                f"mesa {axis} rewrite dropped mandatory software driver(s) "
                f"{', '.join(missing)} (value {value!r}) — a build with no "
                "software renderer bricks headless/VM/recovery sessions"
            )
            _log.error(msg)
            raise PkgbuildPatchError(msg)

    _check("gallium-drivers", _MESA_GALLIUM_RE, gallium, MESA_MANDATORY_GALLIUM)
    _check("vulkan-drivers", _MESA_VULKAN_RE, vulkan, MESA_MANDATORY_VULKAN)


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
