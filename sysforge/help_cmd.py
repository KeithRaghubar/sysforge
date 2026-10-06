# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
help_cmd.py — ``sysforge help [COMMAND ...]`` verb (2.5.0-F2).

Read-only alias for ``--help``: with no argument it prints top-level help,
otherwise it walks the subparser chain (``sysforge help state failed``) and
prints that parser's help — the same bytes ``sysforge state failed --help``
emits, because it is literally the same parser object. Dispatched through the
Verb framework (no sentinel, like ``env``/``resolve``); the argparse surface
is ``HelpVerb.add_parser``; the parser it walks comes from
``verbs.registry.build_parser``.

Help text goes to stdout via ``print_help()`` rather than ``log.ui`` so it
stays byte-identical to the ``--help`` flag and never lands in the log files.
"""
from __future__ import annotations

import argparse

from sysforge import log
from sysforge.verbs import ExecResult, PreCheckResult, Verb


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Return the parser's subcommand map, or ``{}`` if it has no subparsers."""
    action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    return dict(action.choices) if action is not None else {}


class HelpVerb(Verb):
    """Read-only: print top-level or per-command help."""

    name = "help"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("help",
            help="Print help for sysforge or for a COMMAND (alias for --help).")
        p.add_argument("topic", nargs="*", metavar="COMMAND",
            help="Command (and optional subcommand) to describe, e.g. "
                 "`sysforge help state failed`. Omit for top-level help.")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        # Function-level: the registry imports every verb module, this one included.
        from sysforge.verbs.registry import build_parser

        target = build_parser()
        trail: list[str] = []
        for word in getattr(args, "topic", None) or []:
            choices = _subparsers(target)
            if word not in choices:
                where = f"`sysforge {' '.join(trail)}`" if trail else "sysforge"
                known = ", ".join(sorted(c for c in choices if c != "completions"))
                log.error("HELP", f"unknown help topic {word!r} for {where}."
                                  + (f" Known: {known}" if known
                                     else " That command takes no subcommand."))
                return ExecResult(exit_code=2)
            target = choices[word]
            trail.append(word)

        target.print_help()
        return ExecResult()
