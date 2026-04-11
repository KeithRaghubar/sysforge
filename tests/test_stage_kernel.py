"""
test_stage_kernel.py — unit tests for the kernel stage.

Covers all pure-logic functions. Subprocess calls (lsmod, mkinitcpio,
bootctl, makepkg) are mocked — nothing real runs.
"""
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages.kernel import (
    KernelStage,
    _format_kconfig_line,
    _load_hardware_kconfig,
    _load_kernel_config,
    _pkgbuild_path,
    _validate_manual_kconfig,
    _write_kconfig_fragment,
)
from sysforge.pipeline.state import PipelineState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_options(**kwargs):
    defaults = dict(
        resume=False, start_from=None, force_retry=False,
        dry_run=False, state_dir=None,
        no_unified_log=False, no_pkg_logs=False,
        log_dir=None, purge_log=False, persist_log=False,
    )
    defaults.update(kwargs)
    return RunOptions(**defaults)


def make_pkgbuild(pkgbuild_dir, pkgname):
    d = pkgbuild_dir / pkgname
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(f"pkgname={pkgname}\npkgver=6.10\npkgrel=1\n")
    return pb


def make_kernel_toml(tmp_path, pkgbuild_dir, pkgname="linux-git",
                     bootloader="systemd-boot", kconfig=None):
    lines = [
        'enabled = true',
        f'pkgname = "{pkgname}"',
        f'source = "git"',
        f'pkgbuild_src_dir = "{pkgbuild_dir}"',
        f'bootloader = "{bootloader}"',
    ]
    if kconfig:
        for entry in kconfig:
            lines.append("[[kconfig]]")
            lines.append(f'option = "{entry["option"]}"')
            lines.append(f'value  = "{entry["value"]}"')
    p = tmp_path / "kernel.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


def make_hardware_profile(tmp_path, kconfig=None, extra=None):
    lines = []
    if extra:
        for k, v in extra.items():
            lines.append(f"{k} = {str(v).lower()}")
    if kconfig:
        lines.append("[kconfig]")
        for option, value in kconfig.items():
            lines.append(f'{option} = "{value}"')
    p = tmp_path / "hardware_profile.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# _format_kconfig_line
# ---------------------------------------------------------------------------

def test_format_kconfig_y():
    assert _format_kconfig_line("CONFIG_KVM", "y") == "CONFIG_KVM=y"

def test_format_kconfig_m():
    assert _format_kconfig_line("CONFIG_KVM", "m") == "CONFIG_KVM=m"

def test_format_kconfig_n():
    assert _format_kconfig_line("CONFIG_NOUVEAU", "n") == "# CONFIG_NOUVEAU is not set"

def test_format_kconfig_string():
    assert _format_kconfig_line("CONFIG_LOCALVERSION", "-sysforge") == 'CONFIG_LOCALVERSION="-sysforge"'

def test_format_kconfig_integer_string():
    assert _format_kconfig_line("CONFIG_HZ", "1000") == 'CONFIG_HZ="1000"'


# ---------------------------------------------------------------------------
# _validate_manual_kconfig
# ---------------------------------------------------------------------------

def test_validate_kconfig_valid():
    entries = [
        {"option": "CONFIG_HZ_1000", "value": "y"},
        {"option": "CONFIG_NOUVEAU", "value": "n"},
        {"option": "CONFIG_LOCALVERSION", "value": "-sysforge"},
    ]
    result = _validate_manual_kconfig(entries)
    assert result == {
        "CONFIG_HZ_1000": "y",
        "CONFIG_NOUVEAU": "n",
        "CONFIG_LOCALVERSION": "-sysforge",
    }

def test_validate_kconfig_empty_list():
    assert _validate_manual_kconfig([]) == {}

def test_validate_kconfig_missing_option():
    with pytest.raises(RuntimeError, match="missing 'option'"):
        _validate_manual_kconfig([{"value": "y"}])

def test_validate_kconfig_bad_option_format():
    with pytest.raises(RuntimeError, match="invalid option"):
        _validate_manual_kconfig([{"option": "hz_1000", "value": "y"}])

def test_validate_kconfig_bad_option_lowercase():
    with pytest.raises(RuntimeError, match="invalid option"):
        _validate_manual_kconfig([{"option": "CONFIG_hz", "value": "y"}])

def test_validate_kconfig_missing_config_prefix():
    with pytest.raises(RuntimeError, match="invalid option"):
        _validate_manual_kconfig([{"option": "HZ_1000", "value": "y"}])

def test_validate_kconfig_empty_value():
    with pytest.raises(RuntimeError, match="empty value"):
        _validate_manual_kconfig([{"option": "CONFIG_HZ", "value": ""}])

def test_validate_kconfig_duplicate_option():
    entries = [
        {"option": "CONFIG_HZ_1000", "value": "y"},
        {"option": "CONFIG_HZ_1000", "value": "n"},
    ]
    with pytest.raises(RuntimeError, match="duplicate option"):
        _validate_manual_kconfig(entries)


# ---------------------------------------------------------------------------
# _load_hardware_kconfig
# ---------------------------------------------------------------------------

def test_load_hardware_kconfig_no_path_configured():
    result = _load_hardware_kconfig({})
    assert result == {}

def test_load_hardware_kconfig_file_absent(tmp_path):
    config = {"hardware_profile": str(tmp_path / "nonexistent.toml")}
    result = _load_hardware_kconfig(config)
    assert result == {}

def test_load_hardware_kconfig_no_kconfig_section(tmp_path):
    hw = tmp_path / "hardware_profile.toml"
    hw.write_text('nvidia_gpu = true\n')
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == {}

def test_load_hardware_kconfig_returns_kconfig_table(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True},
        kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"},
    )
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == {"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"}

def test_load_hardware_kconfig_ignores_non_kconfig_keys(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True, "amd_cpu": True},
        kconfig={"CONFIG_MZEN3": "y"},
    )
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert "nvidia_gpu" not in result
    assert result == {"CONFIG_MZEN3": "y"}


# ---------------------------------------------------------------------------
# _load_kernel_config
# ---------------------------------------------------------------------------

def test_load_kernel_config_missing_returns_none(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    with patch.object(_km, "KERNEL_PATH", tmp_path / "nonexistent.toml"):
        result = _load_kernel_config()
    assert result is None

def test_load_kernel_config_returns_dict(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    with patch.object(_km, "KERNEL_PATH", p):
        result = _load_kernel_config()
    assert result["pkgname"] == "linux-git"
    assert result["bootloader"] == "systemd-boot"


# ---------------------------------------------------------------------------
# _pkgbuild_path
# ---------------------------------------------------------------------------

def test_pkgbuild_path_missing_pkgbuild_src_dir():
    with pytest.raises(RuntimeError, match="missing pkgbuild_src_dir"):
        _pkgbuild_path({"pkgname": "linux-git"})

def test_pkgbuild_path_missing_pkgname(tmp_path):
    with pytest.raises(RuntimeError, match="missing pkgname"):
        _pkgbuild_path({"pkgbuild_src_dir": str(tmp_path)})

def test_pkgbuild_path_pkgbuild_not_found(tmp_path):
    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        _pkgbuild_path({"pkgbuild_src_dir": str(tmp_path), "pkgname": "linux-git"})

def test_pkgbuild_path_returns_path(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    result = _pkgbuild_path({"pkgbuild_src_dir": str(builds), "pkgname": "linux-git"})
    assert result.name == "PKGBUILD"
    assert result.exists()

def test_pkgbuild_path_srcdir_override(tmp_path):
    """srcdir allows pkgname != source directory name (e.g. linux-custom in dir 'linux')."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux")   # directory is 'linux', not 'linux-custom'
    result = _pkgbuild_path({
        "pkgbuild_src_dir": str(builds),
        "pkgname": "linux-custom",
        "srcdir": "linux",
    })
    assert result.name == "PKGBUILD"
    assert result.exists()

def test_pkgbuild_path_srcdir_not_found(tmp_path):
    """Error message when srcdir directory doesn't exist."""
    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        _pkgbuild_path({
            "pkgbuild_src_dir": str(tmp_path),
            "pkgname": "linux-custom",
            "srcdir": "linux",
        })


# ---------------------------------------------------------------------------
# _write_kconfig_fragment
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_no_entries_is_noop(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    result = _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)
    assert result is None
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_hardware_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    assert result is not None
    content = result.read_text()
    assert "CONFIG_MZEN3=y" in content
    assert "# CONFIG_NOUVEAU is not set" in content
    assert "# source: hardware" in content

def test_write_kconfig_fragment_manual_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_HZ_1000", "value": "y"}],
    }

    result = _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

    assert result is not None
    content = result.read_text()
    assert "CONFIG_HZ_1000=y" in content
    assert "# source: manual" in content

def test_write_kconfig_fragment_merge_hw_and_manual(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_HZ_1000", "value": "y"}],
    }
    config = {"hardware_profile": str(hw)}

    result = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_MZEN3=y" in content
    assert "CONFIG_HZ_1000=y" in content

def test_write_kconfig_fragment_manual_wins_conflict(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_MZEN3", "value": "n"}],  # override hw
    }
    config = {"hardware_profile": str(hw)}

    result = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    # manual value wins — n, not y
    assert "# CONFIG_MZEN3 is not set" in content
    assert "CONFIG_MZEN3=y" not in content

def test_write_kconfig_fragment_conflict_emits_warn(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_MZEN3", "value": "n"}],
    }
    config = {"hardware_profile": str(hw)}

    with patch("sysforge.pipeline.stages.kernel._log") as mock_log:
        _write_kconfig_fragment(kernel_cfg, config, dry_run=False)
        warn_calls = [str(c) for c in mock_log.warn.call_args_list]
        assert any("CONFIG_MZEN3" in c and "manual override wins" in c for c in warn_calls)

def test_write_kconfig_fragment_dry_run_no_file(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result = _write_kconfig_fragment(kernel_cfg, config, dry_run=True)

    assert result is None
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_invalid_manual_raises(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "bad-name", "value": "y"}],
    }
    with pytest.raises(RuntimeError, match="invalid option"):
        _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

def test_write_kconfig_fragment_file_has_header(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_KVM": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "Generated by SysForge" in content
    assert "merge_config.sh" in content


# ---------------------------------------------------------------------------
# KernelStage.run()
# ---------------------------------------------------------------------------

def test_kernel_stage_noop_when_no_kernel_toml(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", tmp_path / "nonexistent.toml"), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
        mock_build.assert_not_called()
        mock_sub.assert_not_called()

def test_kernel_stage_dry_run_calls_nothing(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        KernelStage().run({}, state, make_options(dry_run=True, state_dir=tmp_path / "state"))
        mock_build.assert_not_called()
        mock_sub.assert_not_called()

def test_kernel_stage_calls_makepkg(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    mock_build.assert_called_once()
    called_path = mock_build.call_args[0][0]
    assert "linux-git" in str(called_path)

def test_kernel_stage_runs_mkinitcpio(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    cmds = [c.args[0] for c in mock_sub.call_args_list]
    assert any("mkinitcpio" in str(c) for c in cmds)

def test_kernel_stage_runs_bootctl_by_default(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="systemd-boot")
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    cmds = [c.args[0] for c in mock_sub.call_args_list]
    assert any("bootctl" in str(c) for c in cmds)

def test_kernel_stage_runs_grub_when_configured(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="grub")
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    cmds = [c.args[0] for c in mock_sub.call_args_list]
    assert any("grub-mkconfig" in str(c) for c in cmds)

def test_kernel_stage_skips_bootloader_when_none(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="none")
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    cmds = [c.args[0] for c in mock_sub.call_args_list]
    assert not any("bootctl" in str(c) or "grub" in str(c) for c in cmds)

def test_kernel_stage_mkinitcpio_failure_raises(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    def fail_mkinitcpio(cmd, **kwargs):
        if "mkinitcpio" in cmd:
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run", side_effect=fail_mkinitcpio):
        with pytest.raises(RuntimeError, match="mkinitcpio"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

def test_kernel_stage_writes_kconfig_fragment_when_hw_profile_present(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    config = {"hardware_profile": str(hw)}

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run(config, state, make_options(state_dir=tmp_path / "state"))

    fragment = builds / "linux-git" / "sysforge.config"
    assert fragment.exists()
    assert "CONFIG_MZEN3=y" in fragment.read_text()
