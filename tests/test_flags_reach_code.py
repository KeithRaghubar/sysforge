# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Every parsed flag reaches the code that acts on it (3.5.0-DEV17).

`run kernel --keep-hotplug-drivers` (3.5.0-B129) was parsed, then read by the
stage as ``getattr(options, "keep_hotplug_drivers", None)`` off a RunOptions
that had no such field, so the read always returned the default. Two checks
cover that shape for every command:

* a field read on an options object, by attribute or by ``getattr`` with a
  literal name, is a real RunOptions or BuildOptions field;
* every command's parsed flag is read through its argument object somewhere in
  the package (``args.x``, ``run.args.x``, ``getattr(args, "x")``, ...), or by
  one of the few indirect readers named below.
"""
import argparse
import ast
import dataclasses
import re
from pathlib import Path

from sysforge.build.makepkg_wrapper import BuildOptions
from sysforge.pipeline.stages.base import RunOptions
from sysforge.verbs.registry import build_parser

PKG = Path(__file__).resolve().parent.parent / "sysforge"
_OPTION_NAMES = {"options", "opts", "run_options", "build_options"}


def _sources():
    return {p: p.read_text(encoding="utf-8") for p in sorted(PKG.rglob("*.py"))}


def _bad_option_reads(text: str, where: str) -> list[str]:
    """Reads of a name that is no RunOptions/BuildOptions field, off an object
    named like an options object."""
    fields = {f.name for cls in (BuildOptions, RunOptions) for f in dataclasses.fields(cls)}
    tree = ast.parse(text)
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    bad = []
    for node in ast.walk(tree):
        target: ast.AST | None = None
        name = ""
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            target, name = node.args[0], node.args[1].value
        elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and id(node) not in called):      # `options.append(...)` is a list
            target, name = node.value, node.attr
        holder = getattr(target, "id", None) or getattr(target, "attr", None)
        if holder in _OPTION_NAMES and name not in fields and not name.startswith("_"):
            bad.append(f"{where}:{getattr(node, 'lineno', 0)}: {holder}.{name}")
    return bad


def test_options_reads_name_real_fields():
    bad = [b for path, text in _sources().items()
           for b in _bad_option_reads(text, str(path.relative_to(PKG.parent)))]
    assert not bad, ("options objects read fields RunOptions/BuildOptions do not have, "
                     "so the read always returns its default:\n  " + "\n  ".join(bad))


def test_the_options_check_sees_the_b129_shape():
    src = ('def f(options):\n'
           '    cli = getattr(options, "keep_hotplug_drivrs", None)\n'
           '    options.append(1)\n'
           '    return options.dry_run, options.no_such\n')
    assert _bad_option_reads(src, "x") == ["x:2: options.keep_hotplug_drivrs",
                                           "x:4: options.no_such"]


# Flags read without their own name in the source: the namespace dispatch
# fields, doctor's axis flags (looked up through _AXIS_FLAGS), and the
# packages.toml key the `packages add` flag is named after.
def _indirect_reads():
    from sysforge import doctor
    from sysforge.primitives.config import PKG_KEY_BUILD_FROM_SOURCE
    return set(doctor._AXIS_FLAGS) | {PKG_KEY_BUILD_FROM_SOURCE}


def _commands(parser, path=()):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, sub in a.choices.items():
                yield path + (name,), sub, a.dest
                yield from _commands(sub, path + (name,))


def test_every_parsed_flag_is_read():
    source = "\n".join(_sources().values())
    root = build_parser()
    global_dests = {a.dest for a in root._actions}
    dispatch = {dest for *_, dest in _commands(root)}
    indirect = _indirect_reads()
    unread = []
    for path, sub, _ in _commands(root):
        for a in sub._actions:
            d = a.dest
            if (isinstance(a, argparse._SubParsersAction) or d in ("help", "verb_cls")
                    or d in global_dests or d in dispatch or d in indirect):
                continue
            if not re.search(rf'\.args\.{d}\b|\bargs\.{d}\b|getattr\(\s*[\w.]*args\s*,\s*"{d}"'
                             rf'|resolve_flag_default\(\s*[\w.]*args\s*,\s*"{d}"', source):
                unread.append(f"sysforge {' '.join(path)}: {d}")
    assert not unread, "parsed but never read:\n  " + "\n  ".join(sorted(set(unread)))


def test_doctor_axis_flags_are_the_indirect_ones():
    """The doctor exemption above is only sound while _AXIS_FLAGS names the
    axis flags exactly."""
    from sysforge import doctor
    root = build_parser()
    doctor_sub = next(a for a in root._actions
                      if isinstance(a, argparse._SubParsersAction)).choices["doctor"]
    axis_dests = set()
    for a in doctor_sub._actions:
        if isinstance(a, argparse._SubParsersAction):
            for sub in a.choices.values():
                axis_dests |= {x.dest for x in sub._actions if x.dest in doctor._AXIS_FLAGS}
    assert axis_dests == set(doctor._AXIS_FLAGS)
