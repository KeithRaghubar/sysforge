"""
pkgbuild_meta.py — static PKGBUILD parser

Responsible for reading and parsing PKGBUILD metadata. Does not source,
execute, or modify any PKGBUILD. All mutation lives in pkgbuild_patcher.py.

Public API:
    parse_pkgbuild(path) -> {"globals": {...}, "functions": {...}}
    has_hardcoded_gcc(parsed) -> bool
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


def _parse_array_items(raw):
    """Parse array contents respecting quoted strings with spaces."""
    items = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", raw)
    result = []
    for groups in items:
        val = next((g for g in groups if g), None)
        if val:
            result.append(val)
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
    text = _strip_comments(open(path).read())
    result = {"globals": {}, "functions": {}}
    result["functions"], global_text = _extract_functions(text)
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
    _apply_var_expansion(result["globals"])
    return result
