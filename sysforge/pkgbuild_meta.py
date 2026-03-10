import re


def parse_pkgbuild(path):
    text = open(path).read()
    result = {}

    # Scalars
    for m in re.finditer(
        r'^(\w+)=["\']?([^()\n"\']+)["\']?',
        text,
        re.MULTILINE,
    ):
        result[m.group(1)] = m.group(2).strip()

    # Arrays
    for m in re.finditer(
        r"^(\w+)=\(([^)]*)\)",
        text,
        re.MULTILINE | re.DOTALL,
    ):
        items = re.findall(r'["\']?([^\s"\']+)["\']?', m.group(2))
        result[m.group(1)] = [i for i in items if i]

    return result
