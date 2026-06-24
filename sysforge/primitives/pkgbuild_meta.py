# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pkgbuild_meta.py — static PKGBUILD parser

Responsible for reading and parsing PKGBUILD metadata. Does not source,
execute, or modify any PKGBUILD. All mutation lives in pkgbuild_patcher.py.

Public API:
    parse_pkgbuild(path) -> {"globals": {...}, "functions": {...}}
    has_hardcoded_gcc(parsed) -> bool
    is_musl_static_build(parsed) -> bool
"""
import re


def _strip_comments(text):
    """Strip # comments, respecting quoted strings."""
    result = []
    for line in text.splitlines():
        out = []
        in_single = False
        in_double = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == "'" and not in_double:
                in_single = not in_single
            elif c == '"' and not in_single:
                in_double = not in_double
            elif c == "#" and not in_single and not in_double:
                break
            out.append(c)
            i += 1
        result.append("".join(out).rstrip())
    return "\n".join(result)


def _extract_arrays(text):
    """Extract array assignments with proper paren depth tracking."""
    arrays = {}
    pattern = re.compile(r"^(\w+)=\(", re.MULTILINE)
    for m in pattern.finditer(text):
        key = m.group(1)
        j = m.end()
        depth = 1
        while j < len(text) and depth > 0:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        raw = text[m.end() : j - 1]
        arrays[key] = _parse_array_items(raw)
    return arrays


def _extract_functions(text):
    """Extract function bodies and return cleaned global text."""
    functions = {}
    spans = []
    i = 0
    func_start = re.compile(r"([\w][\w-]*)\s*\(\s*\)\s*\{")
    while i < len(text):
        if i == 0 or text[i - 1] == "\n":
            m = func_start.match(text, i)
        else:
            m = None
        if m:
            func_name = m.group(1)
            j = m.end()
            depth = 1
            while j < len(text) and depth > 0:
                if text[j] == "$" and j + 1 < len(text) and text[j + 1] == "{":
                    j += 2
                    inner_depth = 1
                    while j < len(text) and inner_depth > 0:
                        if text[j] == "{":
                            inner_depth += 1
                        elif text[j] == "}":
                            inner_depth -= 1
                        j += 1
                    continue
                elif text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            functions[func_name] = text[m.end() : j - 1].strip("\n")
            spans.append((m.start(), j))
            i = j
        else:
            i += 1
    global_text = text
    for start, end in reversed(spans):
        global_text = global_text[:start] + global_text[end:]
    return functions, global_text


def _split_top_commas(s):
    """Split ``s`` on commas that sit at brace-depth 0 (brace-expansion helper)."""
    parts, depth, cur = [], 0, []
    for c in s:
        if c == "{":
            depth += 1
            cur.append(c)
        elif c == "}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _expand_sequence(s):
    """Expand a bash sequence expression ``x..y[..incr]``; return None if not one.

    Supports numeric (``1..3``, ``5..1``, ``0..10..2``) and single-char
    (``a..e``) ranges. Zero-padding fidelity is intentionally skipped — it does
    not occur in dependency/source arrays in practice.
    """
    m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", s)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        step = abs(int(m.group(3))) if m.group(3) else 1
        step = step or 1
        rng = (range(start, end + 1, step) if start <= end
               else range(start, end - 1, -step))
        return [str(v) for v in rng]
    m = re.fullmatch(r"([a-zA-Z])\.\.([a-zA-Z])(?:\.\.(-?\d+))?", s)
    if m:
        start, end = ord(m.group(1)), ord(m.group(2))
        step = abs(int(m.group(3))) if m.group(3) else 1
        step = step or 1
        rng = (range(start, end + 1, step) if start <= end
               else range(start, end - 1, -step))
        return [chr(v) for v in rng]
    return None


def _expand_braces(token):
    """Bash-style brace expansion of a single unquoted word.

    Handles comma lists (``python-{build,installer,wheel}`` →
    ``python-build python-installer python-wheel``), sequence expressions
    (``{1..3}``, ``{a..c}``), nesting, and multiple groups (cartesian product).
    A brace group with no top-level comma and no sequence is literal, as in bash
    (``{foo}`` stays ``{foo}``). ``${...}`` parameter expansions are skipped
    whole so their contents are never split — variable expansion runs later in
    :func:`parse_pkgbuild`.
    """
    n = len(token)
    i = 0
    while i < n:
        if token[i] == "{":
            # Find the matching close brace, tracking nesting depth.
            depth, j = 0, i
            while j < n:
                if token[j] == "{":
                    depth += 1
                elif token[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:  # unbalanced — nothing further to expand
                return [token]

            if i > 0 and token[i - 1] == "$":
                # ${...} parameter expansion — skip the whole span, never split.
                i = j + 1
                continue

            inner = token[i + 1:j]
            parts = _split_top_commas(inner)
            if len(parts) > 1:
                options = parts
            else:
                options = _expand_sequence(inner)
            if not options:
                # Not a valid brace expansion: keep braces literal, scan on.
                i = j + 1
                continue

            prefix, suffix = token[:i], token[j + 1:]
            results = []
            for opt in options:
                for head in _expand_braces(opt):
                    for tail in _expand_braces(suffix):
                        results.append(prefix + head + tail)
            return results
        i += 1
    return [token]


def _parse_array_items(raw):
    """Parse array contents respecting quoted strings with spaces.

    Unquoted tokens undergo bash brace expansion
    (``python-{build,installer,wheel}`` yields three items); quoted tokens are
    kept verbatim, matching bash (brace expansion does not occur within quotes).
    """
    items = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", raw)
    result = []
    for sq, dq, unq in items:
        if sq:
            result.append(sq)
        elif dq:
            result.append(dq)
        elif unq:
            result.extend(_expand_braces(unq))
    return result


# PKGBUILD(5) array families that accept arch-specific variants
# (e.g. ``makedepends_x86_64``). The arch-suffixed array contributes
# additively to the canonical array for the running architecture.
_ARCH_ARRAY_FAMILIES = (
    "depends",
    "makedepends",
    "checkdepends",
    "optdepends",
    "provides",
    "conflicts",
    "replaces",
)


def _merge_arch_arrays(globals_dict):
    """Merge ``<name>_<arch>`` arrays into their canonical ``<name>`` key.

    PKGBUILD(5) allows arch-specific array variants (`makedepends_x86_64`,
    `depends_aarch64`, etc.) that the runtime appends to the canonical array
    when CARCH matches. The static parser sees every variant unconditionally,
    so consumes inference and rule matching would otherwise miss entries that
    only appear under an arch suffix (e.g. ``makedepends_x86_64=('lib32-rust')``).

    Merge is additive and order-preserving; the canonical key keeps its
    existing entries first, then arch-suffixed entries are appended in the
    order they appear in the dict. Arch-suffixed keys are retained so callers
    that need to distinguish (e.g. CARCH-aware tools) can still read them.
    """
    for family in _ARCH_ARRAY_FAMILIES:
        merged: list = []
        seen: set = set()
        for k, v in list(globals_dict.items()):
            if k != family and not k.startswith(family + "_"):
                continue
            if not isinstance(v, list):
                continue
            for item in v:
                if item not in seen:
                    merged.append(item)
                    seen.add(item)
        if merged:
            globals_dict[family] = merged


# Matches simple variable references: $var and ${var}.  Intentionally does NOT
# match shell parameter-expansion forms like ${var:-default}, ${var%suffix},
# ${var#prefix} — those expressions are left untouched so we never produce a
# misleading partial substitution.
_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_vars(value, scalars, max_iters=8):
    """Substitute $var / ${var} references using scalars until a fixed point.

    Unknown names are preserved verbatim.  Bounded iteration guards against
    self-referential scalars like `_a="$_a"`.
    """
    for _ in range(max_iters):
        def repl(m):
            name = m.group(1) or m.group(2)
            v = scalars.get(name)
            return v if isinstance(v, str) else m.group(0)
        new = _VAR_REF.sub(repl, value)
        if new == value:
            return new
        value = new
    return value


def _apply_var_expansion(globals_dict):
    """Expand $var / ${var} in scalar and array globals in place.

    Some PKGBUILDs define pkgname/pkgbase via shell variables, e.g.
    `_pkgname=foo; pkgname="$_pkgname-git"`.  Without expansion, downstream
    consumers (build_state keys, pacman version checks) see the literal
    reference string and silently miss the package.  This pass substitutes
    references that can be resolved from other scalar globals; unresolvable
    references are left alone so caller-side warnings remain meaningful.
    """
    scalars = {
        k: v for k, v in globals_dict.items() if isinstance(v, str)
    }
    # Resolve scalar-to-scalar references first (fixed point over the dict).
    for _ in range(8):
        changed = False
        new_scalars = {}
        for k, v in scalars.items():
            nv = _expand_vars(v, scalars)
            if nv != v:
                changed = True
            new_scalars[k] = nv
        scalars = new_scalars
        if not changed:
            break

    for k, v in list(globals_dict.items()):
        if isinstance(v, str):
            globals_dict[k] = scalars[k]
        elif isinstance(v, list):
            globals_dict[k] = [
                _expand_vars(item, scalars) if isinstance(item, str) else item
                for item in v
            ]


# Matches an array-parameter reference that occupies an entire array item,
# e.g. ``${_pydeps[@]}`` or ``${_pydeps[@]/#/python-}``.  group(1) is the
# referenced array name; group(2) is the optional transform suffix (everything
# between the closing ``]`` and the closing ``}``).  ``[@]`` and ``[*]`` are both
# accepted — for a dependency array the splice-into-multiple-items behaviour is
# what we want regardless of quoting.
_ARRAY_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\[[@*]\](.*)\}$")


def _has_glob_meta(s):
    """True if *s* contains bash pattern metacharacters we do not interpret."""
    return any(c in s for c in "*?[")


def _apply_array_transform(elements, transform):
    """Apply a bash ``${arr[@]<transform>}`` suffix to each array element.

    Supports the dependency-array idioms seen in real PKGBUILDs:

    - ``""``         → elements unchanged (``${a[@]}``)
    - ``/#/PREFIX``  → prepend PREFIX to each (``${a[@]/#/python-}``)
    - ``/%/SUFFIX``  → append SUFFIX to each  (``${a[@]/%/-git}``)
    - ``/PAT/REPL``  → replace first literal PAT with REPL in each
    - ``//PAT/REPL`` → replace every literal PAT with REPL in each

    Returns ``None`` for an unsupported transform (slices, anchored
    ``/#PAT/``/``/%PAT/`` patterns, glob metacharacters) so the caller can leave
    the original token verbatim rather than emit a misleading partial result.
    """
    if not transform:
        return list(elements)
    if transform.startswith("/#/"):       # ${a[@]/#/PREFIX} → prepend
        return [transform[3:] + e for e in elements]
    if transform.startswith("/%/"):       # ${a[@]/%/SUFFIX} → append
        return [e + transform[3:] for e in elements]
    if transform.startswith("//"):        # ${a[@]//PAT/REPL} → replace all
        rest = transform[2:]
        if "/" in rest and rest[:1] not in ("#", "%"):
            pat, repl = rest.split("/", 1)
            if pat and not _has_glob_meta(pat):
                return [e.replace(pat, repl) for e in elements]
        return None
    if transform.startswith("/"):         # ${a[@]/PAT/REPL} → replace first
        rest = transform[1:]
        if "/" in rest and rest[:1] not in ("#", "%"):
            pat, repl = rest.split("/", 1)
            if pat and not _has_glob_meta(pat):
                return [e.replace(pat, repl, 1) for e in elements]
        return None
    return None


def _expand_array_refs(globals_dict):
    """Resolve ``${arr[@]}`` / ``${arr[*]}`` references in array globals in place.

    bash array expansions such as ``depends=("${_pydeps[@]/#/python-}")`` splice
    every element of ``_pydeps`` (each prefixed with ``python-``) into ``depends``.
    The static parser captures ``_pydeps`` as its own array global, so this pass
    resolves the reference by symbol-table lookup — no shell sourcing.  Supported
    transforms are the dependency-array idioms in :func:`_apply_array_transform`.

    A reference to an unknown array, or an unsupported transform, leaves the token
    verbatim — downstream the AUR resolver detects the surviving ``${...}`` and
    rescues the deps from authoritative AUR RPC metadata, and a guard keeps the
    junk token out of ``pacman``/AUR queries.  Runs after ``_merge_arch_arrays``
    and before ``_apply_var_expansion`` so spliced items still get ``$var``
    substitution.
    """
    for key, value in list(globals_dict.items()):
        if not isinstance(value, list):
            continue
        new_items = []
        changed = False
        for item in value:
            m = _ARRAY_REF.match(item) if isinstance(item, str) else None
            if not m:
                new_items.append(item)
                continue
            ref = globals_dict.get(m.group(1))
            if not isinstance(ref, list):
                new_items.append(item)        # unknown array — leave verbatim
                continue
            expanded = _apply_array_transform(ref, m.group(2))
            if expanded is None:
                new_items.append(item)        # unsupported transform — verbatim
                continue
            new_items.extend(expanded)
            changed = True
        if changed:
            globals_dict[key] = new_items


# Matches a hardcoded gcc/g++ invocation at the start of a logical line:
# optional leading whitespace, optional VAR=value env assignments, optional
# ccache prefix, then gcc or g++ as the command itself (followed by space or
# end-of-line). Deliberately conservative — does not match $CC / ${CXX},
# -lgcc, libgcc, or mentions inside strings/comments (comments are stripped
# by parse_pkgbuild before this runs).
_HARDCODED_GCC_CMD = re.compile(
    r"""
    ^                             # start of a logical line
    [ \t]*                        # leading whitespace
    (?:[A-Za-z_][A-Za-z0-9_]*=\S+[ \t]+)*  # any leading VAR=value assignments
    (?:ccache[ \t]+)?             # optional ccache prefix
    (?:gcc|g\+\+)                 # gcc or g++ as the command
    (?:[ \t]|$)                   # word-terminated (space, tab, or line end)
    """,
    re.VERBOSE | re.MULTILINE,
)

# Matches CC=gcc / CXX=g++ (and HOSTCC/HOSTCXX) anywhere a build-time
# assignment would land: standalone (`CC=gcc make`), as a make argument
# (`make CXX=g++`), or as an exported quoted value (`export CC='gcc -m32'`).
# The terminator is a lookahead so optional surrounding quotes work without
# trying to model the entire quoted value — only that gcc/g++ is the leading
# token after `=` and that it ends on a word boundary.
_HARDCODED_GCC_ASSIGN = re.compile(
    r"""
    (?:^|[ \t;&|])                # start of line or shell separator
    (?:CC|CXX|HOSTCC|HOSTCXX)     # build-time compiler variable
    =                             # literal =
    ['"]?                         # optional opening quote
    (?:gcc|g\+\+)                 # gcc or g++
    (?:-\d+(?:\.\d+)*)?           # optional version suffix (gcc-12, g++-11.2)
    (?=[ \t'"&;|]|$)              # word-terminated (lookahead)
    """,
    re.VERBOSE | re.MULTILINE,
)


# PKGBUILD(5) build-time functions that may invoke compilers. ``verify`` is
# omitted — it authenticates sources and never compiles. ``package_<pkgname>``
# variants for split packages are matched dynamically via prefix below.
_SCAN_FUNCS_LITERAL = ("prepare", "build", "check", "package")


def has_hardcoded_gcc(parsed):
    """
    True if any PKGBUILD(5) build-time function body invokes gcc/g++ as a
    direct command, or forces CC=gcc / CXX=g++ when invoking a subsidiary
    build tool (e.g. ``make CXX=g++``, ``export CC='gcc -m32'``).

    Functions scanned: ``prepare``, ``build``, ``check``, ``package``, and any
    ``package_<pkgname>`` split-package variant (per PKGBUILD(5)). ``verify``
    is excluded — it is a source-authentication hook, not a build step.

    Used as a proactive signal for the flag guard: when the package's build
    system bypasses ``$CC``/``$CXX``, clang-only flags such as ``-flto=thin``
    must be rewritten to their GCC equivalents before invoking makepkg.

    Detection is deliberately conservative. False is not authoritative — a
    Makefile checked out in src/ may still hardcode ``g++``. The reactive
    post-failure path in ``invoke_makepkg`` catches those cases.
    """
    funcs = parsed.get("functions", {}) if isinstance(parsed, dict) else {}
    for name, body in funcs.items():
        if not body:
            continue
        if name not in _SCAN_FUNCS_LITERAL and not name.startswith("package_"):
            continue
        if _HARDCODED_GCC_CMD.search(body):
            return True
        if _HARDCODED_GCC_ASSIGN.search(body):
            return True
    return False


# musl makedepends that mark a PKGBUILD as a static-musl bootstrap (it builds
# its bundled libs against musl rather than glibc). pacman-static is the
# canonical example: makedepends=(... 'musl' 'kernel-headers-musl' ...).
_MUSL_MAKEDEPENDS = ("musl", "kernel-headers-musl")

# Matches CC=musl-gcc as a build-time assignment (standalone, make arg, or
# exported), mirroring _HARDCODED_GCC_ASSIGN's terminator handling. Also the
# value form `export CC="musl-gcc -..."`.
_MUSL_CC_ASSIGN = re.compile(
    r"""
    (?:^|[ \t;&|])                # start of line or shell separator
    (?:CC|CXX|HOSTCC|HOSTCXX)     # build-time compiler variable
    \+?=                          # literal = (or += append)
    ['"]?                         # optional opening quote
    musl-(?:gcc|g\+\+|clang)      # musl wrapper compiler
    (?=[ \t'"&;|]|$)              # word-terminated (lookahead)
    """,
    re.VERBOSE | re.MULTILINE,
)

# Matches a `-static` token added to LDFLAGS in a build-time function body
# (e.g. `export LDFLAGS="$LDFLAGS -static"`), word-bounded so `-static-libgcc`
# does not match.
_STATIC_LDFLAGS = re.compile(
    r"""
    LDFLAGS                       # the linker-flags variable
    [^\n]*?                       # anything up to the token on the same line
    (?<![\w-])-static(?![\w-])    # a bare -static token
    """,
    re.VERBOSE,
)


def is_musl_static_build(parsed):
    """
    True if the PKGBUILD bootstraps its libs as **static musl** binaries
    (``CC=musl-gcc`` + ``-static``), e.g. pacman-static.

    Such builds cannot take the sysforge profile's lld linker (``-fuse-ld=lld``
    + ``-static`` + musl produces a startup-crashing binary) or its PGO flags
    (musl-gcc cannot consume a clang ``.profdata``). Callers pass the result to
    ``emit_makepkg_conf(is_musl_static=True)`` to scrub those flags — the musl
    analogue of the ``is_lib32`` scrub.

    Detection is conservative (matching ``has_hardcoded_gcc``'s stance): it
    requires a ``musl``/``kernel-headers-musl`` makedepend **and** a build-time
    signal — a ``CC=musl-gcc`` assignment or a ``-static`` LDFLAGS append. The
    makedepend alone is insufficient (a package may merely link one static lib);
    False is not authoritative.
    """
    if not isinstance(parsed, dict):
        return False
    globals_ = parsed.get("globals", {})
    makedeps = globals_.get("makedepends", []) or []
    if not any(d in _MUSL_MAKEDEPENDS for d in makedeps):
        return False

    # The build-time signal may live in a scanned function body or at global
    # PKGBUILD scope (top-level `export CC=musl-gcc` / `LDFLAGS+=' -static'`,
    # which run when makepkg sources the file — e.g. pacman-static).
    bodies = [parsed.get("global_body", "")]
    funcs = parsed.get("functions", {})
    for name, body in funcs.items():
        if not body:
            continue
        if name not in _SCAN_FUNCS_LITERAL and not name.startswith("package_"):
            continue
        bodies.append(body)
    for body in bodies:
        if body and (_MUSL_CC_ASSIGN.search(body) or _STATIC_LDFLAGS.search(body)):
            return True
    return False


def parse_pkgbuild(path):
    """
    Parse a PKGBUILD statically without sourcing or executing it.

    Returns:
        {
            "globals":   { "pkgname": ..., "makedepends": [...], ... },
            "functions": { "build": "...", "prepare": "...", ... }
        }

    Reliably parseable: pkgname, pkgver, pkgrel, epoch, groups, depends,
    makedepends, provides, and all standard scalar/array globals. Function
    bodies are extracted verbatim under their function name.

    Not statically parseable: computed values, conditional metadata,
    depends+=() inside functions. The wrapper falls back to the default
    profile when parsing fails.
    """
    text = _strip_comments(open(path, encoding="utf-8").read())
    result = {"globals": {}, "functions": {}}
    result["functions"], global_text = _extract_functions(text)
    # Retain the raw top-level script body (functions removed). Some PKGBUILDs
    # set build-time toolchain via global `export CC=...` / `LDFLAGS+=...`
    # statements that run when makepkg sources the file — these never land in
    # `globals` (the `^(\w+)=` scan skips `export X=`) nor in `functions`.
    result["global_body"] = global_text
    result["globals"].update(_extract_arrays(global_text))
    for m in re.finditer(
        r"""^(\w+)=(?:"([^"]*)"|'([^']*)'|([^()\n'"]+))""",
        global_text,
        re.MULTILINE,
    ):
        key = m.group(1)
        value = next(g for g in m.groups()[1:] if g is not None)
        if key not in result["globals"]:
            result["globals"][key] = value.strip()
    _merge_arch_arrays(result["globals"])
    _expand_array_refs(result["globals"])
    _apply_var_expansion(result["globals"])
    return result
