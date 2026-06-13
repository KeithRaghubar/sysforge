"""
test_state_failed.py — coverage for `sysforge state failed`.

Lists / clears the [failures] table of build_state.toml.
"""
from pathlib import Path
from types import SimpleNamespace

from sysforge import log
from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_failed


def _args(state_dir: Path, **kw):
    base = dict(state_dir=str(state_dir), no_pager=True, clear=None, clear_all=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _seed_failure(state_dir: Path, pkgbase: str, **fields):
    state_dir.mkdir(parents=True, exist_ok=True)
    bs = BuildState(state_dir)
    bs.record_failure(pkgbase, **fields)
    bs.save()


def test_empty_failed_list(tmp_path, capsys):
    cmd_state_failed(_args(tmp_path / "state"))
    out = capsys.readouterr().out
    assert "No build failures recorded" in out


def test_lists_failure_with_fix(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed_failure(
        state_dir, "gpu-burn-git",
        error="[build_failed] nvcc exit 4",
        signature="cuda:host-gcc-too-new",
        fix_cmd="NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'",
    )
    cmd_state_failed(_args(state_dir))
    out = capsys.readouterr().out
    assert "gpu-burn-git" in out
    assert "cuda:host-gcc-too-new" in out
    assert "fix: NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'" in out
    assert "1 failed package(s)" in out


def test_failed_table_colourised_when_enabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(log, "_COLOR_MODE", "always")
    state_dir = tmp_path / "state"
    _seed_failure(
        state_dir, "gpu-burn-git",
        error="[build_failed] nvcc exit 4",
        signature="cuda:host-gcc-too-new",
        fix_cmd="export FOO=bar",
    )
    cmd_state_failed(_args(state_dir))
    out = capsys.readouterr().out
    # The failed pkgbase is painted red; the fix hint green. Reset codes present.
    assert log._ANSI_RED in out
    assert log._ANSI_GREEN in out
    # Content survives colourisation unbroken.
    assert "gpu-burn-git" in out
    assert "fix: export FOO=bar" in out


def test_clear_one(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed_failure(state_dir, "foo-git", error="boom")
    _seed_failure(state_dir, "bar-git", error="boom")
    cmd_state_failed(_args(state_dir, clear="foo-git"))
    out = capsys.readouterr().out
    assert "Cleared recorded failure for 'foo-git'." in out
    remaining = BuildState(state_dir).all_failures()
    assert set(remaining) == {"bar-git"}


def test_clear_missing(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed_failure(state_dir, "foo-git", error="boom")
    cmd_state_failed(_args(state_dir, clear="nope-git"))
    out = capsys.readouterr().out
    assert "No recorded failure for 'nope-git'." in out
    assert "foo-git" in BuildState(state_dir).all_failures()


def test_clear_all(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed_failure(state_dir, "foo-git", error="boom")
    _seed_failure(state_dir, "bar-git", error="boom")
    cmd_state_failed(_args(state_dir, clear_all=True))
    out = capsys.readouterr().out
    assert "Cleared 2 recorded failure(s)." in out
    assert BuildState(state_dir).all_failures() == {}
