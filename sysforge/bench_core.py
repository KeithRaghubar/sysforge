# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bench_core.py — the shared bench engine behind ``bench toolchain``,
``state profiles`` and the toolchain stage (3.4.0-F6, 3.4.0-F11).

``run_toolchain_bench`` times the installed sysforge-built clang against the
stock repo clang of the repo version, on the bench's own workload
(``primitives/bench_workload.py``), and reports the median, spread and
stock/sysforge ratio. Report only: no verdict and no threshold, because a
cutoff tuned on one host means nothing on another. Read-only: the stock build
is extracted into a temp prefix by ``primitives/stock_pkg.py`` and nothing is
installed or escalated. The last result is recorded through
``primitives/bench_state.py`` for ``state profiles``.

This is the one home, like ``build_core`` for ``build``/``update``: verb
modules are peers and may not import each other, so ``bench_cmd`` (the
argparse surface), ``state_cmd`` (the GAIN column) and the toolchain stage
(``[bench] after_build``) all call in here.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from sysforge import log
from sysforge.primitives import bench_harness as bh
from sysforge.primitives import bench_state, bench_workload, run, stock_pkg

_log = log.get_logger("BENCH")

TARGET = "toolchain"
_STOCK_PKGS = ("clang", "llvm-libs")
# Split out of clang in 23.1.3-4 (resource dir, libclang-cpp); a sync DB from
# before the split has no such package (3.5.0-B146).
_STOCK_OPTIONAL = ("clang-libs",)
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


def recorded_built_at(state_dir: Path) -> str | None:
    """build_state's ``built_at`` for clang: what dates the build a result measured."""
    entry = _build_entry(Path(state_dir)) or {}
    value = entry.get("built_at")
    return str(value) if value else None


def load_tcfg():
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


def current_fingerprint(tcfg, recorded: bench_state.BenchResult | None = None) -> str | None:
    """The fingerprint the bench would measure today (``state profiles`` staleness).

    ``None`` when the configured corpus no longer resolves: no current workload
    can match a stored result, so the caller treats it as stale. With a
    *recorded* result whose ``corpus_key`` still matches the corpus's paths,
    sizes and mtimes, its fingerprint is returned without reading the sources.
    """
    corpus = _corpus(tcfg)
    if not corpus:
        return bench_workload.default_fingerprint()
    try:
        files = bench_workload.corpus_files(corpus)
        if recorded and recorded.corpus_key and \
                bench_workload.stat_key(files) == recorded.corpus_key:
            return recorded.fingerprint
        return bench_workload.fingerprint(files)
    except (ValueError, OSError):
        return None


def _stock_env(prefix: Path) -> dict[str, str]:
    # Without this the stock binary resolves libLLVM/libclang-cpp from the host,
    # which is the sysforge build: the comparison would silently be ~1.00x.
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(prefix / "usr/lib")
    return env


def _probe_stock(prefix: Path) -> None:
    stock_pkg.check_complete(prefix)
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


def _run_jobs(jobs: dict[str, bh.Job], runs: int,
              on_round: Callable[[str], None] | None = None) -> dict[str, list[float]]:
    return bh.run_interleaved(jobs, runs, on_round=on_round)


def _phase(label: str | None) -> None:
    """Show *label* on the progress line (3.4.0-F9); ``None`` clears it.

    Without it the verb is silent from launch to report: a ~105 MB download,
    the extraction and every timed round read as a hang. The narration at
    ``info()`` stays as it was; this is the display, not the log.
    """
    from sysforge.ui import progress
    progress.phase(None if label is None else f"bench toolchain: {label}")


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


def _obtain_all(pkgs, tmp: Path, offline: bool) -> list[Path]:
    """Every archive of *pkgs*; on failure, drop what this attempt downloaded."""
    archives: list[Path] = []
    try:
        for p in pkgs:
            _phase(f"fetching stock {p.name} {p.version}")
            archives.append(stock_pkg.obtain(p, tmp, offline=offline))
    except stock_pkg.StockUnavailable:
        for a in archives:
            if a.parent == tmp:
                a.unlink(missing_ok=True)  # $TMPDIR is usually a tmpfs
        raise
    return archives


def _fetch_stock(tmp: Path, offline: bool):
    """``(pkgs, archives)`` for the stock build, past a stale sync DB (3.5.0-F1).

    When no mirror serves what the system sync DB names, the DB is behind the
    repo: retry once against a private fresh copy. ``--offline`` never gets
    here, because an offline miss is not a download failure.
    """
    pkgs = stock_pkg.resolve(list(_STOCK_PKGS), optional=list(_STOCK_OPTIONAL))
    try:
        return pkgs, _obtain_all(pkgs, tmp, offline)
    except stock_pkg.StockDownloadFailed as exc:
        _log.info(f"{exc}: the sync database looks stale; refreshing a private copy")
        _phase("refreshing a private copy of the sync database")
        try:
            dbpath = stock_pkg.private_sync(tmp)
        except stock_pkg.StockUnavailable as sync_exc:
            raise stock_pkg.StockUnavailable(
                f"{exc}; {sync_exc}. The sync database is probably stale: "
                "run `pacman -Syu`, then bench again") from exc
        pkgs = stock_pkg.resolve(list(_STOCK_PKGS), optional=list(_STOCK_OPTIONAL),
                                 dbpath=dbpath)
        return pkgs, _obtain_all(pkgs, tmp, offline)


def _measure(tcfg, runs: int, offline: bool, fail: Callable[[str], None]):
    """Fetch and unpack stock clang, then time both compilers.

    Returns ``(workload, stock_version, samples)``, or the exit code when a
    step fails (the failure already reported through *fail*).
    """
    with tempfile.TemporaryDirectory(prefix="sysforge-bench-") as td:
        tmp = Path(td)
        try:
            workload = _workload(tcfg, tmp)
        except ValueError as exc:
            fail(f"bench toolchain: {exc}")
            return 1
        try:
            pkgs, archives = _fetch_stock(tmp, offline)
            prefix = tmp / "stock"
            _phase("unpacking stock clang")
            stock_pkg.extract(archives, prefix, downloaded_in=tmp)
            _probe_stock(prefix)
        except stock_pkg.StockUnavailable as exc:
            fail(f"bench toolchain: no stock clang to compare against: {exc}")
            return 1
        stock_ver = next(p.version for p in pkgs if p.name == "clang")

        objdir = tmp / "obj"
        objdir.mkdir()
        _log.info(f"timing {_plural(len(workload.files), 'file')} x "
                  f"{_plural(runs, 'run')} per compiler")
        try:
            samples = _run_jobs(_jobs(workload, prefix, objdir), runs, on_round=_phase)
        except bh.BenchFailed as exc:
            fail(f"bench toolchain: {exc}")
            return 1
    return workload, stock_ver, samples


def run_toolchain_bench(state_dir: Path, *, runs: int = DEFAULT_RUNS,
                        offline: bool = False, tcfg=None,
                        fail: Callable[[str], None] | None = None) -> int:
    """Measure, report and record; return the exit code (0, 1 or 2).

    *tcfg* is the typed toolchain config (``None`` = defaults: built-in
    workload). *fail* receives each failure message; it defaults to
    ``error()``, and the toolchain stage passes its own so a bench that cannot
    run never reads as a failed toolchain install.
    """
    fail = fail or _log.error
    state_dir = Path(state_dir)

    if not _clang_present():
        fail("bench toolchain: clang is not installed; nothing to measure")
        return 2
    entry = _build_entry(state_dir)
    if not entry or entry.get("build_mode") == "pacman":
        fail("bench toolchain: the installed clang is the stock build, so there "
                   "is nothing to compare (build one with `sysforge run toolchain`)")
        return 2
    inst_ver = installed_version("clang")
    if inst_ver is None:
        fail("bench toolchain: could not read the installed clang version")
        return 2
    recorded = _recorded_version(entry)
    if recorded != inst_ver:
        # Stage-owned entries are never auto-demoted, so a stock clang put back
        # by `pacman -S`/`-Syu` still has a sysforge record: timing it would
        # report stock against stock as the measured gain.
        fail(f"bench toolchain: the installed clang {inst_ver} is not the build "
                   f"sysforge recorded ({recorded}); it was replaced outside sysforge. "
                   "Rebuild it with `sysforge run toolchain` before benchmarking")
        return 2

    try:
        measured = _measure(tcfg, runs, offline, fail)
    finally:
        _phase(None)  # the report replaces the phase line, on every exit path
    if isinstance(measured, int):
        return measured
    workload, stock_ver, samples = measured

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
            built_at=str(entry.get("built_at") or ""),
            corpus_key=workload.stat_key,
        ))
    except OSError as exc:
        _log.warn(f"could not record the result in {state_dir}: {exc}")
    return 0
