# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Guards on ``verbs/registry.py``, the one list the parser is built from (3.2.0-F7).

Each verb owns its argparse surface through ``Verb.add_parser``. A verb class
that exists but is missing from ``COMMANDS`` (directly or through a
``VerbGroup``) has no command line at all, and nothing else would notice."""
import argparse
import ast
import inspect
from pathlib import Path

from sysforge.verbs import Verb, VerbGroup
from sysforge.verbs.registry import COMMANDS, build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent


def leaves(parser):
    """Every subparser under *parser*, depth-first."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sp in action.choices.values():
                yield sp
                yield from leaves(sp)


def _registered() -> list[type[Verb]]:
    out: list[type[Verb]] = []
    for entry in COMMANDS:
        out.extend(entry.members if isinstance(entry, VerbGroup) else (entry,))
    return out


def _concrete_verbs(base: type = Verb) -> set[type[Verb]]:
    found: set[type[Verb]] = set()
    for cls in base.__subclasses__():
        if cls.__module__.startswith("sysforge.") and not inspect.isabstract(cls):
            found.add(cls)
        found |= _concrete_verbs(cls)
    return found


def test_every_concrete_verb_is_registered_once():
    registered = _registered()
    assert len(registered) == len(set(registered)), "a verb is registered twice"
    missing = _concrete_verbs() - set(registered)
    assert not missing, (
        "Verb subclasses absent from verbs/registry.py COMMANDS: "
        + ", ".join(sorted(c.__qualname__ for c in missing)))


def test_every_registered_verb_defines_add_parser():
    base = Verb.add_parser.__func__  # type: ignore[attr-defined]
    for cls in _registered():
        assert cls.add_parser.__func__ is not base, f"{cls.__qualname__} lacks add_parser"


def test_each_leaf_parser_dispatches_to_its_own_class():
    """``set_defaults(verb_cls=cls)`` binds each subparser to the class that built it."""
    bound = {sp._defaults.get("verb_cls") for sp in leaves(build_parser())}
    assert set(_registered()) <= bound


def test_help_cmd_does_not_import_cli():
    """help builds the parser from the registry, not by reaching up into cli."""
    tree = ast.parse((REPO_ROOT / "sysforge/help_cmd.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "sysforge.cli" not in modules


def test_no_update_help_describes_the_source_sync_not_git_pull():
    """3.4.0-B15: source sync goes through the scheduler (fetch + fast-forward),
    so no verb's --no-update may still promise to skip `git pull --rebase`."""
    seen = []
    for sp in leaves(build_parser()):
        for action in sp._actions:
            if "--no-update" in action.option_strings:
                seen.append(sp.prog)
                assert "git pull" not in (action.help or ""), sp.prog
                assert "source sync" in (action.help or ""), sp.prog
    assert len(seen) == 6, seen


def test_no_user_facing_text_names_git_pull():
    """3.5.0-B2: source sync is the scheduler's fetch + fast-forward, so the
    update summary row, `update --offline` help and the makepkg dependency
    hint must not describe or recommend `git pull`."""
    from sysforge.update_summary import _ACTION_FORMATS
    tag, label, row = _ACTION_FORMATS["PULL_FAILED"]
    assert (tag, label) == ("SYNC_FAILED", "sync failed")
    assert "git pull" not in row and "source sync failed" in row
    for sp in leaves(build_parser()):
        for action in sp._actions:
            assert "git pull" not in (action.help or ""), (sp.prog, action.dest)
    src = (REPO_ROOT / "sysforge/primitives/makepkg_invoke.py").read_text(encoding="utf-8")
    assert "git pull" not in src
