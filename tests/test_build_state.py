"""
test_build_state.py — unit tests for sysforge.primitives.build_state

Uses tmp_path; no filesystem state is required beyond the temp directory.
"""
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.build_state import BuildState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(bs, pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
            pkgbase="htop", pkgbuild_dir=None, build_mode=None):
    if pkgbuild_dir is None:
        pkgbuild_dir = Path("/home/user/src/htop")
    bs.record(pkgname=pkgname, pkgver=pkgver, pkgrel=pkgrel,
              epoch=epoch, pkgbase=pkgbase, pkgbuild_dir=pkgbuild_dir,
              build_mode=build_mode)


# ---------------------------------------------------------------------------
# record / get
# ---------------------------------------------------------------------------

def test_record_and_get(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    entry = bs.get("htop")
    assert entry is not None
    assert entry["pkgver"] == "3.4.1"
    assert entry["pkgrel"] == "1"
    assert entry["epoch"] == "0"
    assert entry["pkgbase"] == "htop"
    assert "built_at" in entry


def test_get_missing_returns_none(tmp_path):
    bs = BuildState(tmp_path)
    assert bs.get("nonexistent") is None


# ---------------------------------------------------------------------------
# save / reload
# ---------------------------------------------------------------------------

def test_save_and_reload(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", pkgver="3.4.1")
    bs.save()

    bs2 = BuildState(tmp_path)
    entry = bs2.get("htop")
    assert entry is not None
    assert entry["pkgver"] == "3.4.1"


def test_atomic_write_no_tmp_leftover(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    bs.save()
    tmp_file = bs.path.with_suffix(".toml.tmp")
    assert not tmp_file.exists()


def test_serialized_toml_is_valid(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", pkgver="3.4.1")
    _record(bs, pkgname="neovim", pkgver="0.10.0", pkgbase="neovim")
    bs.save()
    with open(bs.path, "rb") as f:
        data = tomllib.load(f)
    assert "htop" in data
    assert "neovim" in data
    assert data["htop"]["pkgver"] == "3.4.1"


# ---------------------------------------------------------------------------
# all_packages
# ---------------------------------------------------------------------------

def test_all_packages(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop")
    _record(bs, pkgname="neovim", pkgbase="neovim")
    packages = bs.all_packages()
    assert set(packages.keys()) == {"htop", "neovim"}


def test_all_packages_empty(tmp_path):
    bs = BuildState(tmp_path)
    assert bs.all_packages() == {}


# ---------------------------------------------------------------------------
# Split packages (multiple pkgnames, same pkgbase)
# ---------------------------------------------------------------------------

def test_split_package_records(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="llvm", pkgver="19.1.0", pkgrel="1", epoch="0",
              pkgbase="llvm", pkgbuild_dir=Path("/src/llvm"))
    bs.record(pkgname="llvm-libs", pkgver="19.1.0", pkgrel="1", epoch="0",
              pkgbase="llvm", pkgbuild_dir=Path("/src/llvm"))
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("llvm") is not None
    assert bs2.get("llvm-libs") is not None
    assert bs2.get("llvm")["pkgbase"] == "llvm"
    assert bs2.get("llvm-libs")["pkgbase"] == "llvm"


# ---------------------------------------------------------------------------
# build_mode field
# ---------------------------------------------------------------------------

def test_record_with_build_mode(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, build_mode="profiled")
    entry = bs.get("htop")
    assert entry["build_mode"] == "profiled"


def test_record_without_build_mode_omits_field(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    entry = bs.get("htop")
    assert "build_mode" not in entry


def test_build_mode_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="mesa", pkgbase="mesa", build_mode="profiled")
    _record(bs, pkgname="htop", pkgbase="htop")
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("mesa")["build_mode"] == "profiled"
    assert "build_mode" not in bs2.get("htop")


def test_build_mode_in_serialized_toml(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, build_mode="profiled")
    bs.save()
    with open(bs.path, "rb") as f:
        data = tomllib.load(f)
    assert data["htop"]["build_mode"] == "profiled"


# ---------------------------------------------------------------------------
# State dir via SYSFORGE_STATE_DIR env var
# ---------------------------------------------------------------------------

def test_corrupt_toml_raises(tmp_path):
    """A corrupt build_state.toml should raise TOMLDecodeError on load."""
    state_file = tmp_path / "build_state.toml"
    state_file.write_text("this is not valid toml [[[")
    import pytest
    with pytest.raises(tomllib.TOMLDecodeError):
        BuildState(tmp_path)


def test_state_dir_from_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path))
    from sysforge.pipeline.state import resolve_state_dir
    chosen, source = resolve_state_dir()
    assert chosen == tmp_path
    assert source == "SYSFORGE_STATE_DIR"
