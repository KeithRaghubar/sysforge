import re


def parse_pkgbuild(path):
    text = open(path).read()
    result = {"globals": {}, "functions": {}}

    # Strip function bodies out first
    func_pattern = re.compile(
        r"^(\w+)\s*\(\s*\)\s*\{([^}]*)\}",
        re.MULTILINE | re.DOTALL,
    )
    for m in func_pattern.finditer(text):
        result["functions"][m.group(1)] = m.group(2).strip()

    global_text = func_pattern.sub("", text)

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
        items = re.findall(r'["\']?([^\s"\']+)["\']?', m.group(2))
        result["globals"][m.group(1)] = [i for i in items if i]

    return result
