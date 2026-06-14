# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
env_cmd.py — ``sysforge env`` verb.

Read-only: prints the inherited environment chain (shell → profile →
makepkg.conf) and the points where those layers diverge. Dispatched through
the Verb framework; the argparse surface lives in ``cli._add_env_parser``.
"""
from sysforge import log
from sysforge.verbs import ExecResult, PreCheckResult, Verb


class EnvVerb(Verb):
    """Read-only: print the inherited env chain and divergences."""

    name = "env"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.primitives.env_chain import collect_env_chain, format_env_chain
        print(format_env_chain(collect_env_chain(), verbosity=log.get_verbosity()))
        return ExecResult()
