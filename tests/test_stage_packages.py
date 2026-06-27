"""
test_stage_packages.py — unit tests for the packages stage.

Mocks out makepkg_wrapper.run() and subprocess (pacman) so nothing real
is installed. Uses a temp packages.toml and temp state dir.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.pipeline.stages.packages import (
    PackagesStage,
    _enable_display_managers,
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
    """When not found locally, _resolve_pkgbuild triggers AUR clone via the scheduler."""
    from sysforge.primitives.source_sync import reset_scheduler
    reset_scheduler()

    def fake_clone(name, dest, **kw):
        dest.mkdir()
        (dest / "PKGBUILD").write_text("pkgname=mesa-git\n")

    try:
        with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
             patch("sysforge.primitives.aur.aur_info", return_value={"mesa-git": {}}), \
             patch("sysforge.primitives.source_sync.aur_clone", side_effect=fake_clone):
            result = _resolve_pkgbuild("mesa-git", {"pkgbuild_src_dir": str(tmp_path)}, {})
    finally:
        reset_scheduler()

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

    with patch("sysforge.pipeline.stages.packages.makepkg_run"), \
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
        with pytest.raises(RuntimeError, match=r"\[PACKAGES\] stage finished with failures"):
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
    from sysforge.primitives.source_sync import reset_scheduler
    reset_scheduler()

    builds_dir = tmp_path / "builds"
    # Only create the repo PKGBUILD; llvm and mesa-git are missing (should be cloned)
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state = PipelineState(tmp_path / "state")
    config = {"packages_file": str(pkg_file)}

    def fake_clone(name, dest, **kw):
        dest.mkdir(parents=True)
        (dest / "PKGBUILD").write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")

    built = []
    def fake_makepkg(path, **kwargs):
        built.append(Path(path).parent.name)

    try:
        with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=fake_makepkg), \
             patch("sysforge.pipeline.stages.packages.subprocess.run",
                   return_value=MagicMock(returncode=0)), \
             patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
             patch("sysforge.primitives.aur.aur_info", return_value={"llvm": {}, "mesa-git": {}}), \
             patch("sysforge.primitives.source_sync.aur_clone", side_effect=fake_clone):
            PackagesStage().run(config, state, make_options(state_dir=tmp_path / "state"))
    finally:
        reset_scheduler()

    assert set(built) == {"llvm", "mesa-git"}
    p = state.get_package_progress()
    assert set(p["built"]) == {"llvm", "htop", "mesa-git"}


# ---------------------------------------------------------------------------
# repo_mode routing
# ---------------------------------------------------------------------------

PACKAGES_TOML_REPO_MODE = """
[build]
pkgbuild_src_dir = "{pkgbuild_src_dir}"
repo_mode = "build_from_source"

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
enable_build_from_source = true

[[package]]
name = "neovim"
source = "repo"
"""


def test_repo_mode_profiled_builds_repo_pkg_from_source(tmp_path):
    """repo_mode=build_from_source: repo packages are built via makepkg, not pacman."""
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

    assert "htop" in built       # repo pkg routed to source build
    assert "mesa-git" in built   # aur pkg always built
    mock_pacman.assert_not_called()


def test_enable_build_from_source_overrides_pacman_repo_mode(tmp_path):
    """enable_build_from_source=true on a repo pkg forces source build regardless of global repo_mode."""
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

    assert "htop" in built             # enable_build_from_source → source build
    assert "neovim" in pacman_installed  # no enable_build_from_source → pacman


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


# ---------------------------------------------------------------------------
# Sentinel coverage (stage_in_progress.toml)
# ---------------------------------------------------------------------------


def test_packages_stage_writes_sentinel_during_build_and_clears_on_success(tmp_path):
    """Sentinel is present during the main build loop, cleared on clean exit.
    Closes the gap surfaced by the safeguards audit (PackagesStage previously
    ran pacman -S without any stage-level sentinel)."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds_dir = tmp_path / "builds"
    for name in ("llvm", "mesa-git"):
        make_pkgbuild(builds_dir, name)
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    seen = {"present": False, "stage": None}

    def check_sentinel_during_build(*_a, **_kw):
        record = StageSentinel(state_dir).get_active()
        if record is not None:
            seen["present"] = True
            seen["stage"] = record.get("stage")

    with patch("sysforge.pipeline.stages.packages.makepkg_run",
               side_effect=check_sentinel_during_build), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        PackagesStage().run(
            {"packages_file": str(pkg_file)}, state,
            make_options(state_dir=state_dir),
        )

    assert seen["present"] is True
    assert seen["stage"] == "packages"
    # Cleared on clean exit
    assert StageSentinel(state_dir).get_active() is None


def test_packages_stage_clears_sentinel_even_when_some_packages_fail(tmp_path):
    """Per-package failures are caught by the inner try/except and reported via
    state. The stage's final RuntimeError is raised OUTSIDE the sentinel scope,
    so the sentinel is cleared. The sentinel only persists for interruptions
    (CleanExitRequested) or unexpected exceptions inside the scope."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    def fail_llvm(path, **kwargs):
        if "llvm" in str(path):
            raise RuntimeError("llvm build failed")

    with patch("sysforge.pipeline.stages.packages.makepkg_run",
               side_effect=fail_llvm), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        with pytest.raises(RuntimeError, match="stage finished with failures"):
            PackagesStage().run(
                {"packages_file": str(pkg_file)}, state,
                make_options(state_dir=state_dir),
            )

    # Sentinel should be cleared — the final raise is outside the scope.
    assert StageSentinel(state_dir).get_active() is None


def test_packages_stage_preserves_sentinel_on_unexpected_exception(tmp_path):
    """A non-RuntimeError leaking from inside the scope (e.g. KeyError from a
    code bug, or a SystemExit from an interrupt handler upstream) must leave
    the sentinel in place so the next sysforge run blocks for recovery."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds_dir = tmp_path / "builds"
    make_pkgbuild(builds_dir, "llvm")
    make_pkgbuild(builds_dir, "mesa-git")
    pkg_file = make_packages_toml(tmp_path, builds_dir)

    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    def boom(*_a, **_kw):
        raise KeyError("simulated upstream bug")

    with patch("sysforge.pipeline.stages.packages.makepkg_run", side_effect=boom), \
         patch("sysforge.pipeline.stages.packages.subprocess.run") as mock_pacman:
        mock_pacman.return_value = MagicMock(returncode=0)
        with pytest.raises(KeyError):
            PackagesStage().run(
                {"packages_file": str(pkg_file)}, state,
                make_options(state_dir=state_dir),
            )

    record = StageSentinel(state_dir).get_active()
    assert record is not None
    assert record["stage"] == "packages"


# ---------------------------------------------------------------------------
# Display-manager enablement (boot-into-desktop fix)
# ---------------------------------------------------------------------------


def _gnome_group_entries():
    """Synthetic expand_package_groups output for an installed GNOME group."""
    return [
        {"name": "gnome-shell", "group": "gnome"},
        {"name": "gdm", "group": "gnome"},
        {"name": "htop"},  # ungrouped — must be ignored
    ]


def test_enable_display_managers_enables_dm_when_built():
    built = {"gnome-shell", "gdm"}
    with patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        _enable_display_managers(_gnome_group_entries(), built, dry_run=False)
    calls = [c.args[0] for c in m.call_args_list]
    assert ["sudo", "systemctl", "enable", "gdm.service"] in calls


def test_enable_display_managers_skips_when_dm_not_built():
    # The group was selected but its DM package failed to build — don't enable.
    built = {"gnome-shell"}
    with patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        _enable_display_managers(_gnome_group_entries(), built, dry_run=False)
    m.assert_not_called()


def test_enable_display_managers_ignores_non_desktop_groups():
    entries = [{"name": "htop", "group": "tools"}, {"name": "vim", "group": "tools"}]
    with patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        _enable_display_managers(entries, {"htop", "vim"}, dry_run=False)
    m.assert_not_called()


def test_enable_display_managers_dry_run_does_nothing():
    with patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        _enable_display_managers(_gnome_group_entries(), {"gdm"}, dry_run=True)
    m.assert_not_called()


def test_enable_display_managers_enables_each_dm_once():
    entries = [
        {"name": "gdm", "group": "gnome"},
        {"name": "gnome-shell", "group": "gnome"},
    ]
    with patch("sysforge.pipeline.stages.packages.subprocess.run") as m:
        _enable_display_managers(entries, {"gdm", "gnome-shell"}, dry_run=False)
    enable_calls = [
        c.args[0] for c in m.call_args_list if "enable" in c.args[0]
    ]
    assert enable_calls.count(["sudo", "systemctl", "enable", "gdm.service"]) == 1
