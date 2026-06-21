"""
test_state_forget.py — coverage for `sysforge state forget`.

Deletes build_state.toml records so `sysforge update` stops maintaining the
package (the escape hatch for durable-by-default tracking).
"""
from pathlib import Path
from types import SimpleNamespace

from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_forget


def _args(state_dir: Path, *pkgnames):
    return SimpleNamespace(state_dir=str(state_dir), pkgnames=list(pkgnames))


def _seed(state_dir: Path, pkgname: str, pkgbase: str, build_mode="source_built"):
    state_dir.mkdir(parents=True, exist_ok=True)
    bs = BuildState(state_dir)
    bs.record(pkgname=pkgname, pkgver="1", pkgrel="1", epoch="0",
              pkgbase=pkgbase, pkgbuild_dir=state_dir, build_mode=build_mode,
              source="repo")
    bs.save()


def test_forget_deletes_record(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed(state_dir, "mesa", "mesa")
    cmd_state_forget(_args(state_dir, "mesa"))
    out = capsys.readouterr().out
    assert "Stopped tracking" in out and "mesa" in out
    assert BuildState(state_dir).get("mesa") is None


def test_forget_pkgbase_expands_to_split_members(tmp_path):
    """Forgetting a pkgbase drops every split-package member sharing it."""
    state_dir = tmp_path / "state"
    _seed(state_dir, "llvm", "llvm")
    _seed(state_dir, "llvm-libs", "llvm")
    cmd_state_forget(_args(state_dir, "llvm"))
    bs = BuildState(state_dir)
    assert bs.get("llvm") is None
    assert bs.get("llvm-libs") is None


def test_forget_missing_package_reports_nothing_to_forget(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed(state_dir, "mesa", "mesa")
    cmd_state_forget(_args(state_dir, "ghost"))
    out = capsys.readouterr().out
    assert "nothing to forget" in out
    # The unrelated record is untouched.
    assert BuildState(state_dir).get("mesa") is not None
