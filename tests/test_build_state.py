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
# toolchain_variant
# ---------------------------------------------------------------------------

def test_record_with_toolchain_variant(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="htop", pkgver="3.4.0", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=Path("/tmp/x"),
              toolchain_variant="pgo_llvm")
    assert bs.get("htop")["toolchain_variant"] == "pgo_llvm"


def test_record_without_toolchain_variant_omits_field(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    assert "toolchain_variant" not in bs.get("htop")


def test_toolchain_variant_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="htop", pkgver="3.4.0", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=Path("/tmp/x"),
              toolchain_variant="stock_llvm")
    bs.save()
    bs2 = BuildState(tmp_path)
    assert bs2.get("htop")["toolchain_variant"] == "stock_llvm"


def test_toolchain_variant_sticky_on_subsequent_record(tmp_path):
    """Variant survives a rebuild path that doesn't know to re-pass it
    (e.g. repair/backfill). Matches the sticky-preservation pattern of
    source and owner_stage."""
    bs = BuildState(tmp_path)
    bs.record(pkgname="htop", pkgver="3.4.0", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=Path("/tmp/x"),
              toolchain_variant="pgo_llvm")
    bs.record(pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=Path("/tmp/x"))
    assert bs.get("htop")["toolchain_variant"] == "pgo_llvm"


def test_toolchain_variant_in_serialized_toml(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="htop", pkgver="3.4.0", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=Path("/tmp/x"),
              toolchain_variant="gcc")
    bs.save()
    text = (tmp_path / "build_state.toml").read_text()
    assert 'toolchain_variant = "gcc"' in text


# ---------------------------------------------------------------------------
# origin_pkgbase — pre-rename pkgbase for -sysforge builds
# ---------------------------------------------------------------------------

def test_record_with_origin_pkgbase(tmp_path):
    # A renamed llvm-sysforge build: pkgbase is the renamed value, origin_pkgbase
    # carries the upstream identity update uses for version/source correlation.
    bs = BuildState(tmp_path)
    bs.record(pkgname="llvm-sysforge", pkgver="18.1.8", pkgrel="1", epoch="0",
              pkgbase="llvm-sysforge", pkgbuild_dir=Path("/tmp/llvm"),
              origin_pkgbase="llvm")
    assert bs.get("llvm-sysforge")["origin_pkgbase"] == "llvm"


def test_record_without_origin_pkgbase_omits_field(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs)
    assert "origin_pkgbase" not in bs.get("htop")


def test_origin_pkgbase_sticky_on_subsequent_record(tmp_path):
    """origin_pkgbase survives a rebuild that doesn't re-pass it (e.g. an
    update-driven rebuild). Matches the sticky pattern of the other provenance
    fields."""
    bs = BuildState(tmp_path)
    bs.record(pkgname="llvm-sysforge", pkgver="18.1.8", pkgrel="1", epoch="0",
              pkgbase="llvm-sysforge", pkgbuild_dir=Path("/tmp/llvm"),
              origin_pkgbase="llvm")
    bs.record(pkgname="llvm-sysforge", pkgver="18.1.9", pkgrel="1", epoch="0",
              pkgbase="llvm-sysforge", pkgbuild_dir=Path("/tmp/llvm"))
    assert bs.get("llvm-sysforge")["origin_pkgbase"] == "llvm"


def test_origin_pkgbase_persisted_and_reloaded(tmp_path):
    bs = BuildState(tmp_path)
    bs.record(pkgname="mesa-sysforge", pkgver="24.1.0", pkgrel="1", epoch="0",
              pkgbase="mesa-sysforge", pkgbuild_dir=Path("/tmp/mesa"),
              origin_pkgbase="mesa")
    bs.save()
    text = (tmp_path / "build_state.toml").read_text()
    assert 'origin_pkgbase = "mesa"' in text
    bs2 = BuildState(tmp_path)
    assert bs2.get("mesa-sysforge")["origin_pkgbase"] == "mesa"


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


# ---------------------------------------------------------------------------
# Build failures namespace ([failures] table)
# ---------------------------------------------------------------------------

def test_record_failure_and_all_failures(tmp_path):
    bs = BuildState(tmp_path)
    bs.record_failure(
        "gpu-burn-git",
        error="[build_failed] nvcc exit 4",
        pkgver="r93.a113ce7",
        signature="cuda:host-gcc-too-new",
        fix_cmd="NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'",
    )
    failures = bs.all_failures()
    assert set(failures) == {"gpu-burn-git"}
    rec = failures["gpu-burn-git"]
    assert rec["signature"] == "cuda:host-gcc-too-new"
    assert rec["fix_cmd"] == "NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15'"
    assert rec["pkgver"] == "r93.a113ce7"
    assert "failed_at" in rec


def test_failures_isolated_from_install_mirror(tmp_path):
    """[failures] must not leak into all_packages()/sync_with_installed()."""
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", pkgbase="htop")
    bs.record_failure("foo-git", error="boom")
    assert "foo-git" not in bs.all_packages()
    assert "failures" not in bs.all_packages()
    # sync prunes the install mirror against pacman -Q but must leave failures.
    bs.sync_with_installed({"htop": "3.4.1-1"})
    assert bs.all_failures() == {} or "foo-git" in bs.all_failures()
    assert "foo-git" in bs.all_failures()


def test_success_record_clears_failure(tmp_path):
    bs = BuildState(tmp_path)
    bs.record_failure("gpu-burn-git", error="boom")
    assert "gpu-burn-git" in bs.all_failures()
    bs.record(pkgname="gpu-burn", pkgver="1", pkgrel="1", epoch="0",
              pkgbase="gpu-burn-git", pkgbuild_dir=Path("/x"),
              build_mode="profiled")
    assert bs.all_failures() == {}


def test_clear_failure(tmp_path):
    bs = BuildState(tmp_path)
    bs.record_failure("foo-git", error="boom")
    assert bs.clear_failure("foo-git") is True
    assert bs.clear_failure("foo-git") is False
    assert bs.all_failures() == {}


def test_failures_persist_across_reload(tmp_path):
    bs = BuildState(tmp_path)
    _record(bs, pkgname="htop", pkgbase="htop")
    bs.record_failure(
        "gpu-burn-git",
        error="line1\nline2\n[build_failed] boom",
        signature="cuda:host-gcc-too-new",
    )
    bs.save()
    bs2 = BuildState(tmp_path)
    assert "gpu-burn-git" in bs2.all_failures()
    assert bs2.all_failures()["gpu-burn-git"]["signature"] == "cuda:host-gcc-too-new"
    # install mirror still intact and free of the failures key
    assert "htop" in bs2.all_packages()
    assert "failures" not in bs2.all_packages()


def test_error_is_truncated(tmp_path):
    bs = BuildState(tmp_path)
    long_err = "\n".join(f"line {i}" for i in range(50))
    bs.record_failure("foo-git", error=long_err)
    stored = bs.all_failures()["foo-git"]["error"]
    # Only the tail is kept (<= 6 lines).
    assert stored.count("\n") <= 5
    assert "line 49" in stored
    assert "line 0" not in stored


# ---------------------------------------------------------------------------
# flags_string (recorded for flag-drift detection — see primitives/flag_drift)
# ---------------------------------------------------------------------------

def test_build_state_records_flags_string(tmp_path):
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nCXXFLAGS=-O3\nLDFLAGS=-Wl,-O1"
    bs.record(pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    entry = bs.get("htop")
    assert entry["flags_string"] == fs


def test_build_state_flags_string_persisted(tmp_path):
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nLDFLAGS=-Wl,-O1"
    bs.record(pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("htop")["flags_string"] == fs


def test_build_state_flags_string_valid_toml(tmp_path):
    """flags_string with newlines must serialize to valid TOML."""
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nCXXFLAGS=-O3"
    bs.record(pkgname="mesa", pkgver="24.0.0", pkgrel="1", epoch="0",
              pkgbase="mesa", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    bs.save()
    with open(bs.path, "rb") as f:
        data = tomllib.load(f)
    assert data["mesa"]["flags_string"] == fs


def test_reviewed_commit_recorded_and_sticky(tmp_path):
    """reviewed_commit persists like the other provenance fields: a later
    record() that doesn't know it (e.g. a pipeline-stage rebuild) must not
    erase it, and an explicit new value replaces it."""
    bs = BuildState(tmp_path)
    bs.record(pkgname="htop", pkgver="1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", reviewed_commit="aaa111")
    assert bs.get("htop")["reviewed_commit"] == "aaa111"
    # Unaware rebuild: no reviewed_commit passed -> prior value preserved.
    bs.record(pkgname="htop", pkgver="2", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path, build_mode="profiled")
    assert bs.get("htop")["reviewed_commit"] == "aaa111"
    # Aware rebuild: explicit value replaces.
    bs.record(pkgname="htop", pkgver="3", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", reviewed_commit="bbb222")
    assert bs.get("htop")["reviewed_commit"] == "bbb222"
