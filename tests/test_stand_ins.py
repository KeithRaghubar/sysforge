# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Tests stand in real objects with the real thing (3.5.0-DEV13).

A ``SimpleNamespace`` in place of ``BuildOptions``, ``RunOptions`` or a parsed
command line drifts from what it imitates. When the code under test reads a new
field, every such test breaks at once (``cc_override``, ``save_toolchain``
during 3.5.0-F14/B125/B126). When a field is renamed, the stub keeps the old
name and the test goes on passing against an object production never builds.

``BuildOptions(...)`` and ``RunOptions(...)`` default every field and reject a
name they do not have, and ``cli_args("state", "list", state_dir=d)``
(tests/cli_args.py) parses a real command line, then sets fields the command
has. Use those.

Options stand-ins are recognised by a field that is on the dataclass but is no
CLI flag's destination, so only an options object has it. Argument stand-ins
are recognised by fields that are all one command's flags. ``_ARGS_STAND_INS``
lists the deliberate exceptions with their reason, as a **shrinking**
allowlist (the discipline of tests/test_module_layering.py): a new stand-in
fails, and a converted exception must be removed from it.
"""
import argparse
import ast
import collections
import dataclasses
from pathlib import Path

from sysforge.build.makepkg_wrapper import BuildOptions
from sysforge.pipeline.stages.base import RunOptions
from sysforge.verbs.registry import build_parser

TESTS = Path(__file__).resolve().parent

# Deliberate exceptions, as file name -> (stand-ins it keeps, why). Empty, and
# that is the steady state: the last one tested a getattr fallback no caller
# needed, and went with it. Every parsed command line is built with
# tests/cli_args.cli_args; converting them found 3.5.0-B129.
_ARGS_STAND_INS: dict[str, tuple[int, str]] = {}


def _stand_ins():
    """(file, line, keyword names) for every SimpleNamespace(...) call in tests/."""
    for py in sorted(TESTS.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            func = getattr(node, "func", None)
            if isinstance(node, ast.Call) and \
                    getattr(func, "id", getattr(func, "attr", None)) == "SimpleNamespace":
                yield py, node.lineno, {k.arg for k in node.keywords if k.arg}


def _command_dests():
    """(global dests, [each leaf command's own dests])."""
    root = build_parser()

    def leaves(p):
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            yield {a.dest for a in p._actions if a.dest != "help"}
        for a in subs:
            for s in a.choices.values():
                yield from leaves(s)

    def every(p):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for s in a.choices.values():
                    yield from every(s)
            else:
                yield a.dest

    global_dests = {a.dest for a in root._actions}
    return global_dests, set(every(root)), [d - global_dests for d in leaves(root)]


def test_no_stand_in_for_build_or_run_options():
    _, cli, _ = _command_dests()
    bad = []
    for cls in (BuildOptions, RunOptions):
        fields = {f.name for f in dataclasses.fields(cls)}
        internal = fields - cli
        for py, line, keys in _stand_ins():
            if keys and keys <= fields and keys & internal:
                bad.append(f"{py.name}:{line}: use {cls.__name__}(...) "
                           f"(fields {sorted(keys & internal)})")
    assert not bad, "SimpleNamespace stand-ins for an options dataclass:\n  " + "\n  ".join(bad)


def test_parsed_argument_stand_ins_only_shrink():
    global_dests, _, commands = _command_dests()
    found = collections.Counter()
    for py, _line, keys in _stand_ins():
        keys -= global_dests
        if len(keys) >= 2 and any(keys <= d for d in commands):
            found[py.name] += 1
    allowed = {f: n for f, (n, _why) in _ARGS_STAND_INS.items()}
    grown = {f: n for f, n in found.items() if n > allowed.get(f, 0)}
    assert not grown, (
        "new SimpleNamespace stand-ins for parsed arguments; build them with "
        f"tests/cli_args.cli_args(...) instead: {grown}")
    shrunk = {f: (n, found.get(f, 0)) for f, n in allowed.items()
              if found.get(f, 0) < n}
    assert not shrunk, f"lower these _ARGS_STAND_INS counts (listed, now): {shrunk}"


def test_stand_in_allowlist_is_empty():
    """The shrinking-allowlist check passes vacuously on an empty list, so this
    pins it shut, as tests/test_module_layering.py does. Re-adding an entry
    needs a reason a real object cannot serve."""
    assert _ARGS_STAND_INS == {}
