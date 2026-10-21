# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Every environment variable and journal field sysforge uses is registered
(3.5.0-B128, 3.5.0-DOC13), and the man page is generated from the registry.

Before primitives/env_vars.py the man page listed three of the eleven
variables, with a stale editor order and pager flags; SYSFORGE_PROFILE_STORE
was documented nowhere, and the journal fields not at all.
"""
import ast
from pathlib import Path

from sysforge.primitives.env_vars import ENV_VARS
from sysforge.primitives.journal import JOURNAL_FIELDS

REPO = Path(__file__).resolve().parent.parent

# SYSFORGE_* strings that are not names sysforge reads: heredoc delimiters in
# the PKGBUILD text llvm_std_exports generates.
_NOT_NAMES = {"SYSFORGE_CMAKE", "SYSFORGE_TU"}


def _sysforge_strings():
    """(where, value) for each SYSFORGE_* string literal under sysforge/."""
    for py in sorted((REPO / "sysforge").rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.startswith("SYSFORGE_")
                    and node.value.replace("_", "").isalnum()
                    and node.value.isupper()):
                yield f"{py.relative_to(REPO)}:{node.lineno}", node.value


def test_every_sysforge_name_in_the_code_is_registered():
    known = {v.name for v in ENV_VARS} | {f for f, _ in JOURNAL_FIELDS} | _NOT_NAMES
    bad = [f"{where}: {name}" for where, name in _sysforge_strings() if name not in known]
    assert not bad, ("register these in primitives/env_vars.ENV_VARS "
                     "(or journal.JOURNAL_FIELDS):\n  " + "\n  ".join(bad))


def test_every_registered_variable_is_read_somewhere():
    """A stale entry documents a variable that no longer does anything."""
    used = {name for _, name in _sysforge_strings()}
    source = "".join(p.read_text(encoding="utf-8")
                     for p in (REPO / "sysforge").rglob("*.py")
                     if p.name != "env_vars.py")
    stale = [v.name for v in ENV_VARS
             if v.name not in used and f'"{v.name}"' not in source]
    assert not stale, f"registered but never read: {stale}"


def test_registry_shape():
    names = [v.name for v in ENV_VARS]
    assert len(names) == len(set(names))
    assert {v.audience for v in ENV_VARS} <= {"user", "diagnostic"}
    assert all(v.text.endswith(".") for v in ENV_VARS)


def test_man_page_lists_every_variable_and_journal_field():
    man = (REPO / "man/sysforge.1.scd.in").read_text(encoding="utf-8")
    assert "@ENVIRONMENT@" in man and "@JOURNAL@" in man
    rendered = (REPO / "man/sysforge.1").read_text(encoding="utf-8")
    missing = [n for n in [v.name for v in ENV_VARS] + [f for f, _ in JOURNAL_FIELDS]
               if n.replace("_", "\\_") not in rendered and n not in rendered]
    assert not missing, f"not in man/sysforge.1 (run `make man`): {missing}"
