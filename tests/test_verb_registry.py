# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Guards on ``verbs/registry.py``, the one list the parser is built from (3.2.0-F7).

Each verb owns its argparse surface through ``Verb.add_parser``. A verb class
that exists but is missing from ``COMMANDS`` (directly or through a
``VerbGroup``) has no command line at all, and nothing else would notice."""
import argparse
import ast
import re
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


# `sysforge` followed by words, placeholders and flags, up to anything else
# (a pipe, a closing backtick, punctuation).
_CMD = re.compile(
    r"\bsysforge((?:[ \t]+(?:--?[A-Za-z][\w-]*(?:=[^\s`'\"]*)?|<[^>\s]+>|[A-Za-z][\w.+-]*))+)")
# Where a user can be told to run a command. Design docs, ROADMAP and release
# notes describe old spellings on purpose and are left out.
_DOC_FILES = ("README.md", "CLAUDE.md", "sysforge/CLAUDE.md", "man/sysforge.1.scd.in")
_DOC_GLOBS = ("docs/guides/*.md", "docs/concepts/*.md", "etc/sysforge/*.toml")


def _texts():
    """(where, text) for every place a command can be written down: string
    literals, docstrings and comments under sysforge/, and the user docs."""
    import io
    import tokenize
    for py in sorted((REPO_ROOT / "sysforge").rglob("*.py")):
        rel = py.relative_to(REPO_ROOT)
        src = py.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                yield f"{rel}:{tok.start[0]}", tok.string
    docs = [REPO_ROOT / f for f in _DOC_FILES]
    for pattern in _DOC_GLOBS:
        docs += sorted(REPO_ROOT.glob(pattern))
    for doc in docs:
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            yield f"{doc.relative_to(REPO_ROOT)}:{n}", line


def _subparsers(parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _check_command(root, tokens):
    """Problems with one written-down command, or [] when it is fine. Long
    flags only: build and update pass makepkg's short flags through."""
    if not tokens:
        return []
    import difflib

    def options(p):
        return {o: a for a in p._actions for o in a.option_strings}

    global_opts = options(root)
    first = tokens[0]
    if not first.startswith("-") and first not in _subparsers(root).choices:
        return []  # prose or a path ("sysforge builds", "sysforge -mindepth")
    parser, path, problems = root, [], []
    takes_value = False
    for tok in tokens:
        if takes_value:              # `--thaw mesa`: the flag's value
            takes_value = False
            continue
        if tok.startswith("--"):
            flag, eq, _ = tok.partition("=")
            action = (options(parser) | global_opts).get(flag)
            if action is None:
                problems.append(f"{' '.join(path) or '(top level)'} has no {flag}")
            else:
                takes_value = action.nargs != 0 and not eq
            continue
        if tok.startswith("-") or tok.startswith("<"):
            continue
        sub = _subparsers(parser)
        if sub is None:
            continue  # a positional (a package name, a path)
        if tok in sub.choices:
            parser = sub.choices[tok]
            path.append(tok)
        elif difflib.get_close_matches(tok, list(sub.choices), n=1, cutoff=0.8):
            # Close to a real one: a typo or a rename. Anything else is prose
            # ("sysforge config files on a packaged install") or a positional.
            problems.append(f"{' '.join(path)} has no subcommand {tok!r}")
            break
        else:
            break
    return problems


def test_every_written_command_names_real_flags_and_subcommands():
    """3.5.0-DEV11 (generalising 3.5.0-B3's doctor-only check): every
    `sysforge ...` the code or the user docs tell someone to run names
    subcommands and long flags that exist. B3 was `doctor --restart` printed
    after the flat form was removed; F15's renames left the same kind of
    leftover in comments."""
    root = build_parser()
    seen, bad = 0, []
    for where, text in _texts():
        for m in _CMD.finditer(text):
            seen += 1
            for problem in _check_command(root, m.group(1).split()):
                bad.append(f"{where}: sysforge{m.group(1)}  ({problem})")
    assert seen > 100, f"the scan found only {seen} commands"
    assert not bad, "written-down commands that do not exist:\n  " + "\n  ".join(bad)


def test_command_check_catches_the_known_failure_shapes():
    root = build_parser()
    assert _check_command(root, ["doctor", "--restart"])           # B3: flat doctor form
    assert _check_command(root, ["run", "kernel", "--compiler", "gcc"])  # F15 rename
    assert _check_command(root, ["config", "files", "on", "a"]) == []  # prose
    assert _check_command(root, ["--thaw", "mesa", "update"]) == []    # a global's value
    assert _check_command(root, ["run", "toolchian"])               # typo'd subcommand
    assert _check_command(root, ["build", "mesa", "--toolchain", "gcc"]) == []
    assert _check_command(root, ["build", "-sfCci", "mesa"]) == []  # makepkg pass-through
    assert _check_command(root, ["builds", "--anything"]) == []     # prose, not a command
