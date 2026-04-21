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

# Matches CC=gcc / CXX=g++ (and HOSTCC/HOSTCXX) anywhere that a build-time
# assignment would land — either a standalone line like `CC=gcc make` or as
# an argument to make: `make CXX=g++`. The trailing context must be a word
# boundary so we don't match longer values like CC=gcc-12 (harmless to match,
# but exclude for clarity) — actually, gcc-12 is still hardcoded GCC, so we
# accept any -\d suffix too.
_HARDCODED_GCC_ASSIGN = re.compile(
    r"""
    (?:^|[ \t;&|])                # start of line or typical command separator
    (?:CC|CXX|HOSTCC|HOSTCXX)     # build-time compiler variable
    =                             # literal =
    (?:gcc|g\+\+)                 # gcc or g++ value
    (?:-\d+(?:\.\d+)*)?           # optional version suffix (gcc-12, g++-11.2)
    (?:[ \t;&|]|$)                # word-terminated
    """,
    re.VERBOSE | re.MULTILINE,
)


def has_hardcoded_gcc(parsed):
    """
    True if the PKGBUILD's build()/package()/prepare() function bodies invoke
    gcc/g++ as a direct command, or force CC=gcc / CXX=g++ when invoking a
    subsidiary build tool (e.g. ``make CXX=g++``).

    Used as a proactive signal for the flag guard: when the package's build
    system bypasses ``$CC``/``$CXX``, clang-only flags such as ``-flto=thin``
    must be rewritten to their GCC equivalents before invoking makepkg.

    Detection is deliberately conservative. False is not authoritative — a
    Makefile checked out in src/ may still hardcode ``g++``. The reactive
    post-failure path in ``invoke_makepkg`` catches those cases.
    """
    funcs = parsed.get("functions", {}) if isinstance(parsed, dict) else {}
    for name in ("build", "package", "prepare"):
        body = funcs.get(name)
        if not body:
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
    _apply_var_expansion(result["globals"])
    return result
