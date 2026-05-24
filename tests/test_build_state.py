"""
test_build_state.py — unit tests for sysforge.primitives.build_state

Uses tmp_path; no filesystem state is required beyond the temp directory.
"""
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.build_state import BuildState, parse_pacman_version


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
# built_upstream_commit field
# ---------------------------------------------------------------------------

_FAKE_SHA = "deadbeef0123456789deadbeef0123456789dead"


def test_record_with_built_upstream_commit(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="cosmic-comp-git", pkgver="1.0", pkgrel="1", epoch="0",
              pkgbase="cosmic-comp-git", pkgbuild_dir=Path("/tmp/x"),
              built_upstream_commit=_FAKE_SHA)
    entry = bs.get("cosmic-comp-git")
    assert entry["built_upstream_commit"] == _FAKE_SHA


def test_record_without_built_upstream_commit_omits_field(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    entry = bs.get("htop")
    assert "built_upstream_commit" not in entry


def test_built_upstream_commit_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="cosmic-comp-git", pkgver="1.0", pkgrel="1", epoch="0",
              pkgbase="cosmic-comp-git", pkgbuild_dir=Path("/tmp/x"),
              built_upstream_commit=_FAKE_SHA)
    _record(bs, pkgname="htop")
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("cosmic-comp-git")["built_upstream_commit"] == _FAKE_SHA
    assert "built_upstream_commit" not in bs2.get("htop")


# ---------------------------------------------------------------------------
# source field — persisted origin classification (aur/repo/git)
# ---------------------------------------------------------------------------

def test_record_with_source(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="llvm", pkgver="20.1.0", pkgrel="1", epoch="0",
              pkgbase="llvm", pkgbuild_dir=Path("/tmp/x"), source="repo")
    assert bs.get("llvm")["source"] == "repo"


def test_record_without_source_omits_field(tmp_path):
    """Back-compat: callers that don't pass source produce no field."""
    bs = BuildState(tmp_path)
    _record(bs)
    assert "source" not in bs.get("htop")


def test_source_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="mesa-git", pkgver="25.0", pkgrel="1", epoch="0",
              pkgbase="mesa-git", pkgbuild_dir=Path("/tmp/x"), source="aur")
    bs.record(pkgname="llvm", pkgver="20.1.0", pkgrel="1", epoch="0",
              pkgbase="llvm", pkgbuild_dir=Path("/tmp/x"), source="repo")
    _record(bs, pkgname="htop")  # no source
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("mesa-git")["source"] == "aur"
    assert bs2.get("llvm")["source"] == "repo"
    assert "source" not in bs2.get("htop")


def test_record_with_local_source(tmp_path):
    """`source = "local"` is accepted and persisted as-is."""
    bs = BuildState(tmp_path)
    bs.record(pkgname="linux-custom", pkgver="6.13.0", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"), source="local")
    assert bs.get("linux-custom")["source"] == "local"


def test_source_sticky_on_subsequent_record(tmp_path):
    """A second record() without source preserves the previously-set source.

    Prevents accidental clearing when the update flow rebuilds a package
    through a code path that doesn't know the original source classification.
    """
    bs = BuildState(tmp_path)
    bs.record(pkgname="linux-custom", pkgver="6.13.0", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"), source="local")
    # Second build, source not passed
    bs.record(pkgname="linux-custom", pkgver="6.13.1", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"))
    assert bs.get("linux-custom")["source"] == "local"


# ---------------------------------------------------------------------------
# owner_stage — lifecycle ownership for skipping in `sysforge update`
# ---------------------------------------------------------------------------

def test_record_with_owner_stage(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="linux-custom", pkgver="6.13.0", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"),
              owner_stage="kernel")
    assert bs.get("linux-custom")["owner_stage"] == "kernel"


def test_record_without_owner_stage_omits_field(tmp_path):
    """Back-compat: callers that don't pass owner_stage produce no field."""
    bs = BuildState(tmp_path)
    _record(bs)
    assert "owner_stage" not in bs.get("htop")


def test_owner_stage_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="linux-custom", pkgver="6.13.0", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"),
              owner_stage="kernel")
    bs.save()
    bs2 = BuildState(tmp_path)
    assert bs2.get("linux-custom")["owner_stage"] == "kernel"


def test_owner_stage_sticky_on_subsequent_record(tmp_path):
    """Owner_stage survives a rebuild that doesn't re-pass it (the
    ``sysforge update --include-stage-owned`` rebuild path)."""
    bs = BuildState(tmp_path)
    bs.record(pkgname="linux-custom", pkgver="6.13.0", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"),
              source="local", owner_stage="kernel")
    bs.record(pkgname="linux-custom", pkgver="6.13.1", pkgrel="1", epoch="0",
              pkgbase="linux-custom", pkgbuild_dir=Path("/tmp/x"))
    assert bs.get("linux-custom")["owner_stage"] == "kernel"
    assert bs.get("linux-custom")["source"] == "local"


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


# ---------------------------------------------------------------------------
# parse_pacman_version
# ---------------------------------------------------------------------------

def test_parse_pacman_version_basic():
    assert parse_pacman_version("3.4.1-1") == ("0", "3.4.1", "1")


def test_parse_pacman_version_with_epoch():
    assert parse_pacman_version("2:1.2.3-4") == ("2", "1.2.3", "4")


def test_parse_pacman_version_multi_dash_pkgver():
    # r12345.abcdef-1 style (VCS packages)
    assert parse_pacman_version("r12345.abcdef-1") == ("0", "r12345.abcdef", "1")


def test_parse_pacman_version_no_pkgrel():
    assert parse_pacman_version("1.0") == ("0", "1.0", "1")


def test_parse_pacman_version_empty():
    assert parse_pacman_version("") == ("0", "", "1")


# ---------------------------------------------------------------------------
# _parse_built_pkg_filename — canonical post-build version source
# ---------------------------------------------------------------------------

def test_parse_built_pkg_filename_basic():
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "htop", "htop-3.4.1-1-x86_64.pkg.tar.zst"
    ) == ("0", "3.4.1", "1")


def test_parse_built_pkg_filename_with_epoch():
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "openssl-1.1", "openssl-1.1-2:1.1.1.w-9-x86_64.pkg.tar.zst"
    ) == ("2", "1.1.1.w", "9")


def test_parse_built_pkg_filename_hyphenated_pkgname():
    # pkgname contains hyphens; anchor on the exact name prevents mis-splitting.
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "openssl-1.0", "openssl-1.0-1.0.2.u-7-x86_64.pkg.tar.zst"
    ) == ("0", "1.0.2.u", "7")


def test_parse_built_pkg_filename_wrong_name_returns_none():
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "htop", "neovim-0.10.0-1-x86_64.pkg.tar.zst"
    ) is None


def test_parse_built_pkg_filename_non_pkg_file_returns_none():
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename("htop", "htop-3.4.1-1.tar.gz") is None
    assert _parse_built_pkg_filename("htop", "htop-3.4.1-1-x86_64.sig") is None


def test_parse_built_pkg_filename_alt_compression():
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "htop", "htop-3.4.1-1-x86_64.pkg.tar.xz"
    ) == ("0", "3.4.1", "1")


def test_parse_built_pkg_filename_uncompressed():
    # PKGEXT='.pkg.tar' yields uncompressed names; `makepkg --packagelist`
    # emits them and evaluate_vcs_pkgver feeds them through this parser.
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    assert _parse_built_pkg_filename(
        "cosmic-applets-git",
        "cosmic-applets-git-1.0.11.r7.gc003924-1-x86_64.pkg.tar",
    ) == ("0", "1.0.11.r7.gc003924", "1")


# ---------------------------------------------------------------------------
# sync_with_installed (superset behaviour)
# ---------------------------------------------------------------------------

def test_sync_adds_pacman_mode_entries_for_new_installs(tmp_path):
    bs = BuildState(tmp_path)
    added, removed = bs.sync_with_installed({
        "htop": "3.4.1-1",
        "neovim": "0.10.0-1",
    })
    assert added == 2
    assert removed == 0
    entry = bs.get("htop")
    assert entry is not None
    assert entry["build_mode"] == "pacman"
    assert entry["pkgver"] == "3.4.1"
    assert entry["pkgrel"] == "1"
    assert entry["epoch"] == "0"
    assert "pkgbuild_dir" not in entry
    assert "flags_string" not in entry


def test_sync_preserves_profiled_entries(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", pkgver="3.4.1", build_mode="profiled")
    # Pre-record carries pkgbuild_dir; sync must not overwrite it.
    added, removed = bs.sync_with_installed({"htop": "3.4.1-1"})
    assert added == 0
    assert removed == 0
    entry = bs.get("htop")
    assert entry["build_mode"] == "profiled"
    assert entry["pkgbuild_dir"] == "/home/user/src/htop"


def test_sync_prunes_uninstalled_entries(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", build_mode="profiled")
    _record(bs, pkgname="neovim", pkgbase="neovim", build_mode="profiled")
    added, removed = bs.sync_with_installed({"htop": "3.4.1-1"})
    assert added == 0
    assert removed == 1
    assert bs.get("htop") is not None
    assert bs.get("neovim") is None


def test_sync_prunes_zombie_variable_keys(tmp_path):
    # Simulate a pre-superset zombie: an entry whose key holds a literal
    # ``$_pkgname`` because the parser failed to expand it. Such a key can
    # never match `pacman -Q`, so sync_with_installed must drop it.
    bs = BuildState(tmp_path)
    bs._data["$_pkgname-git"] = {
        "pkgver": "r1.abc",
        "pkgrel": "1",
        "epoch": "0",
        "pkgbase": "$_pkgname",
        "build_mode": "profiled",
    }
    added, removed = bs.sync_with_installed({"real-pkg": "1.0-1"})
    assert removed == 1
    assert bs.get("$_pkgname-git") is None
    assert bs.get("real-pkg") is not None


def test_sync_roundtrips_through_disk(tmp_path):
    bs = BuildState(tmp_path)
    bs.sync_with_installed({"1password": "8.10.30-1", "openssl-1.1": "1.1.1.w-4"})
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("1password")["build_mode"] == "pacman"
    assert bs2.get("openssl-1.1")["pkgver"] == "1.1.1.w"
    assert bs2.get("openssl-1.1")["pkgrel"] == "4"
