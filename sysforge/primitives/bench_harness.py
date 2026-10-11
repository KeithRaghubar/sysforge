# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_harness.py — interleaved compile timing and its statistics (3.4.0-F6).

Interleaving (stock, sysforge, stock, sysforge, …) spreads thermal and
page-cache drift across both compilers instead of loading it onto whichever ran
second. One untimed warm-up round per label primes the page cache. The numbers
are reported, never judged: there is no threshold here (no machine-calibrated
cutoffs).
"""
from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sysforge.primitives import run

SAME = "same"
DIFFERS = "differs"
MAJOR_GAP = "major-gap"


@dataclass(frozen=True)
class Job:
    """One compiler's round: argvs run in order, under *env* (``None`` = inherit)."""

    argvs: tuple[tuple[str, ...], ...]
    env: dict[str, str] | None = None


class BenchFailed(RuntimeError):
    """A compile in the workload exited non-zero; names the compiler and command."""

    def __init__(self, label: str, argv: Sequence[str], detail: str):
        self.label = label
        self.argv = list(argv)
        super().__init__(f"{label} compile failed: {' '.join(argv)}: {detail}")


@dataclass(frozen=True)
class Stat:
    median: float
    low: float
    high: float


def _default_runner(argv, env):
    return run.probe(list(argv), env=env)


def _round(label: str, job: Job, runner) -> None:
    for argv in job.argvs:
        res = runner(list(argv), job.env)
        if res.returncode != 0:
            tail = (res.stderr or "").strip().splitlines()[-3:]
            raise BenchFailed(label, argv, " / ".join(tail) or f"exit {res.returncode}")


def run_interleaved(
    jobs: dict[str, Job],
    runs: int,
    *,
    runner: Callable | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, list[float]]:
    """Wall time per round, per label: one warm-up, then *runs* alternating rounds."""
    if runs < 1:
        raise ValueError("runs must be >= 1")
    runner = runner or _default_runner
    for label, job in jobs.items():
        _round(label, job, runner)  # warm-up, untimed
    samples: dict[str, list[float]] = {label: [] for label in jobs}
    for _ in range(runs):
        for label, job in jobs.items():
            start = clock()
            _round(label, job, runner)
            samples[label].append(clock() - start)
    return samples


def summarize(samples: Sequence[float]) -> Stat:
    if not samples:
        raise ValueError("no samples")
    return Stat(statistics.median(samples), min(samples), max(samples))


_VER = re.compile(r"^(?:(?P<epoch>\d+):)?(?P<major>\d+)")


def _major(version: str) -> tuple[int, int]:
    m = _VER.match(version)
    if not m:
        return (0, -1)
    return (int(m.group("epoch") or 0), int(m.group("major")))


def version_relation(stock: str, installed: str) -> str:
    """``same`` / ``differs`` / ``major-gap`` over ``[epoch:]pkgver-pkgrel``."""
    if stock == installed:
        return SAME
    return DIFFERS if _major(stock) == _major(installed) else MAJOR_GAP


def ratio(stock: Stat, installed: Stat, relation: str) -> float | None:
    """Stock median over installed median; ``None`` across a major gap."""
    if relation == MAJOR_GAP or installed.median <= 0:
        return None
    return stock.median / installed.median
