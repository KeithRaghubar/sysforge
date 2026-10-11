# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Toolchain stage → bench hand-off after a verified install (3.4.0-F11)."""
from types import SimpleNamespace

import pytest

from sysforge import bench_core
from sysforge.pipeline.stages.toolchain import measure
from sysforge.pipeline.stages.toolchain.config import ToolchainConfig


def _opts(tmp_path, dry_run=False):
    return SimpleNamespace(dry_run=dry_run, state_dir=str(tmp_path))


def _tcfg(after_build):
    return ToolchainConfig.from_toml({"bench": {"after_build": after_build}})


@pytest.fixture
def bench_calls(monkeypatch):
    calls = []

    def fake(state_dir, **kw):
        calls.append((state_dir, kw))
        return 0

    monkeypatch.setattr(bench_core, "run_toolchain_bench", fake)
    return calls


def test_default_prints_the_hint_and_does_not_bench(tmp_path, bench_calls, capsys):
    measure.after_install(_tcfg(False), _opts(tmp_path))
    assert "sysforge bench toolchain" in capsys.readouterr().err
    assert bench_calls == []


def test_after_build_runs_the_bench_with_the_stage_config(tmp_path, bench_calls):
    tcfg = _tcfg(True)
    measure.after_install(tcfg, _opts(tmp_path))
    assert len(bench_calls) == 1
    state_dir, kw = bench_calls[0]
    assert str(state_dir) == str(tmp_path)
    assert kw["tcfg"] is tcfg and kw["offline"] is False


def test_dry_run_neither_hints_nor_benches(tmp_path, bench_calls, capsys):
    measure.after_install(_tcfg(True), _opts(tmp_path, dry_run=True))
    measure.after_install(_tcfg(False), _opts(tmp_path, dry_run=True))
    assert bench_calls == []
    assert "bench" not in capsys.readouterr().err


def test_a_bench_that_cannot_run_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    def no_stock(state_dir, *, fail, **kw):
        fail("bench toolchain: no stock clang to compare against: offline")
        return 1

    monkeypatch.setattr(bench_core, "run_toolchain_bench", no_stock)
    measure.after_install(_tcfg(True), _opts(tmp_path))
    err = capsys.readouterr().err
    assert "no stock clang" in err and "toolchain is installed" in err
    assert "[ERROR]" not in err


def test_an_unexpected_bench_crash_never_fails_the_stage(tmp_path, monkeypatch, capsys):
    def boom(state_dir, **kw):
        raise RuntimeError("bsdtar exploded")

    monkeypatch.setattr(bench_core, "run_toolchain_bench", boom)
    measure.after_install(_tcfg(True), _opts(tmp_path))
    assert "bsdtar exploded" in capsys.readouterr().err


def test_default_verbosity_is_quiet(tmp_path, bench_calls, quiet_at_default):
    err_v0, _ = quiet_at_default(lambda: measure.after_install(_tcfg(False), _opts(tmp_path)))
    assert "sysforge bench toolchain" in err_v0
