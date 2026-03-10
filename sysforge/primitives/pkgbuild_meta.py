import re


def _extract_functions(text):
    """Extract function bodies with proper brace depth tracking."""
    functions = {}
    i = 0
    func_start = re.compile(r"(\w+)\s*\(\s*\)\s*\{")
    while i < len(text):
        m = func_start.match(text, i)
        if m:
            func_name = m.group(1)
            j = m.end()
            depth = 1
            while j < len(text) and depth > 0:
                # skip ${ } variable expansions to avoid false brace counting
                if text[j] == "$" and j + 1 < len(text) and text[j + 1] == "{":
                    j += 2
                    while j < len(text) and text[j] != "}":
                        j += 1
                elif text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            functions[func_name] = text[m.end() : j - 1].strip()
            i = j
        else:
            i += 1
    return functions


def _parse_array_items(raw):
    """Parse array contents respecting quoted strings with spaces."""
    items = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", raw)
    result = []
    for groups in items:
        val = next((g for g in groups if g), None)
        if val:
            result.append(val)
    return result


def parse_pkgbuild(path):
    text = open(path).read()
    result = {"globals": {}, "functions": {}}

    result["functions"] = _extract_functions(text)
    global_text = re.sub(r"\w+\s*\(\s*\)\s*\{[^}]*\}", "", text, flags=re.DOTALL)

    # Scalars
    for m in re.finditer(
        r'^(\w+)=["\']?([^()\n"\']+)["\']?',
        global_text,
        re.MULTILINE,
    ):
        result["globals"][m.group(1)] = m.group(2).strip()

    # Arrays
    for m in re.finditer(
        r"^(\w+)=\(([^)]*)\)",
        global_text,
        re.MULTILINE | re.DOTALL,
    ):
        result["globals"][m.group(1)] = _parse_array_items(m.group(2))

    return result
