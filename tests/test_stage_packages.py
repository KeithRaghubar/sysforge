"""
test_stage_packages.py — unit tests for the packages stage.

Mocks out makepkg_wrapper.run() and subprocess (pacman) so nothing real
is installed. Uses a temp packages.toml and temp state dir.
"""
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.pipeline.stages.packages import (
    PackagesStage,
    _load_packages,
    _resolve_pkgbuild,
)
from sysforge.pipeline.state import PipelineState
from sysforge.pipeline.stages.base import RunOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PACKAGES_TOML = """
[build]
pkgbuild_src_dir = "{pkgbuild_src_dir}"

[[package]]
name = "llvm"
source = "aur"

[[package]]
name = "htop"
source = "repo"

[[package]]
name = "mesa-git"
source = "aur"
"""

def make_packages_toml(tmp_path, pkgbuild_src_dir=None):
    if pkgbuild_src_dir is None:
        pkgbuild_src_dir = tmp_path / "builds"
    p = tmp_path / "packages.toml"
    p.write_text(PACKAGES_TOML.format(pkgbuild_src_dir=pkgbuild_src_dir))
    return p

def make_pkgbuild(pkgbuild_src_dir, name):
    """Create a minimal PKGBUILD for a package."""
    d = pkgbuild_src_dir / name
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")
    return pb

def make_options(**kwargs):
    defaults = dict(resume=False, start_from=None, force_retry=False,
                    dry_run=False, state_dir=None)
    defaults.update(kwargs)
    return RunOptions(**defaults)


# ---------------------------------------------------------------------------
# _resolve_pkgbuild
# ---------------------------------------------------------------------------

def test_resolve_pkgbuild_finds_local(tmp_path):
    pb = make_pkgbuild(tmp_path, "htop")
    result = _resolve_pkgbuild("htop", {"pkgbuild_src_dir": str(tmp_path)}, {})
    assert result == pb.resolve()


def test_resolve_pkgbuild_aur_clone(tmp_path):
    """When not found locally, _resolve_pkgbuild triggers AUR clone."""
    def fake_clone(name, dest):
        dest.mkdir()
        (dest / "PKGBUILD").write_text("pkgname=mesa-git\n")

    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={"mesa-git": {}}), \
         patch("sysforge.primitives.aur.aur_clone", side_effect=fake_clone):
        result = _resolve_pkgbuild("mesa-git", {"pkgbuild_src_dir": str(tmp_path)}, {})

    assert result == (tmp_path / "mesa-git" / "PKGBUILD").resolve()


def test_resolve_pkgbuild_not_found_raises(tmp_path):
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="PKGBUILD not found"):
            _resolve_pkgbuild("nonexistent", {"pkgbuild_src_dir": str(tmp_path)}, {})


def test_resolve_pkgbuild_falls_back_to_config_paths(tmp_path):
    """Falls back to flag_profiles [paths] pkgbuild_src_dir if build_cfg has none."""
    pb = make_pkgbuild(tmp_path, "htop")
    result = _resolve_pkgbuild("htop", {}, {"paths": {"pkgbuild_src_dir": str(tmp_path)}})
    assert result == pb.resolve()


# ---------------------------------------------------------------------------
# _load_packages
# ---------------------------------------------------------------------------

def test_load_packages_reads_toml(tmp_path):
    pb_dir = tmp_path / "builds"
    p = make_packages_toml(tmp_path, pb_dir)
    build_cfg, packages = _load_packages({"packages_file": str(p)})
    assert len(packages) == 3
    assert packages[0]["name"] == "llvm"

def test_load_packages_missing_raises(tmp_path):
    with pytest.raises(RuntimeError, match="packages.toml not found"):
        _load_packages({"packages_file": str(tmp_path / "nonexistent.toml")})


# ---------------------------------------------------------------------------
# PackagesStage.run() — happy path
# ---------------------------------------------------------------------------

def test_packages_stage_builds_all(tmp_path):
    builds_dir = tmp_path / "builds"
    for name in ("llvm", "mesa-git"):
        make_pkgbuild(builds_dir, name)
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")
    config = {"packages_file": str(pkg_file)}
    options = make_options(state_dir=tmp_path / "state")

    with patch("sysforge.pipeline.stages.packages.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        PackagesStage().run(config, state, options)

    p = state.get_package_progress()
    assert set(p["built"]) == {"llvm", "htop", "mesa-git"}
    assert p["failed"] == []

def test_packages_stage_checkpoints_after_each(tmp_path):
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")
    config = {"packages_file": str(pkg_file)}

    built_order = []

    def fake_run(path, **kwargs):
        name = Path(path).parent.name
        built_order.append(name)

    with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=fake_run), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        PackagesStage().run(config, state, make_options())

    # llvm before mesa-git (manifest order)
    assert built_order.index("llvm") < built_order.index("mesa-git")


# ---------------------------------------------------------------------------
# PackagesStage.run() — failure handling
# ---------------------------------------------------------------------------

def test_packages_stage_continues_after_failure(tmp_path):
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")

    def fail_llvm(path, **kwargs):
        if "llvm" in str(path):
            raise RuntimeError("llvm build failed")

    with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=fail_llvm), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        with pytest.raises(RuntimeError, match="packages stage finished with failures"):
            PackagesStage().run(config={"packages_file": str(pkg_file)},
                                state=state, options=make_options())

    p = state.get_package_progress()
    assert "llvm" in p["failed"]
    assert "htop" in p["built"]   # continued after llvm failed
    assert "mesa-git" in p["built"]

def test_packages_stage_records_error_message(tmp_path):
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.packages.makepkg_run",
               side_effect=RuntimeError("specific error message")), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0)
        with pytest.raises(RuntimeError):
            PackagesStage().run({"packages_file": str(pkg_file)}, state, make_options())

    errors = state.get_package_errors()
    assert "specific error message" in errors.get("llvm", "")


# ---------------------------------------------------------------------------
# PackagesStage.run() — resume / skip
# ---------------------------------------------------------------------------

def test_packages_stage_skips_already_built(tmp_path):
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")
    state.init_package_list(["llvm", "htop", "mesa-git"])
    state.mark_package_built("llvm")  # already built
    state.save()

    called = []
    def track_run(path, **kwargs):
        called.append(Path(path).parent.name)

    with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=track_run), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0)
        PackagesStage().run({"packages_file": str(pkg_file)}, state, make_options())

    assert "llvm" not in called   # was already built
    assert "mesa-git" in called

def test_packages_stage_aur_auto_clone(tmp_path):
    """AUR package missing locally is cloned then built."""
    builds_dir = tmp_path / "builds"
    # Only create the repo PKGBUILD; llvm and mesa-git are missing (should be cloned)
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")
    config = {"packages_file": str(pkg_file)}

    def fake_clone(name, dest):
        dest.mkdir(parents=True)
        (dest / "PKGBUILD").write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")

    built = []
    def fake_makepkg(path, **kwargs):
        built.append(Path(path).parent.name)

    with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=fake_makepkg), \
         patch("sysforge.pipeline.stages.packages.subprocess.run",
               return_value=MagicMock(returncode=0)), \
         patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={"llvm": {}, "mesa-git": {}}), \
         patch("sysforge.primitives.aur.aur_clone", side_effect=fake_clone):
        PackagesStage().run(config, state, make_options(state_dir=tmp_path / "state"))

    assert set(built) == {"llvm", "mesa-git"}
    p = state.get_package_progress()
    assert set(p["built"]) == {"llvm", "htop", "mesa-git"}


# ---------------------------------------------------------------------------
# repo_mode routing
# ---------------------------------------------------------------------------

PACKAGES_TOML_REPO_MODE = """
[build]
pkgbuild_src_dir = "{pkgbuild_src_dir}"
repo_mode = "profiled"

[[package]]
name = "htop"
source = "repo"

[[package]]
name = "mesa-git"
source = "aur"
"""

PACKAGES_TOML_PKGBUILD_PATCH_OVERRIDE = """
[build]
pkgbuild_src_dir = "{pkgbuild_src_dir}"

[[package]]
name = "htop"
source = "repo"
pkgbuild_patch = true

[[package]]
name = "neovim"
source = "repo"
"""


def test_repo_mode_profiled_builds_repo_pkg_from_source(tmp_path):
    """repo_mode=profiled: repo packages are built via makepkg, not pacman."""
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "htop")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text(PACKAGES_TOML_REPO_MODE.format(pkgbuild_src_dir=builds_dir))

    state = PipelineState(tmp_path / "state")

    built = []
    with patch("sysforge.pipeline.stages.packages.makepkg_run",
               side_effect=lambda path, **kw: built.append(Path(path).parent.name)), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        PackagesStage().run({"packages_file": str(pkg_file)}, state, make_options())

    assert "htop" in built       # repo pkg routed to profiled build
    assert "mesa-git" in built   # aur pkg always built
    mock_pacman.assert_not_called()


def test_pkgbuild_patch_overrides_pacman_repo_mode(tmp_path):
    """pkgbuild_patch=true on a repo pkg forces profiled build regardless of global repo_mode."""
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "htop")
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text(PACKAGES_TOML_PKGBUILD_PATCH_OVERRIDE.format(pkgbuild_src_dir=builds_dir))

    state = PipelineState(tmp_path / "state")

    built = []
    pacman_installed = []
    with patch("sysforge.pipeline.stages.packages.makepkg_run",
               side_effect=lambda path, **kw: built.append(Path(path).parent.name)), \
         patch("sysforge.pipeline.stages.packages.subprocess.run",
               side_effect=lambda cmd, **kw: pacman_installed.append(cmd[-1]) or MagicMock(returncode=0)):
        PackagesStage().run({"packages_file": str(pkg_file)}, state, make_options())

    assert "htop" in built             # pkgbuild_patch → profiled
    assert "neovim" in pacman_installed  # no pkgbuild_patch → pacman


def test_repo_mode_invalid_raises(tmp_path):
    """Invalid repo_mode value raises a clear error at load time."""
    pkg_file = tmp_path / "packages.toml"
    pkg_file.write_text('[build]\nrepo_mode = "hybrid"\n\n[[package]]\nname = "htop"\nsource = "repo"\n')
    with pytest.raises(ValueError, match="Invalid.*repo_mode"):
        _load_packages({"packages_file": str(pkg_file)})


def test_packages_stage_dry_run_calls_nothing(tmp_path):
    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.packages.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        PackagesStage().run(
            {"packages_file": str(pkg_file)},
            state,
            make_options(dry_run=True),
        )
        mock_run.assert_not_called()
        mock_pacman.assert_not_called()
