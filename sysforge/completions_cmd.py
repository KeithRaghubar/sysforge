# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
completions_cmd.py — ``sysforge completions`` verb.

A shell-completion data sink: prints candidate names for a requested resource
(``makepkg-flags`` / ``state`` / ``manifest`` / ``local`` / the default
repo+AUR package universe), one per line, for the ``_sysforge`` completion
script to consume. Not user-facing.

A bare ``sysforge completions …`` never reaches this verb: ``cli.main`` sends it
to :func:`sysforge.completions_data.main` before the registry loads
(3.5.0-B124). The verb keeps the command in the parser, so it is still hidden
from ``--help``, and serves the rare call that puts a global flag first
(``sysforge -v completions local``). Both routes print through
:func:`completions_data.emit`.
"""
import sysforge.completions_data as completions_data
from sysforge.verbs import ExecResult, PreCheckResult, Verb


class CompletionsVerb(Verb):
    """Shell-completion data sink. Not user-facing; called from _sysforge."""

    name = "completions"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("completions")
        completions_data.add_arguments(p)
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=completions_data.emit(args))
