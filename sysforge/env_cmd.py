# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
env_cmd.py — ``sysforge env`` verb.

Read-only: prints the inherited environment chain (shell → profile →
makepkg.conf) and the points where those layers diverge. Dispatched through
the Verb framework; the argparse surface is ``EnvVerb.add_parser``.
"""
from sysforge import log
from sysforge.verbs import ExecResult, PreCheckResult, Verb


class EnvVerb(Verb):
    """Read-only: print the inherited env chain and divergences."""

    name = "env"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("env",
            help="Print the inherited env chain, all contributing sources "
                 "(shell init files, systemd-user, PAM env, sysforge "
                 "[defaults] profile), and a mismatches block when sources "
                 "disagree. -vv adds inline per-var divergence annotations.")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.primitives.env_chain import collect_env_chain, format_env_chain
        print(format_env_chain(collect_env_chain(), verbosity=log.get_verbosity()))
        return ExecResult()
