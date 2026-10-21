# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""CLI spelling conventions, checked over the real parser tree (3.5.0-F15).

The flag vocabulary drifted verb by verb: `--no-pkg-log` beside
`--no-pkg-logs`, four spellings of "don't ask" (`--yes`, `--no-confirm`,
`--force`), `--non-interactive` beside `--no-update`, and group parents that
accepted their default subcommand's flags only to drop them silently once a
subcommand was named. Each test pins one rule (§CLI Verb Framework, "Flag
conventions") so a new flag cannot reintroduce the drift.
"""
import argparse
import re

from sysforge.verbs.registry import build_parser


def _commands(parser=None, prefix=""):
    """(qualified name, parser, help) for every visible subcommand."""
    parser = parser or build_parser()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helps = {ca.dest: ca.help for ca in action._choices_actions}
        for name, sub in action.choices.items():
            if helps.get(name) is None:  # hidden plumbing (`completions`)
                continue
            yield f"{prefix}{name}", sub, helps[name]
            yield from _commands(sub, f"{prefix}{name} ")


def _actions(sub):
    return [a for a in sub._actions
            if a.dest != "help" and not isinstance(a, argparse._SubParsersAction)]


def _flags():
    """(command, option string) for every option of every command."""
    for name, sub, _ in _commands():
        for a in _actions(sub):
            for opt in a.option_strings:
                yield name, opt


def test_help_text_is_sentence_case():
    """Command and option help reads as a sentence: capital first, closed by
    `.` (or a closing paren). --help and sysforge(1) both print it verbatim."""
    bad = []
    for name, sub, help_txt in _commands():
        texts = [(name, help_txt or "")] + [
            (f"{name} {'/'.join(a.option_strings) or a.dest}", a.help)
            for a in _actions(sub) if a.help]
        for where, text in texts:
            if text and (text[0].islower() or not text.rstrip().endswith((".", ")"))):
                bad.append(f"{where}: {text[:60]!r}")
    assert not bad, "help text not in sentence case:\n  " + "\n  ".join(bad)


def test_positionals_have_an_uppercase_metavar():
    bad = [f"{name} {a.dest}" for name, sub, _ in _commands()
           for a in _actions(sub)
           if not a.option_strings and not (a.metavar and a.metavar.isupper())]
    assert not bad, "positionals without an uppercase metavar: " + ", ".join(bad)


def test_no_flag_differs_from_another_only_by_a_plural_s():
    names = {opt for _, opt in _flags() if opt.startswith("--")}
    clashes = sorted(f"{n} / {n}s" for n in names if f"{n}s" in names)
    assert not clashes, f"singular/plural flag pairs: {clashes}"


def test_prompt_skip_is_spelled_yes():
    """Skipping a confirmation is `--yes` everywhere; `--force` means
    override a guard, never "don't ask"."""
    bad = [f"{name} {opt}" for name, opt in _flags()
           if opt in ("--no-confirm", "--noconfirm", "--assume-yes", "-y")]
    assert not bad, f"use --yes to skip a confirmation: {bad}"


def test_negative_flags_use_the_no_prefix():
    bad = [f"{name} {opt}" for name, opt in _flags()
           if re.match(r"--(non|not|dont|disable)-", opt)]
    assert not bad, f"spell a negative flag --no-<thing>: {bad}"


def test_group_parents_take_no_options():
    """A group parent's flag is silently dropped once a subcommand is named
    (argparse lets the subparser's defaults overwrite it): `packages
    --packages X add foo` wrote to the default file. Flags live on the
    subcommand only."""
    bad = []
    for name, sub, _ in _commands():
        has_sub = any(isinstance(a, argparse._SubParsersAction) for a in sub._actions)
        if has_sub:
            bad += [f"{name} {'/'.join(a.option_strings)}"
                    for a in _actions(sub) if a.option_strings]
    assert not bad, f"options on a group parent: {bad}"


def test_global_quiet_is_not_shadowed_by_a_local_flag():
    """The global --quiet is hoisted from anywhere on the command line, so a
    command-local --quiet/-q would be unreachable or ambiguous."""
    bad = [f"{name} {opt}" for name, opt in _flags() if opt in ("--quiet", "-q")]
    assert not bad, f"command-local --quiet shadows the global flag: {bad}"
