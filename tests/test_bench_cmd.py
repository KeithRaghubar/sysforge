# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`sysforge bench toolchain` verb: flow, exit codes, persistence (3.4.0-F6)."""
import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysforge import bench_cmd
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
    monkeypatch.setattr(bench_cmd, "_clang_present", lambda: True)
    monkeypatch.setattr(bench_cmd, "installed_version", lambda name: env["installed"])
    monkeypatch.setattr(
        bench_cmd, "_build_entry",
        lambda state_dir: (
            {"build_mode": env["build_mode"], "pkgver": env["recorded"][0],
             "pkgrel": env["recorded"][1], "epoch": env["recorded"][2]}
            if env["build_mode"] else None))
    monkeypatch.setattr(bench_cmd, "_load_tcfg", lambda: None)
    monkeypatch.setattr(sp, "resolve", lambda names: [
        sp.StockPkg(n, env["stock"], "extra", f"{n}.pkg.tar.zst", "https://m", "x")
        for n in names])
    monkeypatch.setattr(sp, "obtain",
                        lambda pkg, workdir, offline: Path(workdir) / pkg.filename)
    monkeypatch.setattr(sp, "extract", lambda archives, prefix: None)
    monkeypatch.setattr(bench_cmd, "_probe_stock", lambda prefix: None)

    def fake_run(jobs, runs):
        env["jobs"] = jobs
        return env["samples"]

    monkeypatch.setattr(bench_cmd, "_run_jobs", fake_run)
    return env


def test_success_reports_ratio_and_persists(tmp_path, happy, capsys):
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "stock/sysforge: 1.20x" in err and "built-in synthetic TUs" in err
    r = bs.load(tmp_path, "toolchain")
    assert r.ratio == pytest.approx(4.2 / 3.5, abs=1e-4) and r.runs == 3


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
    def fail(jobs, runs):
        raise bh.BenchFailed("stock", ["clang", "x.c"], "error: boom")

    monkeypatch.setattr(bench_cmd, "_run_jobs", fail)
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 1
    assert not (tmp_path / bs.FILENAME).exists()


def test_stock_built_clang_exits_2(tmp_path, happy):
    happy["build_mode"] = "pacman"
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 2
    happy["build_mode"] = None
    assert bench_cmd.cmd_bench_toolchain(_args(tmp_path)) == 2


def test_bad_configured_corpus_exits_1(tmp_path, happy, monkeypatch):
    monkeypatch.setattr(bench_cmd, "_load_tcfg",
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
