# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_cmd.py — ``sysforge bench``: the argparse surface (3.4.0-F6).

The engine lives in ``bench_core.py`` so ``state profiles`` and the toolchain
stage can reach it without one verb module importing another.
"""
from __future__ import annotations

import argparse

from sysforge import bench_core
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb, VerbGroup

DEFAULT_RUNS = bench_core.DEFAULT_RUNS


def cmd_bench_toolchain(args) -> int:
    from sysforge.primitives.paths import resolve_state_dir

    state_dir = resolve_state_dir(getattr(args, "state_dir", None))[0]
    return bench_core.run_toolchain_bench(
        state_dir,
        runs=int(getattr(args, "runs", DEFAULT_RUNS)),
        offline=bool(getattr(args, "offline", False)),
        tcfg=bench_core.load_tcfg(),
    )


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


class BenchToolchainVerb(Verb):
    """Time the installed clang against the stock repo clang (3.4.0-F6)."""

    name = "bench-toolchain"
    requires_sentinel = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("toolchain",
            help="Compile a fixed workload with the stock repo clang and the installed "
                 "sysforge clang, interleaved; report the median, spread and ratio. "
                 "Read-only: the stock build is unpacked to a temp dir.")
        p.add_argument("--runs", type=_positive_int, default=DEFAULT_RUNS, metavar="N",
            help=f"Timed rounds per compiler (default {DEFAULT_RUNS}).")
        p.add_argument("--offline", action="store_true", dest="offline",
            help="Use the stock package only from the pacman cache; never download.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory (where bench.toml is recorded).")
        p.set_defaults(verb_cls=cls)
        return p

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        return ExecResult(exit_code=cmd_bench_toolchain(args))


BENCH_GROUP = VerbGroup(
    name="bench",
    help="Measure whether an optimized build is faster than stock (report only).",
    dest="bench_target",
    metavar="TARGET",
    members=(BenchToolchainVerb,),
)
