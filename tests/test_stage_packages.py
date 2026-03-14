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
    _hardware_gate,
)
from sysforge.pipeline.state import PipelineState
from sysforge.pipeline.stages.base import RunOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PACKAGES_TOML = """
[build]
pkgbuild_dir = "{pkgbuild_dir}"

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

def make_packages_toml(tmp_path, pkgbuild_dir=None):
    if pkgbuild_dir is None:
        pkgbuild_dir = tmp_path / "builds"
    p = tmp_path / "packages.toml"
    p.write_text(PACKAGES_TOML.format(pkgbuild_dir=pkgbuild_dir))
    return p

def make_pkgbuild(pkgbuild_dir, name):
    """Create a minimal PKGBUILD for a package."""
    d = pkgbuild_dir / name
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
# _hardware_gate
# ---------------------------------------------------------------------------

def test_hardware_gate_no_requirement_passes():
    assert _hardware_gate({"name": "htop"}, {}) is True

def test_hardware_gate_no_profile_skips(tmp_path):
    pkg = {"name": "nvidia-open-dkms", "requires_hardware": "nvidia_gpu"}
    assert _hardware_gate(pkg, {}) is False

def test_hardware_gate_present_passes(tmp_path):
    hw_path = tmp_path / "hardware_profile.toml"
    hw_path.write_text('nvidia_gpu = true\n')
    pkg = {"name": "nvidia-open-dkms", "requires_hardware": "nvidia_gpu"}
    result = _hardware_gate(pkg, {"hardware_profile": str(hw_path)})
    assert result is True

def test_hardware_gate_absent_skips(tmp_path):
    hw_path = tmp_path / "hardware_profile.toml"
    hw_path.write_text('amd_cpu = true\n')
    pkg = {"name": "nvidia-open-dkms", "requires_hardware": "nvidia_gpu"}
    result = _hardware_gate(pkg, {"hardware_profile": str(hw_path)})
    assert result is False


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
