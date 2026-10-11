# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_cmd.py — ``sysforge bench``: measure an optimized build against stock (3.4.0-F6).

``bench toolchain`` times the installed sysforge-built clang against the stock
repo clang of the repo version, on the bench's own workload
(``primitives/bench_workload.py``), and reports the median, spread and
stock/sysforge ratio. Report only: no verdict and no threshold, because a
cutoff tuned on one host means nothing on another. Read-only
(``requires_sentinel=False``): the stock build is extracted into a temp prefix
by ``primitives/stock_pkg.py`` and nothing is installed or escalated. The last
result is recorded through ``primitives/bench_state.py`` for ``state profiles``.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from sysforge import log
from sysforge.primitives import bench_harness as bh
from sysforge.primitives import bench_state, bench_workload, run, stock_pkg
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb, VerbGroup

_log = log.get_logger("BENCH")

TARGET = "toolchain"
_STOCK_PKGS = ("clang", "llvm-libs")
_INSTALLED_CC = "/usr/bin/clang"
_INSTALLED_CXX = "/usr/bin/clang++"
DEFAULT_RUNS = 5


def installed_version(name: str) -> str | None:
    """``pacman -Q`` version of *name*, or ``None`` when not installed."""
    res = run.probe(["pacman", "-Q", name])
    parts = res.stdout.split() if res.returncode == 0 else []
    return parts[1] if len(parts) == 2 else None


def _recorded_version(entry: dict) -> str:
    """The ``[epoch:]pkgver-pkgrel`` a build_state entry recorded (pacman -Q shape)."""
    epoch = str(entry.get("epoch") or "0")
    ver = f"{entry.get('pkgver')}-{entry.get('pkgrel')}"
    return ver if epoch == "0" else f"{epoch}:{ver}"


def _clang_present() -> bool:
    return Path(_INSTALLED_CC).exists() and Path(_INSTALLED_CXX).exists()


def _build_entry(state_dir: Path) -> dict | None:
    from sysforge.primitives.build_state import BuildState
    return BuildState(state_dir).get("clang")


def _load_tcfg():
    """The typed toolchain.toml, or ``None`` (absent or unparseable: the defaults)."""
    from sysforge.pipeline.stages.toolchain import config as tconfig
    try:
        return tconfig.load()
    except RuntimeError as exc:
        _log.warn(str(exc))
        return None


def _corpus(tcfg) -> tuple[str, ...]:
    return tuple(getattr(tcfg, "bench_corpus", ()) or ())


def _workload(tcfg, tmp: Path) -> bench_workload.Workload:
    corpus = _corpus(tcfg)
    if corpus:
        return bench_workload.configured_workload(corpus)
    return bench_workload.default_workload(tmp)


def current_fingerprint(tcfg) -> str | None:
    """The fingerprint the bench would measure today (``state profiles`` staleness).

    ``None`` when the configured corpus no longer resolves: no current workload
    can match a stored result, so the caller treats it as stale.
    """
    corpus = _corpus(tcfg)
    if not corpus:
        return bench_workload.default_fingerprint()
    try:
        return bench_workload.configured_workload(corpus).fingerprint
    except ValueError:
        return None


def _stock_env(prefix: Path) -> dict[str, str]:
    # Without this the stock binary resolves libLLVM/libclang-cpp from the host,
    # which is the sysforge build: the comparison would silently be ~1.00x.
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(prefix / "usr/lib")
    return env


def _probe_stock(prefix: Path) -> None:
    res = run.probe([str(prefix / "usr/bin/clang"), "--version"], env=_stock_env(prefix))
    if res.returncode != 0:
        raise stock_pkg.StockUnavailable(
            f"the extracted stock clang does not run: {(res.stderr or '').strip()}")


def _jobs(workload: bench_workload.Workload, prefix: Path, objdir: Path) -> dict[str, bh.Job]:
    def argvs(cc: str, cxx: str, tag: str) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(bench_workload.compile_argv(cc, cxx, src, objdir / f"{tag}-{i}.o"))
            for i, src in enumerate(workload.files)
        )
    stock_cc = str(prefix / "usr/bin/clang")
    stock_cxx = str(prefix / "usr/bin/clang++")
    return {
        "stock": bh.Job(argvs(stock_cc, stock_cxx, "stock"), _stock_env(prefix)),
        "sysforge": bh.Job(argvs(_INSTALLED_CC, _INSTALLED_CXX, "sysforge")),
    }


def _run_jobs(jobs: dict[str, bh.Job], runs: int) -> dict[str, list[float]]:
    return bh.run_interleaved(jobs, runs)


def _fmt(stat: bh.Stat) -> str:
    return f"median {stat.median:.2f}s  ({stat.low:.2f}-{stat.high:.2f})"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _report(workload, runs, stock_ver, inst_ver, s_stat, i_stat, relation, ratio) -> None:
    _log.ui(f"corpus: {workload.label} ({_plural(len(workload.files), 'file')}) · "
            f"{_plural(runs, 'run')}, interleaved")
    _log.ui(f"stock     clang {stock_ver}  {_fmt(s_stat)}")
    _log.ui(f"sysforge  clang {inst_ver}  {_fmt(i_stat)}")
    if relation == bh.DIFFERS:
        _log.ui(f"versions differ (stock {stock_ver}, installed {inst_ver}): "
                "the ratio includes upstream changes, not only the optimization")
    if ratio is None:
        _log.ui(f"no ratio: stock {stock_ver} and installed {inst_ver} are different "
                "major versions, so the comparison would not isolate the optimization")
    else:
        _log.ui(f"stock/sysforge: {ratio:.2f}x")


def cmd_bench_toolchain(args) -> int:
    from sysforge.primitives.paths import resolve_state_dir

    state_dir = Path(resolve_state_dir(getattr(args, "state_dir", None))[0])
    runs = int(getattr(args, "runs", DEFAULT_RUNS))

    if not _clang_present():
        _log.error("bench toolchain: clang is not installed; nothing to measure")
        return 2
    entry = _build_entry(state_dir)
    if not entry or entry.get("build_mode") == "pacman":
        _log.error("bench toolchain: the installed clang is the stock build, so there "
                   "is nothing to compare (build one with `sysforge run toolchain`)")
        return 2
    inst_ver = installed_version("clang")
    if inst_ver is None:
        _log.error("bench toolchain: could not read the installed clang version")
        return 2
    recorded = _recorded_version(entry)
    if recorded != inst_ver:
        # Stage-owned entries are never auto-demoted, so a stock clang put back
        # by `pacman -S`/`-Syu` still has a sysforge record: timing it would
        # report stock against stock as the measured gain.
        _log.error(f"bench toolchain: the installed clang {inst_ver} is not the build "
                   f"sysforge recorded ({recorded}); it was replaced outside sysforge. "
                   "Rebuild it with `sysforge run toolchain` before benchmarking")
        return 2

    tcfg = _load_tcfg()
    with tempfile.TemporaryDirectory(prefix="sysforge-bench-") as td:
        tmp = Path(td)
        try:
            workload = _workload(tcfg, tmp)
        except ValueError as exc:
            _log.error(f"bench toolchain: {exc}")
            return 1
        offline = bool(getattr(args, "offline", False))
        try:
            pkgs = stock_pkg.resolve(list(_STOCK_PKGS))
            archives = [stock_pkg.obtain(p, tmp, offline=offline) for p in pkgs]
            prefix = tmp / "stock"
            stock_pkg.extract(archives, prefix)
            _probe_stock(prefix)
        except stock_pkg.StockUnavailable as exc:
            _log.error(f"bench toolchain: no stock clang to compare against: {exc}")
            return 1
        stock_ver = next(p.version for p in pkgs if p.name == "clang")

        objdir = tmp / "obj"
        objdir.mkdir()
        _log.info(f"timing {_plural(len(workload.files), 'file')} x "
                  f"{_plural(runs, 'run')} per compiler")
        try:
            samples = _run_jobs(_jobs(workload, prefix, objdir), runs)
        except bh.BenchFailed as exc:
            _log.error(f"bench toolchain: {exc}")
            return 1

    s_stat = bh.summarize(samples["stock"])
    i_stat = bh.summarize(samples["sysforge"])
    relation = bh.version_relation(stock_ver, inst_ver)
    ratio = bh.ratio(s_stat, i_stat, relation)
    _report(workload, runs, stock_ver, inst_ver, s_stat, i_stat, relation, ratio)

    try:
        bench_state.save(state_dir, TARGET, bench_state.BenchResult(
            installed_version=inst_ver, stock_version=stock_ver,
            corpus=workload.label, fingerprint=workload.fingerprint, runs=runs,
            stock_median_s=round(s_stat.median, 4),
            installed_median_s=round(i_stat.median, 4),
            ratio=None if ratio is None else round(ratio, 4),
            measured_at=bench_state.now_iso(),
        ))
    except OSError as exc:
        _log.warn(f"could not record the result in {state_dir}: {exc}")
    return 0


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
