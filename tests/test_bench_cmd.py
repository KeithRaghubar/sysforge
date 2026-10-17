# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`sysforge bench toolchain` verb: flow, exit codes, persistence (3.4.0-F6)."""
import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysforge import bench_cmd, bench_core
from sysforge.primitives import bench_harness as bh
from sysforge.primitives import bench_state as bs
from sysforge.primitives import stock_pkg as sp


def _args(tmp_path, runs=3, offline=False):
    return argparse.Namespace(runs=runs, offline=offline, state_dir=str(tmp_path))


@pytest.fixture
def happy(tmp_path, monkeypatch):
    """Stub every external seam; return a dict the test can tweak."""
    env = {"installed": "23.1.1-1", "stock": "23.1.1-1", "build_mode": "pgo",
           "recorded": ("23.1.1", "1", "0"),
           "jobs": None, "samples": {"stock": [4.0, 4.2, 4.4], "sysforge": [3.4, 3.5, 3.6]}}
    monkeypatch.setattr(bench_core, "_clang_present", lambda: True)
    monkeypatch.setattr(bench_core, "installed_version", lambda name: env["installed"])
    monkeypatch.setattr(
        bench_core, "_build_entry",
        lambda state_dir: (
            {"build_mode": env["build_mode"], "pkgver": env["recorded"][0],
             "pkgrel": env["recorded"][1], "epoch": env["recorded"][2],
             "built_at": "2026-10-10T08:00:00Z"}
            if env["build_mode"] else None))
    monkeypatch.setattr(bench_core, "load_tcfg", lambda: None)
    monkeypatch.setattr(sp, "resolve", lambda names, dbpath=None: [
        sp.StockPkg(n, env["stock"], "extra", f"{n}.pkg.tar.zst", "https://m", "x")
        for n in names])
    monkeypatch.setattr(sp, "obtain",
                        lambda pkg, workdir, offline: Path(workdir) / pkg.filename)
    monkeypatch.setattr(sp, "extract", lambda archives, prefix, **kw: None)
    monkeypatch.setattr(bench_core, "_probe_stock", lambda prefix: None)

    def fake_run(jobs, runs, on_round=None):
        env["jobs"] = jobs
        if on_round:
            on_round("warm-up")
            for i in range(1, runs + 1):
                on_round(f"round {i}/{runs}")
        return env["samples"]

    monkeypatch.setattr(bench_core, "_run_jobs", fake_run)
    return env


def test_success_reports_ratio_and_persists(tmp_path, happy, capsys):
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "stock/sysforge: 1.20x" in err and "built-in synthetic TUs" in err
    r = bs.load(tmp_path, "toolchain")
    assert r.ratio == pytest.approx(4.2 / 3.5, abs=1e-4) and r.runs == 3
    # 3.4.0-B18: dated by the build it measured, not only by its version
    assert r.built_at == "2026-10-10T08:00:00Z"


def test_stock_job_uses_the_extracted_prefix_libs(tmp_path, happy):
    bench_cmd.cmd_bench_toolchain(_args(tmp_path))
    stock, mine = happy["jobs"]["stock"], happy["jobs"]["sysforge"]
    assert stock.env["LD_LIBRARY_PATH"].endswith("/stock/usr/lib")
    assert all("/stock/usr/bin/clang" in a[0] for a in stock.argvs)
    assert all(a[0].startswith("/usr/bin/clang") for a in mine.argvs)
    assert mine.env is None


def test_versions_differ_note(tmp_path, happy, capsys):
    happy["installed"] = "23.1.2-1"
    happy["recorded"] = ("23.1.2", "1", "0")
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "versions differ" in err and "stock/sysforge" in err


def test_major_gap_prints_no_ratio(tmp_path, happy, capsys):
    happy["stock"] = "22.1.8-1"
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "no ratio" in err and "stock/sysforge:" not in err
    assert bs.load(tmp_path, "toolchain").ratio is None


def test_stock_extraction_failure_exits_1_and_persists_nothing(tmp_path, happy, monkeypatch):
    def offline_miss(pkg, workdir, offline):
        raise sp.StockUnavailable(
            "clang.pkg.tar.zst is not in the pacman cache and --offline was given")

    monkeypatch.setattr(sp, "obtain", offline_miss)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path, offline=True)) == 1
    assert not (tmp_path / bs.FILENAME).exists()


def test_compile_failure_exits_1_and_persists_nothing(tmp_path, happy, monkeypatch):
    def fail(jobs, runs, on_round=None):
        raise bh.BenchFailed("stock", ["clang", "x.c"], "error: boom")

    monkeypatch.setattr(bench_core, "_run_jobs", fail)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 1
    assert not (tmp_path / bs.FILENAME).exists()


def test_stock_built_clang_exits_2(tmp_path, happy):
    happy["build_mode"] = "pacman"
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 2
    happy["build_mode"] = None
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 2


def test_bad_configured_corpus_exits_1(tmp_path, happy, monkeypatch):
    monkeypatch.setattr(bench_core, "load_tcfg",
                        lambda: SimpleNamespace(bench_corpus=(str(tmp_path / "nope"),)))
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 1
    assert not (tmp_path / bs.FILENAME).exists()


def test_unwritable_state_dir_still_exits_0(tmp_path, happy, monkeypatch, capsys):
    def denied(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(bs, "save", denied)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    assert "stock/sysforge" in capsys.readouterr().err


def test_default_verbosity_is_quiet(tmp_path, happy, quiet_at_default):
    err_v0, _ = quiet_at_default(lambda: bench_cmd.cmd_bench_toolchain(_args(tmp_path)))
    assert "stock/sysforge" in err_v0


def test_parser_runs_default_and_rejects_zero():
    from sysforge.cli import _build_parser
    p = _build_parser()
    args = p.parse_args(["bench", "toolchain"])
    assert args.runs == 5 and args.offline is False
    assert p.parse_args(["bench", "toolchain", "--runs", "2", "--offline"]).runs == 2
    with pytest.raises(SystemExit):
        p.parse_args(["bench", "toolchain", "--runs", "0"])


def test_bench_is_in_the_inspect_tier():
    from sysforge.verbs import registry
    assert "bench" in dict(registry._COMMAND_TIERS)["Inspect"]


def test_stock_clang_reinstalled_behind_the_record_exits_2(tmp_path, happy, capsys):
    """build_state still says sysforge built 23.1.1-1, but pacman has a different
    clang installed: timing it would report stock vs stock as the gain."""
    happy["installed"] = "23.1.2-1"
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 2
    assert "not the build sysforge recorded" in capsys.readouterr().err
    assert not (tmp_path / bs.FILENAME).exists()


def test_recorded_epoch_is_matched(tmp_path, happy):
    happy["installed"] = "1:23.1.1-1"
    happy["stock"] = "1:23.1.1-1"
    happy["recorded"] = ("23.1.1", "1", "1")
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0


def test_phase_line_names_each_step_then_clears_for_the_report(tmp_path, happy, monkeypatch):
    """3.4.0-F9: fetching, unpacking and every round each set the progress phase,
    and the phase is cleared before the report so the report replaces it."""
    from sysforge.ui import progress
    seen = []
    monkeypatch.setattr(progress, "phase", seen.append)
    monkeypatch.setattr(bench_core, "_report", lambda *a: seen.append("REPORT"))
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path, runs=2)) == 0
    assert seen == [
        "bench toolchain: fetching stock clang 23.1.1-1",
        "bench toolchain: fetching stock llvm-libs 23.1.1-1",
        "bench toolchain: unpacking stock clang",
        "bench toolchain: warm-up",
        "bench toolchain: round 1/2",
        "bench toolchain: round 2/2",
        None,
        "REPORT",
    ]


def test_phase_line_is_cleared_when_the_bench_fails(tmp_path, happy, monkeypatch):
    from sysforge.ui import progress
    seen = []
    monkeypatch.setattr(progress, "phase", seen.append)

    def offline_miss(pkg, workdir, offline):
        raise sp.StockUnavailable("not cached")

    monkeypatch.setattr(sp, "obtain", offline_miss)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 1
    assert seen[-1] is None



# --- stale sync DB: private refresh (3.5.0-F1) -----------------------------


def _stale_db(monkeypatch, env, *, sync_ok=True):
    """System DB names 23.1.3-2, which every mirror has dropped; a fresh private
    sync names 23.1.3-3. Returns the list of private_sync calls."""
    synced = []

    def resolve(names, dbpath=None):
        ver = "23.1.3-3" if dbpath else "23.1.3-2"
        return [sp.StockPkg(n, ver, "extra", f"{n}-{ver}.pkg.tar.zst", "https://m", "x")
                for n in names]

    def obtain(pkg, workdir, offline):
        if pkg.version == "23.1.3-2":
            raise sp.StockDownloadFailed(f"could not download {pkg.filename} (404)")
        return Path(workdir) / pkg.filename

    def private_sync(workdir):
        synced.append(workdir)
        if not sync_ok:
            raise sp.StockUnavailable("error: failed to synchronize all databases")
        return Path(workdir) / "db"

    monkeypatch.setattr(sp, "resolve", resolve)
    monkeypatch.setattr(sp, "obtain", obtain)
    monkeypatch.setattr(sp, "private_sync", private_sync)
    env["installed"] = env["stock"] = "23.1.3-3"
    env["recorded"] = ("23.1.3", "3", "0")
    return synced


def test_stale_sync_db_refreshes_privately_and_benches_the_current_build(
        tmp_path, happy, monkeypatch, capsys):
    synced = _stale_db(monkeypatch, happy)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    assert len(synced) == 1
    assert "stock     clang 23.1.3-3" in capsys.readouterr().err
    assert bs.load(tmp_path, "toolchain").stock_version == "23.1.3-3"


def test_failed_private_refresh_exits_1_and_points_at_pacman_syu(
        tmp_path, happy, monkeypatch, capsys):
    _stale_db(monkeypatch, happy, sync_ok=False)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "404" in err and "failed to synchronize" in err
    assert "pacman -Syu" in err
    assert bs.load(tmp_path, "toolchain") is None


def test_offline_miss_never_refreshes_the_sync_db(tmp_path, happy, monkeypatch):
    synced = []
    monkeypatch.setattr(sp, "private_sync", lambda workdir: synced.append(workdir))

    def offline_miss(pkg, workdir, offline):
        raise sp.StockUnavailable("not in the pacman cache and --offline was given")

    monkeypatch.setattr(sp, "obtain", offline_miss)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path, offline=True)) == 1
    assert synced == []


def test_fresh_sync_db_never_refreshes(tmp_path, happy, monkeypatch):
    synced = []
    monkeypatch.setattr(sp, "private_sync", lambda workdir: synced.append(workdir))
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    assert synced == []


def test_stale_attempt_drops_its_downloads_before_refreshing(tmp_path, happy, monkeypatch):
    """clang downloaded, llvm-libs 404'd: the stale clang archive must not sit
    on the tmpfs next to the fresh one."""
    left = []

    def resolve(names, dbpath=None):
        ver = "23.1.3-3" if dbpath else "23.1.3-2"
        return [sp.StockPkg(n, ver, "extra", f"{n}-{ver}.pkg.tar.zst", "https://m", "x")
                for n in names]

    def obtain(pkg, workdir, offline):
        if pkg.name == "llvm-libs" and pkg.version == "23.1.3-2":
            raise sp.StockDownloadFailed("404")
        path = Path(workdir) / pkg.filename
        path.write_bytes(b"x")
        return path

    def private_sync(workdir):
        left.extend(p.name for p in Path(workdir).glob("*.pkg.tar.zst"))
        return Path(workdir) / "db"

    monkeypatch.setattr(sp, "resolve", resolve)
    monkeypatch.setattr(sp, "obtain", obtain)
    monkeypatch.setattr(sp, "private_sync", private_sync)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    assert left == []
