# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Every config key the code reads is documented in the shipped default (3.5.0-F18).

`sysforge config check` counts a key in a fixed section as valid when the
shipped default documents it, so a key the code reads but the default never
mentions would be reported as unknown, and `config clean --unknown` would
delete a working setting. `toolchain.toml`'s `profile_store` and
`profiles.toml`'s `extends_system` were both read and undocumented.

The scan finds section reads off a config-like object (a name containing cfg,
config, conf, toml, data or raw, or a load_*() call): `cfg.get("ui", {})
.get("editor")`, `cfg["build"]["jobs"]`, and `.get`/`[]` on a local assigned
from such a section in the same function. The typed stage configs' top-level
keys have their own tests (tests/test_check_shipped.py); this adds the
fixed-section keys and the schema's top-level keys.
"""
import ast
import collections
import re
from pathlib import Path

from sysforge.primitives import config_schema as cs

PKG = Path(__file__).resolve().parent.parent / "sysforge"
_CONFIGISH = re.compile(r"cfg|config|conf|toml|data|raw", re.IGNORECASE)


def _fixed_sections() -> dict[str, set[str]]:
    """Section -> the live config files where it is a fixed (not user-named) one."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    for name in cs.CONFIG_FILES:
        for section in cs.KNOWN_SECTIONS[name] - cs.OPEN_SECTIONS.get(name, set()):
            out[section].add(name)
    return out


def _configish(base) -> bool:
    if isinstance(base, ast.Call):
        name = getattr(base.func, "id", None) or getattr(base.func, "attr", "")
        return name.startswith("load") or bool(_CONFIGISH.search(name))
    return bool(_CONFIGISH.search(getattr(base, "id", None) or getattr(base, "attr", None) or ""))


def _section_of(expr, sections) -> str | None:
    if isinstance(expr, ast.BoolOp):                  # cfg.get("ui") or {}
        expr = expr.values[0]
    if isinstance(expr, ast.Call) and getattr(expr.func, "attr", None) == "get":
        base, key = expr.func.value, expr.args[0] if expr.args else None
    elif isinstance(expr, ast.Subscript):
        base, key = expr.value, expr.slice
    else:
        return None
    if (isinstance(key, ast.Constant) and key.value in sections and _configish(base)):
        return key.value
    return None


def section_reads(text: str, sections) -> set[tuple[str, str, int]]:
    """(section, key, line) for each fixed-section key read in ``text``."""
    out = set()
    for fn in (n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)):
        local = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                    and isinstance(n.targets[0], ast.Name):
                section = _section_of(n.value, sections)
                if section:
                    local[n.targets[0].id] = section
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get" and n.args \
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                holder, key = n.func.value, n.args[0].value
            elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                    and isinstance(n.slice.value, str):
                holder, key = n.value, n.slice.value
            else:
                continue
            section = _section_of(holder, sections) or (
                local.get(holder.id) if isinstance(holder, ast.Name) else None)
            if section:
                out.add((section, key, n.lineno))
    return out


def test_every_fixed_section_key_the_code_reads_is_documented():
    sections = _fixed_sections()
    documented = {name: cs._documented(cs.shipped_text(name)) for name in cs.CONFIG_FILES}
    reads, undocumented = 0, []
    for py in sorted(PKG.rglob("*.py")):
        for section, key, line in section_reads(py.read_text(encoding="utf-8"), sections):
            reads += 1
            if not any(key in documented[f].get(section, set()) for f in sections[section]):
                undocumented.append(f"{py.relative_to(PKG.parent)}:{line}: [{section}] {key}")
    assert reads >= 20, f"the scan found only {reads} reads; it has stopped seeing the code"
    assert not undocumented, ("document these in the shipped default (active or as a "
                              "commented example):\n  " + "\n  ".join(sorted(undocumented)))


def test_every_known_top_level_key_is_documented():
    for name in ("kernel.toml", "toolchain.toml", "profiles.toml"):
        documented = cs._documented(cs.shipped_text(name)).get("", set())
        assert cs.KNOWN_TOP_KEYS[name] <= documented, (name, cs.KNOWN_TOP_KEYS[name] - documented)


def test_the_scan_sees_chained_and_local_reads():
    src = ('def f(cfg, entry):\n'
           '    a = cfg.get("ui", {}).get("editor")\n'
           '    build = cfg.get("build") or {}\n'
           '    b = build.get("jobs")\n'
           '    c = entry.get("ui", {}).get("not_config")\n')   # entry is no config
    assert {(s, k) for s, k, _ in section_reads(src, {"ui", "build"})} == {
        ("ui", "editor"), ("build", "jobs")}
