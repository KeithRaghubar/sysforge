"""
test_stage_kernel.py — unit tests for the kernel stage.

Covers all pure-logic functions. Subprocess calls (lsmod, mkinitcpio,
bootctl, makepkg) are mocked — nothing real runs.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages.kernel import (
    KernelStage,
    _capture_lsmod_snapshot,
    _fdo_is_llvm,
    _format_kconfig_line,
    _gate_fdo_llvm,
    _load_hardware_kconfig,
    _load_kernel_config,
    _merge_lsmod,
    _pkgbuild_path,
    _resolve_fdo,
    _validate_manual_kconfig,
    _write_kconfig_fragment,
    resolve_kconfig_targets,
)
from sysforge.pipeline.state import PipelineState
from sysforge.primitives import device_probe, kbuild_map, kernel_safety
import sysforge.pipeline.stages.kernel as _km


@contextmanager
def _capture_logs():
    """Capture SysForge log output at its stable primitive seam (``sysforge.log.*``).

    The stage emits through a module-level ``_log = get_logger(...)`` whose
    ``warn``/``info``/``ui`` methods forward to ``sysforge.log.{warn,info,ui}``.
    Patching there — rather than the stage's ``_log`` binding — keeps these
    assertions valid when the stage module is decomposed (the ``_log`` object
    moves) or re-tagged (the tag string changes), since we assert on the message
    text, not the logger identity. Module funcs take ``(tag, message)``, so the
    message is ``call.args[1]``.
    """
    with patch("sysforge.log.warn") as w, \
         patch("sysforge.log.info") as i, \
         patch("sysforge.log.ui") as u:
        yield SimpleNamespace(warn=w, info=i, ui=u)


# ---------------------------------------------------------------------------
# Boot-safety gate neutralization
#
# The KernelStage.run() tests exercise build/install/bootloader flow, not the
# boot-safety gates (those have dedicated tests below). This autouse fixture
# neutralizes the gates' system-touching primitives so the flow tests stay
# hermetic: a fallback kernel is "present", /boot is fine, the resolved-config
# audit and post-install verification find nothing, and the artifact install
# is a no-op. Individual gate tests re-patch the specific primitive they probe.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _neutralize_kernel_gates(monkeypatch):
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels",
                        lambda *a, **k: ["linux"])
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space",
                        lambda *a, **k: None)
    monkeypatch.setattr(kernel_safety, "detect_root_topology",
                        lambda: kernel_safety.RootTopology())
    monkeypatch.setattr(kernel_safety, "list_dkms_modules", lambda: [])
    monkeypatch.setattr(kernel_safety, "check_mkinitcpio_hooks",
                        lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "verify_boot_artifacts",
                        lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "check_dkms_for_kernel",
                        lambda *a, **k: [])
    monkeypatch.setattr(device_probe, "enumerate_devices", lambda *a, **k: [])
    monkeypatch.setattr(_km, "install_built_packages", lambda *a, **k: [])
    # The pkgname repo-collision check and the configured-vs-installed toolchain
    # mismatch check both shell out to pacman; neutralize them so run()-flow
    # tests stay hermetic. Dedicated tests below exercise them directly.
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package",
                        lambda *a, **k: False)
    monkeypatch.setattr(
        "sysforge.primitives.llvm_state.detect_toolchain_config_mismatch",
        lambda *a, **k: [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_options(**kwargs):
    defaults = dict(
        resume=False, start_from=None, force_retry=False,
        dry_run=False, state_dir=None,
        no_unified_log=False, no_pkg_logs=False,
        log_dir=None, purge_log=False, persist_log=False,
        # Stage-level tests are not exercising the scheduler — default to
        # --no-update so the kernel pre-sync is a no-op. Sync-specific tests
        # below override this explicitly.
        no_update=True,
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
                     bootloader="systemd-boot", kconfig=None, source="local"):
    lines = [
        'enabled = true',
        f'pkgname = "{pkgname}"',
        f'source = "{source}"',
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


def make_hardware_profile(tmp_path, kconfig=None, extra=None, kconfig_devices=None):
    lines = []
    if extra:
        for k, v in extra.items():
            lines.append(f"{k} = {str(v).lower()}")
    if kconfig:
        lines.append("[kconfig]")
        for option, value in kconfig.items():
            lines.append(f'{option} = "{value}"')
    if kconfig_devices:
        lines.append("[kconfig_devices]")
        for option, value in kconfig_devices.items():
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
# resolve_kconfig_targets
# ---------------------------------------------------------------------------

def test_resolve_kconfig_targets_unset_returns_none():
    assert resolve_kconfig_targets({}, interactive=True) is None


def test_resolve_kconfig_targets_ui_target_reordered_last():
    cfg = {"kconfig_targets": ["nconfig", "localmodconfig", "olddefconfig"]}
    assert resolve_kconfig_targets(cfg, interactive=True) == [
        "localmodconfig",
        "olddefconfig",
        "nconfig",
    ]


def test_resolve_kconfig_targets_two_ui_targets_rejected():
    cfg = {"kconfig_targets": ["nconfig", "menuconfig"]}
    with pytest.raises(ValueError, match="at most one"):
        resolve_kconfig_targets(cfg, interactive=True)


def test_resolve_kconfig_targets_randconfig_rejected():
    with pytest.raises(ValueError, match="randconfig"):
        resolve_kconfig_targets({"kconfig_targets": ["randconfig"]}, interactive=True)


def test_resolve_kconfig_targets_unknown_target_rejected():
    with pytest.raises(ValueError, match="unknown"):
        resolve_kconfig_targets({"kconfig_targets": ["bogusconfig"]}, interactive=True)


def test_resolve_kconfig_targets_prompting_target_rejected_when_non_interactive():
    with pytest.raises(ValueError, match="olddefconfig"):
        resolve_kconfig_targets({"kconfig_targets": ["oldconfig"]}, interactive=False)


def test_resolve_kconfig_targets_local_target_rejected_when_non_interactive():
    with pytest.raises(ValueError, match="interactively"):
        resolve_kconfig_targets(
            {"kconfig_targets": ["localmodconfig"]}, interactive=False
        )


def test_resolve_kconfig_targets_silent_targets_pass_non_interactive():
    cfg = {"kconfig_targets": ["olddefconfig", "savedefconfig"]}
    assert resolve_kconfig_targets(cfg, interactive=False) == [
        "olddefconfig",
        "savedefconfig",
    ]


def test_resolve_kconfig_targets_localmodconfig_warns(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        _km.log, "warn", lambda tag, msg: warnings.append(msg)
    )
    cfg = {"kconfig_targets": ["localmodconfig"]}
    result = resolve_kconfig_targets(cfg, interactive=True)
    assert result == ["localmodconfig"]
    assert any("lsmod.snapshot" in w for w in warnings)


def test_resolve_kconfig_targets_localyesconfig_warns(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        _km.log, "warn", lambda tag, msg: warnings.append(msg)
    )
    cfg = {"kconfig_targets": ["localyesconfig"]}
    result = resolve_kconfig_targets(cfg, interactive=True)
    assert result == ["localyesconfig"]
    assert any("lsmod.snapshot" in w for w in warnings)


# ---------------------------------------------------------------------------
# _load_hardware_kconfig
# ---------------------------------------------------------------------------

def test_load_hardware_kconfig_no_path_configured():
    result = _load_hardware_kconfig({})
    assert result == ({}, {})

def test_load_hardware_kconfig_file_absent(tmp_path):
    config = {"hardware_profile": str(tmp_path / "nonexistent.toml")}
    result = _load_hardware_kconfig(config)
    assert result == ({}, {})

def test_load_hardware_kconfig_no_kconfig_section(tmp_path):
    hw = tmp_path / "hardware_profile.toml"
    hw.write_text('nvidia_gpu = true\n')
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({}, {})

def test_load_hardware_kconfig_returns_kconfig_table(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True},
        kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"},
    )
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"}, {})

def test_load_hardware_kconfig_ignores_non_kconfig_keys(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True, "amd_cpu": True},
        kconfig={"CONFIG_MZEN3": "y"},
    )
    kconfig, device_kconfig = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert "nvidia_gpu" not in kconfig
    assert kconfig == {"CONFIG_MZEN3": "y"}
    assert device_kconfig == {}

def test_load_hardware_kconfig_returns_device_table(tmp_path):
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    result = _load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({"CONFIG_MZEN3": "y"}, {"CONFIG_IGC": "m"})

def test_load_hardware_kconfig_falls_back_to_state_dir(tmp_path):
    # Standalone `run kernel` after `run hardware`: config has no
    # hardware_profile key, but the hardware stage wrote the file under state_dir.
    make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    result = _load_hardware_kconfig({}, state_dir=tmp_path)
    assert result == ({"CONFIG_MZEN3": "y"}, {"CONFIG_IGC": "m"})

def test_load_hardware_kconfig_state_dir_file_absent(tmp_path):
    # state_dir given but the hardware stage never ran — no file present.
    result = _load_hardware_kconfig({}, state_dir=tmp_path)
    assert result == ({}, {})

def test_load_hardware_kconfig_config_key_wins_over_state_dir(tmp_path):
    # An explicit config path takes precedence over the state_dir fallback.
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    hw = make_hardware_profile(cfg_dir, kconfig={"CONFIG_FROM_CONFIG": "y"})
    make_hardware_profile(tmp_path, kconfig={"CONFIG_FROM_STATE": "y"})
    result = _load_hardware_kconfig(
        {"hardware_profile": str(hw)}, state_dir=tmp_path,
    )
    assert result == ({"CONFIG_FROM_CONFIG": "y"}, {})


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
    # KernelStage.run() stamps the effective src dir in; an empty value here
    # means neither kernel.toml nor the global [paths] had one.
    with pytest.raises(RuntimeError, match="no pkgbuild_src_dir configured"):
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


def test_pkgbuild_path_upstream_pkgname_names_the_dir(tmp_path):
    """Track-upstream mode: the clone dir defaults to upstream_pkgname."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-zen")
    result = _pkgbuild_path({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
        "pkgname": "linux-mine",
    })
    assert result.parent.name == "linux-zen"

def test_pkgbuild_path_srcdir_wins_over_upstream(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "zen-tree")
    result = _pkgbuild_path({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
        "pkgname": "linux-mine",
        "srcdir": "zen-tree",
    })
    assert result.parent.name == "zen-tree"

def test_pkgbuild_path_pkgname_defaults_from_upstream(tmp_path):
    """pkgname omitted → upstream_pkgname satisfies the name requirement."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-zen")
    result = _pkgbuild_path({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
    })
    assert result.parent.name == "linux-zen"


# ---------------------------------------------------------------------------
# _resolve_names / _resolve_source (F40)
# ---------------------------------------------------------------------------

def test_resolve_names_pure_local():
    up, pkg = _km._resolve_names({"pkgname": "linux-sysforge"})
    assert up is None
    assert pkg == "linux-sysforge"

def test_resolve_names_pkgname_defaults_to_upstream():
    up, pkg = _km._resolve_names({"upstream_pkgname": "linux-zen"})
    assert up == "linux-zen"
    assert pkg == "linux-zen"

def test_resolve_names_distinct():
    up, pkg = _km._resolve_names(
        {"upstream_pkgname": "linux-zen", "pkgname": "linux-mine"})
    assert (up, pkg) == ("linux-zen", "linux-mine")

def test_resolve_names_neither_raises():
    with pytest.raises(RuntimeError, match="pkgname"):
        _km._resolve_names({})

def test_resolve_source_explicit_honored(tmp_path):
    for src in ("local", "repo", "aur"):
        assert _km._resolve_source({"source": src}, tmp_path) == src

def test_resolve_source_git_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        _km._resolve_source({"source": "git"}, tmp_path)

def test_resolve_source_auto_existing_plain_dir_is_local(tmp_path):
    d = tmp_path / "linux-sysforge"
    d.mkdir()
    assert _km._resolve_source({"pkgname": "linux-sysforge"}, d) == "local"

def test_resolve_source_auto_existing_git_clone_fetches(tmp_path):
    d = tmp_path / "linux-zen"
    (d / ".git").mkdir(parents=True)
    assert _km._resolve_source(
        {"upstream_pkgname": "linux-zen"}, d) == "repo"

def test_resolve_source_auto_missing_dir_repo_package(tmp_path, monkeypatch):
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package",
                        lambda name: True)
    assert _km._resolve_source(
        {"upstream_pkgname": "linux-zen"}, tmp_path / "linux-zen") == "repo"

def test_resolve_source_auto_missing_dir_aur_package(tmp_path, monkeypatch):
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package",
                        lambda name: False)
    assert _km._resolve_source(
        {"upstream_pkgname": "linux-tkg"}, tmp_path / "linux-tkg") == "aur"


# ---------------------------------------------------------------------------
# _write_kconfig_fragment
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_no_entries_is_noop(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)
    assert result is None
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_hardware_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    assert result is not None
    content = result.read_text()
    assert "CONFIG_MZEN3=y" in content
    assert "# CONFIG_NOUVEAU is not set" in content
    assert "# source: hardware" in content

def test_write_kconfig_fragment_hardware_from_state_dir(tmp_path):
    # Standalone `run kernel`: no config key, profile resolved via state_dir.
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    make_hardware_profile(state_dir, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}

    result, hw_c, _, _, _ = _write_kconfig_fragment(
        kernel_cfg, {}, dry_run=False, state_dir=state_dir,
    )

    assert result is not None
    assert hw_c == 1
    assert "CONFIG_MZEN3=y" in result.read_text()

def test_write_kconfig_fragment_manual_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_HZ_1000", "value": "y"}],
    }

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

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

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

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

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

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

    with _capture_logs() as logs:
        _write_kconfig_fragment(kernel_cfg, config, dry_run=False)
    assert any("CONFIG_MZEN3" in m and "manual override wins" in m
               for m in _warn_messages(logs))

# ---------------------------------------------------------------------------
# kconfig_merge master toggle
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_merge_disabled_is_noop(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig_merge": False,
    }
    config = {"hardware_profile": str(hw)}

    result, hw_c, man_c, dev_c, fdo_c = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    assert result is None
    assert (hw_c, man_c, dev_c, fdo_c) == (0, 0, 0, 0)
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_merge_disabled_removes_stale(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    stale = builds / "linux-git" / "sysforge.config"
    stale.write_text("CONFIG_OLD=y\n")
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig_merge": False,
    }

    result, *_ = _write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

    assert result is None
    assert not stale.exists()

# ---------------------------------------------------------------------------
# _gate2_kconfig_drift — advisory post-build drift check
# ---------------------------------------------------------------------------

def test_gate2_kconfig_drift_warns_on_disabled_option(tmp_path, monkeypatch):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("# source: hardware\nCONFIG_MZEN3=y\nCONFIG_HZ_1000=y\n")
    resolved = tmp_path / ".config"
    resolved.write_text("# CONFIG_MZEN3 is not set\nCONFIG_HZ_1000=y\n")
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: resolved)

    with _capture_logs() as logs:
        _km._gate2_kconfig_drift(tmp_path, fragment)

    warns = _warn_messages(logs)
    assert any("kconfig drift" in m for m in warns)
    assert any("CONFIG_MZEN3" in m and "disabled" in m for m in warns)
    # the surviving option must NOT be reported as drift
    assert not any("CONFIG_HZ_1000" in m for m in warns)

def test_gate2_kconfig_drift_clean_logs_info_no_warn(tmp_path, monkeypatch):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("CONFIG_HZ_1000=y\n")
    resolved = tmp_path / ".config"
    resolved.write_text("CONFIG_HZ_1000=y\nCONFIG_EXTRA=y\n")
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: resolved)

    with _capture_logs() as logs:
        _km._gate2_kconfig_drift(tmp_path, fragment)

    assert not _warn_messages(logs)
    assert any("survived" in m for m in _info_messages(logs))

def test_gate2_kconfig_drift_no_fragment_is_noop(tmp_path, monkeypatch):
    # fragment_path is None (merge disabled / no entries) → check must not run,
    # not even locate the resolved config.
    called = []
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: called.append(d))

    with _capture_logs() as logs:
        _km._gate2_kconfig_drift(tmp_path, None)

    assert called == []
    assert not _warn_messages(logs)

def test_gate2_kconfig_drift_no_resolved_config_skips(tmp_path, monkeypatch):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("CONFIG_HZ_1000=y\n")
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: None)

    with _capture_logs() as logs:
        _km._gate2_kconfig_drift(tmp_path, fragment)

    assert not _warn_messages(logs)
    assert any("resolved .config not found" in m for m in _info_messages(logs))

def test_write_kconfig_fragment_dry_run_no_file(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=True)

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

    result, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "Generated by SysForge" in content
    assert "merge_config.sh" in content


# ---------------------------------------------------------------------------
# _write_kconfig_fragment — device-driven [kconfig_devices]
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_device_entries_merged(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result, hw_count, manual_count, device_count, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_IGC=m" in content
    assert "# source: device" in content
    assert (hw_count, manual_count, device_count) == (1, 0, 1)

def test_write_kconfig_fragment_device_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig_devices={"CONFIG_IGC": "m"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result, _, _, device_count, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    assert result is not None
    assert device_count == 1

def test_write_kconfig_fragment_hardware_wins_over_device(tmp_path):
    # A stale [kconfig_devices] overlap (e.g. nouveau =m for a present NVIDIA
    # GPU) must not override the heuristic [kconfig] disable.
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_DRM_NOUVEAU": "n"},
        kconfig_devices={"CONFIG_DRM_NOUVEAU": "m"},
    )
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    result, hw_count, _, device_count, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "# CONFIG_DRM_NOUVEAU is not set" in content
    assert "CONFIG_DRM_NOUVEAU=m" not in content
    assert (hw_count, device_count) == (1, 0)

def test_write_kconfig_fragment_manual_wins_over_device(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig_devices={"CONFIG_IGC": "m"})
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_IGC", "value": "n"}],
    }
    config = {"hardware_profile": str(hw)}

    result, _, manual_count, device_count, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "# CONFIG_IGC is not set" in content
    assert "CONFIG_IGC=m" not in content
    assert (manual_count, device_count) == (1, 0)

def test_write_kconfig_fragment_device_kconfig_false_skips(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "device_kconfig": False,
    }
    config = {"hardware_profile": str(hw)}

    result, _, _, device_count, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_IGC" not in content
    assert device_count == 0


# ---------------------------------------------------------------------------
# Sample-based FDO (AutoFDO / Propeller) — _resolve_fdo / LLVM gate / fragment
# ---------------------------------------------------------------------------

def _fdo_opts(**kw):
    base = dict(kernel_fdo=None, kernel_propeller=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_fdo_none_when_unset():
    assert _resolve_fdo(_fdo_opts()) == (None, False)


def test_resolve_fdo_valid_modes():
    assert _resolve_fdo(_fdo_opts(kernel_fdo="record")) == ("record", False)
    assert _resolve_fdo(_fdo_opts(kernel_fdo="use", kernel_propeller=True)) == ("use", True)


def test_resolve_fdo_invalid_mode_raises():
    with pytest.raises(RuntimeError, match="invalid --autofdo"):
        _resolve_fdo(_fdo_opts(kernel_fdo="bogus"))


def test_resolve_fdo_propeller_requires_mode():
    with pytest.raises(RuntimeError, match="requires --autofdo"):
        _resolve_fdo(_fdo_opts(kernel_propeller=True))


# Dual-toolchain parity: the LLVM gate passes under clang and refuses gcc.

def test_gate_fdo_llvm_explicit_llvm_passes():
    _gate_fdo_llvm("use", False, "llvm", None)  # no raise


def test_gate_fdo_llvm_explicit_gcc_refuses():
    with pytest.raises(RuntimeError, match="requires the LLVM toolchain"):
        _gate_fdo_llvm("record", False, "gcc", "/usr/bin/gcc")


def test_gate_fdo_llvm_inherited_clang_cc_passes():
    # compiler None but the resolved cc is clang → allowed.
    _gate_fdo_llvm("use", True, None, "/usr/bin/clang")


def test_fdo_is_llvm_env_cc_fallback(monkeypatch):
    monkeypatch.setenv("CC", "/usr/lib/ccache/bin/clang")
    assert _fdo_is_llvm(None, None) is True
    monkeypatch.setenv("CC", "gcc")
    assert _fdo_is_llvm(None, None) is False


def test_fragment_includes_fdo_entries_labeled(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    result, hw, man, dev, fdo = _write_kconfig_fragment(
        kernel_cfg, {}, dry_run=False,
        extra_kconfig={"CONFIG_AUTOFDO_CLANG": "y", "CONFIG_PROPELLER_CLANG": "y"},
    )
    assert fdo == 2
    content = result.read_text()
    assert "CONFIG_AUTOFDO_CLANG=y" in content
    assert "CONFIG_PROPELLER_CLANG=y" in content
    assert "# source: fdo" in content


def test_fragment_manual_overrides_fdo_with_warn(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = {
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_AUTOFDO_CLANG", "value": "n"}],
    }
    with _capture_logs() as logs:
        result, *_ = _write_kconfig_fragment(
            kernel_cfg, {}, dry_run=False,
            extra_kconfig={"CONFIG_AUTOFDO_CLANG": "y"},
        )
    content = result.read_text()
    # manual wins → disabled, even over the feature-requested value
    assert "# CONFIG_AUTOFDO_CLANG is not set" in content
    assert any("CONFIG_AUTOFDO_CLANG" in m and "manual override wins" in m
               for m in _warn_messages(logs))


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

def test_kernel_stage_falls_back_to_global_pkgbuild_src_dir(tmp_path):
    """kernel.toml omits pkgbuild_src_dir → run() resolves it from the global
    [paths] pkgbuild_src_dir instead of hard-failing (Issue 1)."""
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    # kernel.toml WITHOUT pkgbuild_src_dir
    p = tmp_path / "kernel.toml"
    p.write_text('enabled = true\npkgname = "linux-git"\nsource = "local"\n')
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(builds)}}

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run(config, state, make_options(state_dir=tmp_path / "state"))

    mock_build.assert_called_once()
    assert "linux-git" in str(mock_build.call_args[0][0])

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


# ---------------------------------------------------------------------------
# _resolve_compiler / _resolve_bootloader
# ---------------------------------------------------------------------------


def _state_with_toolchain(tmp_path, cc=None, cxx=None):
    s = PipelineState(tmp_path / "state")
    result = {}
    if cc:
        result["cc"] = cc
    if cxx:
        result["cxx"] = cxx
    if result:
        s.set_stage_result("toolchain", result)
    return s


def test_resolve_compiler_cli_wins_over_kernel_toml(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = _state_with_toolchain(tmp_path)
    options = make_options()
    options.compiler = "llvm"
    compiler, cc, cxx = _resolve_compiler({"compiler": "gcc"}, options, state)
    assert compiler == "llvm"
    assert cc == "/usr/bin/clang"
    assert cxx == "/usr/bin/clang++"


def test_resolve_compiler_kernel_toml_wins_over_pipeline_state(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = _state_with_toolchain(tmp_path, cc="/some/pipeline/clang")
    compiler, cc, cxx = _resolve_compiler({"compiler": "gcc"}, make_options(), state)
    assert compiler == "gcc"
    assert cc == "/usr/bin/gcc"
    assert cxx == "/usr/bin/g++"


def test_resolve_compiler_falls_back_to_pipeline_state(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = _state_with_toolchain(tmp_path, cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    compiler, cc, cxx = _resolve_compiler({}, make_options(), state)
    assert compiler is None
    assert cc == "/usr/bin/clang"
    assert cxx == "/usr/bin/clang++"


def test_resolve_compiler_no_override_returns_none(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = PipelineState(tmp_path / "state")
    compiler, cc, cxx = _resolve_compiler({}, make_options(), state)
    assert compiler is None and cc is None and cxx is None


def test_resolve_compiler_rejects_invalid_cli(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = PipelineState(tmp_path / "state")
    options = make_options()
    options.compiler = "icc"
    with pytest.raises(RuntimeError, match="invalid --compiler"):
        _resolve_compiler({}, options, state)


def test_resolve_compiler_rejects_invalid_kernel_toml(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_compiler
    state = PipelineState(tmp_path / "state")
    with pytest.raises(RuntimeError, match="invalid kernel.toml"):
        _resolve_compiler({"compiler": "icc"}, make_options(), state)


def test_resolve_bootloader_cli_wins():
    from sysforge.pipeline.stages.kernel import _resolve_bootloader
    options = make_options()
    options.bootloader = "grub"
    assert _resolve_bootloader({"bootloader": "systemd-boot"}, options) == "grub"


def test_resolve_bootloader_uses_kernel_toml_when_no_cli():
    from sysforge.pipeline.stages.kernel import _resolve_bootloader
    assert _resolve_bootloader({"bootloader": "grub"}, make_options()) == "grub"


def test_resolve_bootloader_default_is_systemd_boot():
    from sysforge.pipeline.stages.kernel import _resolve_bootloader
    assert _resolve_bootloader({}, make_options()) == "systemd-boot"


def test_resolve_bootloader_rejects_invalid_cli():
    from sysforge.pipeline.stages.kernel import _resolve_bootloader
    options = make_options()
    options.bootloader = "lilo"
    with pytest.raises(RuntimeError, match="invalid --bootloader"):
        _resolve_bootloader({}, options)


# ---------------------------------------------------------------------------
# _resolve_subpackages (headers/docs toggles)
# ---------------------------------------------------------------------------

def test_resolve_subpackages_defaults_headers_on_docs_off():
    from sysforge.pipeline.stages.kernel import _resolve_subpackages
    assert _resolve_subpackages({}, make_options()) == (True, False)


def test_resolve_subpackages_kernel_toml_wins_over_default():
    from sysforge.pipeline.stages.kernel import _resolve_subpackages
    cfg = {"build_headers": False, "build_docs": True}
    assert _resolve_subpackages(cfg, make_options()) == (False, True)


def test_resolve_subpackages_cli_headers_off_beats_toml_on():
    from sysforge.pipeline.stages.kernel import _resolve_subpackages
    options = make_options(build_headers=False)
    assert _resolve_subpackages({"build_headers": True}, options) == (False, False)


def test_resolve_subpackages_cli_docs_on_beats_toml_off():
    from sysforge.pipeline.stages.kernel import _resolve_subpackages
    options = make_options(build_docs=True)
    assert _resolve_subpackages({"build_docs": False}, options) == (True, True)


def test_resolve_subpackages_cli_none_falls_through_to_toml():
    from sysforge.pipeline.stages.kernel import _resolve_subpackages
    # RunOptions defaults build_headers/build_docs to None (flag unset).
    options = make_options()
    cfg = {"build_headers": False, "build_docs": True}
    assert _resolve_subpackages(cfg, options) == (False, True)


def test_kernel_stage_threads_subpackages_into_build_options(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", build_headers=False, build_docs=True)
    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.kernel_build_headers is False
    assert build_opts.kernel_build_docs is True


def test_kernel_stage_subpackage_defaults_headers_on_docs_off(tmp_path):
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

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.kernel_build_headers is True
    assert build_opts.kernel_build_docs is False


def test_gate1_warns_when_headers_disabled(monkeypatch):
    """Disabling headers must warn about the DKMS / out-of-tree module risk,
    naming any present DKMS modules."""
    from sysforge.pipeline.stages.kernel import _gate1_preflight
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels", lambda *a, **k: ["linux"])
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space", lambda *a, **k: None)
    monkeypatch.setattr(kernel_safety, "detect_root_topology",
                        lambda *a, **k: kernel_safety.RootTopology())
    monkeypatch.setattr(kernel_safety, "check_mkinitcpio_hooks", lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "list_dkms_modules", lambda: ["nvidia"])

    cfg = {"build_headers": False, "capture_lsmod_snapshot": False}
    with _capture_logs() as logs:
        _gate1_preflight(cfg, make_options(), "linux-custom", dry_run=False)

    joined = "\n".join(_warn_messages(logs))
    assert "-headers subpackage disabled" in joined
    assert "nvidia" in joined


# ---------------------------------------------------------------------------
# Interactive default, --non-interactive, BuildOptions plumbing
# ---------------------------------------------------------------------------


def test_kernel_stage_passes_interactive_true_by_default(tmp_path):
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

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.interactive is True


def test_kernel_stage_non_interactive_flag_flips_to_false(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state")
    opts.non_interactive = True

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.interactive is False


def test_kernel_stage_kernel_toml_interactive_false(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-git"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'interactive = false\n'
    )
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.interactive is False


def test_kernel_stage_cli_compiler_llvm_overrides_pipeline(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = _state_with_toolchain(tmp_path, cc="/usr/bin/gcc", cxx="/usr/bin/g++")

    opts = make_options(state_dir=tmp_path / "state")
    opts.compiler = "llvm"

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.cc_override == "/usr/bin/clang"
    assert build_opts.cxx_override == "/usr/bin/clang++"


def test_kernel_stage_cli_compiler_gcc_overrides_pipeline(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = _state_with_toolchain(tmp_path, cc="/usr/bin/clang", cxx="/usr/bin/clang++")

    opts = make_options(state_dir=tmp_path / "state")
    opts.compiler = "gcc"

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.cc_override == "/usr/bin/gcc"
    assert build_opts.cxx_override == "/usr/bin/g++"


def test_kernel_stage_cli_bootloader_override_beats_kernel_toml(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="systemd-boot")
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state")
    opts.bootloader = "grub"

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    cmds = [c.args[0] for c in mock_sub.call_args_list]
    assert any("grub-mkconfig" in str(c) for c in cmds)
    assert not any("bootctl" in str(c) for c in cmds)


# ---------------------------------------------------------------------------
# Scheduler-routed source sync (--cleansrc / --cleansrc-force)
# ---------------------------------------------------------------------------


def _make_sync_result(status="ok", error=None):
    r = MagicMock()
    r.status = status
    r.error = error
    return r


def test_kernel_stage_no_presync_when_no_update(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler") as mock_sched, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        # default make_options sets no_update=True
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    mock_sched.assert_not_called()


def test_kernel_stage_presyncs_when_update_enabled(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, source="aur")
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(status="ok")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    mock_sched.assert_called_once()
    scheduler_mock.request.assert_called_once()
    req = scheduler_mock.request.call_args.args[0]
    assert req.pkgbase == "linux-git"
    assert req.force_fetch is True


def test_kernel_stage_cleansrc_overrides_no_update(tmp_path):
    """--cleansrc should force a sync even when --no-update is also set."""
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", no_update=True, cleansrc=True)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(status="ok")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    mock_sched.assert_called_once()
    kwargs = mock_sched.call_args.kwargs
    assert kwargs.get("cleansrc") is True


def test_kernel_stage_cleansrc_force_propagates(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", no_update=True, cleansrc_force=True)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(status="ok")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    kwargs = mock_sched.call_args.kwargs
    assert kwargs.get("cleansrc") is True
    assert kwargs.get("cleansrc_force") is True


def test_kernel_stage_sync_failure_raises(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.source_sync import STATUS_FAILED
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, source="aur")
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(
        status=STATUS_FAILED, error="git clone failed"
    )

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run"):
        with pytest.raises(RuntimeError, match="source sync failed"):
            KernelStage().run({}, state, opts)


def _run_kernel_diverged(tmp_path, *, interactive, answer="n"):
    """Drive KernelStage.run() with a diverged source sync.

    Patches the divergence classifier (avoids real git) and the prompt helpers.
    Returns the makepkg_run mock so callers can assert build / no-build.
    """
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.source_sync import STATUS_DIVERGED
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, source="aur")
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(status=STATUS_DIVERGED)

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.primitives.aur.classify_head_vs_upstream",
               return_value=("diverged_upstream", 1, 2)), \
         patch("sysforge.primitives.prompt.is_interactive",
               return_value=interactive), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value=answer), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)
        return mock_build


def test_kernel_stage_sync_diverged_aborts_unattended(tmp_path):
    """No TTY / non-interactive: a diverged kernel source aborts before building."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.source_sync import STATUS_DIVERGED
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, source="aur")
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    scheduler_mock = MagicMock()
    scheduler_mock.request.return_value = _make_sync_result(status=STATUS_DIVERGED)

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.primitives.aur.classify_head_vs_upstream",
               return_value=("diverged_upstream", 1, 2)), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=False), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run"):
        with pytest.raises(RuntimeError, match="diverged source unattended"):
            KernelStage().run({}, state, opts)
    mock_build.assert_not_called()


def test_kernel_stage_sync_diverged_interactive_confirm_builds(tmp_path):
    """Interactive run, user confirms: the diverged build proceeds."""
    mock_build = _run_kernel_diverged(tmp_path, interactive=True, answer="y")
    mock_build.assert_called_once()


def test_kernel_stage_sync_diverged_interactive_decline_aborts(tmp_path):
    """Interactive run, user declines: the build aborts."""
    with pytest.raises(RuntimeError, match="diverged source not"):
        _run_kernel_diverged(tmp_path, interactive=True, answer="n")


# ---------------------------------------------------------------------------
# Sentinel coverage (stage_in_progress.toml)
# ---------------------------------------------------------------------------


def test_kernel_recovery_command_targets_mkinitcpio():
    """The recovery command must regenerate the initramfs — that's the step
    whose absence makes the system unbootable after an interrupted install."""
    from sysforge.pipeline.stages.kernel import _kernel_recovery_command
    cmd = _kernel_recovery_command()
    assert "mkinitcpio" in cmd
    assert cmd.startswith("sudo ")


def test_kernel_stage_writes_sentinel_during_install_and_clears_on_success(tmp_path):
    """Sentinel wraps the install/boot-wiring window (not the build, which
    mutates nothing and runs outside so a Gate 2 abort leaves no sentinel).
    Present while install_built_packages executes, cleared on clean exit."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    seen_during_install = {"present": False, "stage": None}

    def check_sentinel_during_install(*_a, **_kw):
        record = StageSentinel(state_dir).get_active()
        if record is not None:
            seen_during_install["present"] = True
            seen_during_install["stage"] = record.get("stage")
        return []

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch.object(_km, "install_built_packages",
                      side_effect=check_sentinel_during_install), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    assert seen_during_install["present"] is True
    assert seen_during_install["stage"] == "kernel"
    # Cleared on clean exit
    assert StageSentinel(state_dir).get_active() is None


# ---------------------------------------------------------------------------
# Boot-safety gates
# ---------------------------------------------------------------------------

def test_gate1_no_fallback_hard_fails(tmp_path, monkeypatch):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels", lambda *a, **k: [])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch.object(_km, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="no fallback kernel"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    # Hard-fail is before the build — nothing spent, nothing installed.
    mock_build.assert_not_called()
    install_mock.assert_not_called()


def test_gate1_allow_no_fallback_proceeds(tmp_path, monkeypatch):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels", lambda *a, **k: [])

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(
            state_dir=tmp_path / "state", allow_no_fallback=True))
    mock_build.assert_called_once()


def test_gate1_low_boot_space_hard_fails(tmp_path, monkeypatch):
    from sysforge.primitives.kernel_safety import KernelFinding, SEV_ERROR
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space",
                        lambda *a, **k: KernelFinding(
                            SEV_ERROR, "boot_low_space", "/boot has 5 MiB free",
                            "free space", is_brick=True))

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="boot"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    mock_build.assert_not_called()


def test_gate2_brick_aborts_before_install(tmp_path, monkeypatch):
    from sysforge.primitives.kernel_safety import KernelFinding, SEV_ERROR
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    brick = KernelFinding(SEV_ERROR, "boot_kconfig:CONFIG_EXT4_FS",
                          "CONFIG_EXT4_FS is not enabled", "Set it", is_brick=True)
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: tmp_path / ".config")
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: [brick])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch.object(_km, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="boot-critical config"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    install_mock.assert_not_called()


def test_gate2_skip_boot_audit_installs_anyway(tmp_path, monkeypatch):
    from sysforge.primitives.kernel_safety import KernelFinding, SEV_ERROR
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    brick = KernelFinding(SEV_ERROR, "boot_kconfig:CONFIG_EXT4_FS",
                          "CONFIG_EXT4_FS is not enabled", "Set it", is_brick=True)
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: tmp_path / ".config")
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: [brick])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch.object(_km, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(
            state_dir=tmp_path / "state", skip_boot_audit=True))
    install_mock.assert_called_once()


def test_gate2_harvests_kbuild_map_to_state_dir(tmp_path, monkeypatch):
    """Gate 2 parses the built tree (the resolved .config's parent is the
    version-exact source tree), hands the map to the device audit, and caches
    it in the state dir for later hardware-stage runs."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    tree = tmp_path / "kbuild" / "src" / "linux-6.10"
    (tree / "drivers" / "nvme" / "host").mkdir(parents=True)
    (tree / ".config").write_text("CONFIG_EXT4_FS=y\n")
    (tree / "drivers" / "nvme" / "host" / "Makefile").write_text(
        "obj-$(CONFIG_BLK_DEV_NVME) += nvme.o\n")
    (tree / "include" / "config").mkdir(parents=True)
    (tree / "include" / "config" / "kernel.release").write_text("6.10.0-test\n")

    captured = {}
    def fake_enumerate(*a, **k):
        captured.update(k)
        return []
    monkeypatch.setattr(device_probe, "enumerate_devices", fake_enumerate)
    monkeypatch.setattr(_km, "_resolve_built_config", lambda d: tree / ".config")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    # The audit's device enumeration received the tree-derived map …
    assert captured.get("kconfig_map") == {"nvme": "CONFIG_BLK_DEV_NVME"}
    # … and the cache landed in the state dir with provenance.
    cache = tmp_path / "state" / kbuild_map.KBUILD_MAP_FILENAME
    assert kbuild_map.load_map(cache) == (
        {"nvme": "CONFIG_BLK_DEV_NVME"}, "6.10.0-test",
    )


def test_gate3_unbootable_artifacts_raise_after_install(tmp_path, monkeypatch):
    from sysforge.primitives.kernel_safety import KernelFinding, SEV_ERROR
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    brick = KernelFinding(SEV_ERROR, "boot_entry_missing",
                          "no boot entry references the kernel", "add one",
                          is_brick=True)
    monkeypatch.setattr(kernel_safety, "verify_boot_artifacts",
                        lambda *a, **k: [brick])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch.object(_km, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="boot-readiness"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    # The install did happen — Gate 3 fires post-install.
    install_mock.assert_called_once()


def test_kernel_stage_preserves_sentinel_on_mkinitcpio_failure(tmp_path):
    """mkinitcpio failure inside the sentinel scope must leave the sentinel
    behind so the next sysforge invocation hits the recovery prompt."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    def fail_mkinitcpio(cmd, **kwargs):
        if "mkinitcpio" in str(cmd):
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run",
               side_effect=fail_mkinitcpio):
        with pytest.raises(RuntimeError, match="mkinitcpio"):
            KernelStage().run({}, state, make_options(state_dir=state_dir))

    record = StageSentinel(state_dir).get_active()
    assert record is not None
    assert record["stage"] == "kernel"
    assert "mkinitcpio" in record["recovery_cmd"]


def test_kernel_stage_sentinel_records_compiler_metadata_gcc(tmp_path):
    """The sentinel records the gcc-path compiler choice for the recovery prompt."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    seen = {}

    def fail_mkinitcpio(cmd, **kwargs):
        if "mkinitcpio" in str(cmd):
            record = StageSentinel(state_dir).get_active()
            if record is not None:
                seen.update(record)
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    opts = make_options(state_dir=state_dir)
    opts.compiler = "gcc"

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run",
               side_effect=fail_mkinitcpio):
        with pytest.raises(RuntimeError):
            KernelStage().run({}, state, opts)

    assert seen.get("compiler") == "gcc"
    assert seen.get("pkgname") == "linux-git"


def test_kernel_stage_sentinel_records_compiler_metadata_llvm(tmp_path):
    """Parity test for the llvm path — dual-toolchain coverage per CLAUDE.md."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    seen = {}

    def fail_mkinitcpio(cmd, **kwargs):
        if "mkinitcpio" in str(cmd):
            record = StageSentinel(state_dir).get_active()
            if record is not None:
                seen.update(record)
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    opts = make_options(state_dir=state_dir)
    opts.compiler = "llvm"

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run",
               side_effect=fail_mkinitcpio):
        with pytest.raises(RuntimeError):
            KernelStage().run({}, state, opts)

    assert seen.get("compiler") == "llvm"
    assert seen.get("pkgname") == "linux-git"


def test_kernel_stage_passes_source_and_owner_stage_to_makepkg(tmp_path):
    """Kernel stage must hand the resolved source + owner_stage='kernel' to
    BuildOptions so build_state records who owns the package."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-custom")
    # kernel.toml without an explicit `source` — must default to "local"
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-custom"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "none"\n'
    )
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    mock_run.assert_called_once()
    opts = mock_run.call_args.kwargs["options"]
    assert opts.source == "local"
    assert opts.owner_stage == "kernel"


def test_kernel_stage_local_source_skips_presync(tmp_path):
    """When source = "local" (default), source-sync must be skipped — there's
    no remote to fetch against."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-custom")
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-custom"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "none"\n'
    )
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler") as mock_sched, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    mock_sched.assert_not_called()


def test_kernel_stage_invalid_source_rejected(tmp_path):
    """Unknown source values in kernel.toml are an error."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-custom")
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-custom"\n'
        'source = "bogus"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "none"\n'
    )
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p):
        with pytest.raises(RuntimeError, match="invalid kernel.toml source"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))


# ---------------------------------------------------------------------------
# 3.3 — Variant-driven compiler nudge
# ---------------------------------------------------------------------------

def _state_with_variant(tmp_path, variant, cc=None, cxx=None):
    """Helper: build a PipelineState with the toolchain stage's variant field set."""
    s = PipelineState(tmp_path / "state")
    result = {"variant": variant}
    if cc:
        result["cc"] = cc
    if cxx:
        result["cxx"] = cxx
    s.set_stage_result("toolchain", result)
    return s


def _run_kernel_with_state(tmp_path, kernel_cfg_state, opts_override=None):
    """Run KernelStage in a fully-mocked environment, returning the captured logs."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state, p = kernel_cfg_state
    opts = opts_override or make_options(state_dir=tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         _capture_logs() as logs, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel._probe_installed_bootloader",
               return_value={"systemd-boot"}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    return logs


def _warn_messages(logs):
    return [str(c.args[1]) for c in logs.warn.call_args_list]


def _info_messages(logs):
    return [str(c.args[1]) for c in logs.info.call_args_list]


def test_run_logs_pgo_llvm_nudge_when_compiler_unset(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert any("pgo_llvm" in m and "PGO clang" in m for m in _warn_messages(logs)), \
        f"expected pgo_llvm nudge, got warns: {_warn_messages(logs)}"


def test_run_logs_stock_llvm_info_when_compiler_unset(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = _state_with_variant(tmp_path, "stock_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert any("stock_llvm" in m and "inherit clang" in m for m in _info_messages(logs)), \
        f"expected stock_llvm info, got infos: {_info_messages(logs)}"
    # pgo_llvm warn must NOT fire
    assert not any("pgo_llvm" in m and "PGO clang" in m for m in _warn_messages(logs))


def test_run_no_nudge_when_compiler_explicit(tmp_path):
    """kernel.toml compiler explicitly set → no variant-inheritance nudge."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-git"\n'
        'source = "local"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "systemd-boot"\n'
        'compiler = "gcc"\n'
    )
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert not any("PGO clang" in m or "inherit clang" in m
                   for m in _warn_messages(logs) + _info_messages(logs))


def test_run_no_nudge_for_gcc_variant(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = _state_with_variant(tmp_path, "gcc",
                                cc="/usr/bin/gcc", cxx="/usr/bin/g++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert not any("inherit" in m for m in _warn_messages(logs) + _info_messages(logs))


def test_run_no_nudge_for_system_variant(tmp_path):
    """No toolchain stage result → variant=='system' → no nudge."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")  # no set_stage_result → variant=system
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert not any("inherit" in m for m in _warn_messages(logs) + _info_messages(logs))


# ---------------------------------------------------------------------------
# A1 — Per-kernel toolchain drift check
# ---------------------------------------------------------------------------

def _seed_build_state(state_dir, pkgname, toolchain_variant, pkgbuild_dir):
    from sysforge.primitives.build_state import BuildState
    bs = BuildState(state_dir)
    bs.record(
        pkgname=pkgname, pkgver="6.10", pkgrel="1", epoch="0",
        pkgbase=pkgname, pkgbuild_dir=pkgbuild_dir,
        toolchain_variant=toolchain_variant,
    )
    bs.save()


def test_run_warns_on_variant_drift(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state_dir = tmp_path / "state"
    _seed_build_state(state_dir, "linux-git", "stock_llvm", builds / "linux-git")
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    drift = [m for m in _warn_messages(logs)
             if "stock_llvm" in m and "pgo_llvm" in m and "Rebuilding" in m]
    assert drift, f"expected drift warn, got warns: {_warn_messages(logs)}"


def test_run_silent_when_variants_match(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state_dir = tmp_path / "state"
    _seed_build_state(state_dir, "linux-git", "pgo_llvm", builds / "linux-git")
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert not any("Rebuilding will switch toolchains" in m for m in _warn_messages(logs))


def test_run_silent_when_recorded_variant_absent(tmp_path):
    """Back-compat: no recorded variant on installed kernel → no drift warn."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    # No build_state seeded → BuildState.get(pkgname) returns None
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert not any("Rebuilding will switch toolchains" in m for m in _warn_messages(logs))


# ---------------------------------------------------------------------------
# A2 — Bootloader-installed preflight
# ---------------------------------------------------------------------------

def test_probe_bootloader_systemd_via_loader_conf(tmp_path):
    from sysforge.pipeline.stages.kernel import _probe_installed_bootloader

    def fake_exists(self):
        return str(self) == "/boot/loader/loader.conf"

    with patch("pathlib.Path.exists", fake_exists):
        assert _probe_installed_bootloader() == {"systemd-boot"}


def test_probe_bootloader_grub_via_grub_cfg(tmp_path):
    from sysforge.pipeline.stages.kernel import _probe_installed_bootloader

    def fake_exists(self):
        return str(self) == "/boot/grub/grub.cfg"

    with patch("pathlib.Path.exists", fake_exists):
        assert _probe_installed_bootloader() == {"grub"}


def test_probe_bootloader_dual_boot(tmp_path):
    from sysforge.pipeline.stages.kernel import _probe_installed_bootloader

    def fake_exists(self):
        return str(self) in ("/boot/loader/loader.conf", "/boot/grub/grub.cfg")

    with patch("pathlib.Path.exists", fake_exists):
        assert _probe_installed_bootloader() == {"systemd-boot", "grub"}


def test_run_warns_when_bootloader_mismatch(tmp_path):
    """kernel.toml bootloader = grub, only systemd-boot detected → WARN, build proceeds."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="grub")
    state = PipelineState(tmp_path / "state")

    with patch.object(_km, "KERNEL_PATH", p), \
         _capture_logs() as logs, \
         patch("sysforge.pipeline.stages.kernel.makepkg_run"), \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel._probe_installed_bootloader",
               return_value={"systemd-boot"}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    mismatch = [m for m in _warn_messages(logs)
                if "bootloader = 'grub'" in m and "not detected" in m]
    assert mismatch, f"expected bootloader mismatch warn, got: {_warn_messages(logs)}"


# ---------------------------------------------------------------------------
# A3 — pkgname ↔ PKGBUILD pkgbase consistency check
# ---------------------------------------------------------------------------

def _write_pkgbuild_with(builds, dirname, contents):
    d = builds / dirname
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(contents)
    return pb


def test_validate_pkgname_match_split_kernel(tmp_path):
    from sysforge.pipeline.stages.kernel import _validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgbase=linux-custom\n"
        "pkgname=(linux-custom linux-custom-headers)\n"
        "pkgver=6.10\npkgrel=1\n"
    )
    # No raise.
    _validate_pkgname_matches_pkgbuild(pb, "linux-custom")


def test_validate_pkgname_match_simple(tmp_path):
    from sysforge.pipeline.stages.kernel import _validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgname=linux-custom\npkgver=6.10\npkgrel=1\n"
    )
    _validate_pkgname_matches_pkgbuild(pb, "linux-custom")


def test_validate_pkgname_typo_raises(tmp_path):
    from sysforge.pipeline.stages.kernel import _validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgbase=linux-custom\n"
        "pkgname=(linux-custom linux-custom-headers)\n"
        "pkgver=6.10\npkgrel=1\n"
    )
    with pytest.raises(RuntimeError, match="does not match.*pkgbase"):
        _validate_pkgname_matches_pkgbuild(pb, "linux-custm")


def _ui_messages(logs):
    return [str(c.args[1]) for c in logs.ui.call_args_list]


# ---------------------------------------------------------------------------
# B1 — Resolution-summary preview
# ---------------------------------------------------------------------------

def test_dry_run_emits_resolution_summary(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = _state_with_variant(tmp_path, "pgo_llvm",
                                cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    p = make_kernel_toml(tmp_path, builds)
    opts = make_options(state_dir=tmp_path / "state", dry_run=True)
    logs = _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    ui = _ui_messages(logs)
    assert any("Kernel build plan:" in m for m in ui), f"no summary header in {ui}"
    assert any("compiler:" in m for m in ui)
    assert any("variant:" in m and "pgo_llvm" in m for m in ui)
    assert any("gates:" in m for m in ui)


def test_resolution_summary_names_compiler_origin(tmp_path):
    """Explicit kernel.toml compiler is reported with its origin."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'pkgname = "linux-git"\n'
        'source = "local"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "systemd-boot"\n'
        'compiler = "llvm"\n'
    )
    opts = make_options(state_dir=tmp_path / "state", dry_run=True)
    logs = _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    ui = _ui_messages(logs)
    assert any("compiler:" in m and "llvm" in m and "kernel.toml" in m for m in ui), \
        f"compiler origin not surfaced: {ui}"


# ---------------------------------------------------------------------------
# F37 — kconfig_targets wiring into the kernel stage
# ---------------------------------------------------------------------------

def _write_kconfig_targets_toml(tmp_path, builds, targets, *, interactive=None):
    lines = [
        'enabled = true',
        'pkgname = "linux-git"',
        'source = "local"',
        f'pkgbuild_src_dir = "{builds}"',
        'bootloader = "systemd-boot"',
        f'kconfig_targets = {targets!r}'.replace("'", '"'),
    ]
    if interactive is not None:
        lines.append(f'interactive = {str(interactive).lower()}')
    p = tmp_path / "kernel.toml"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_kconfig_targets_passed_to_makepkg_options_when_configured(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = _write_kconfig_targets_toml(tmp_path, builds, ["olddefconfig"], interactive=False)

    import sysforge.pipeline.stages.kernel as _km
    opts = make_options(state_dir=tmp_path / "state")
    with patch.object(_km, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel._probe_installed_bootloader",
               return_value={"systemd-boot"}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    assert mock_run.called
    build_opts = mock_run.call_args.kwargs["options"]
    assert build_opts.kconfig_targets == ["olddefconfig"]


def test_kconfig_targets_unset_passes_none(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)

    import sysforge.pipeline.stages.kernel as _km
    opts = make_options(state_dir=tmp_path / "state")
    with patch.object(_km, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel._probe_installed_bootloader",
               return_value={"systemd-boot"}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    assert mock_run.called
    build_opts = mock_run.call_args.kwargs["options"]
    assert build_opts.kconfig_targets is None


def test_kconfig_targets_invalid_aborts_before_build(tmp_path):
    """A bad kconfig_targets list raises ValueError pre-build — makepkg never runs."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = _write_kconfig_targets_toml(tmp_path, builds, ["randconfig"])

    import sysforge.pipeline.stages.kernel as _km
    opts = make_options(state_dir=tmp_path / "state")
    with patch.object(_km, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_run, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel._probe_installed_bootloader",
               return_value={"systemd-boot"}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(ValueError, match="randconfig"):
            KernelStage().run({}, state, opts)

    mock_run.assert_not_called()


def test_kconfig_targets_summary_line_reports_configured_sequence(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = _write_kconfig_targets_toml(
        tmp_path, builds, ["localmodconfig", "olddefconfig", "nconfig"],
        interactive=True,
    )
    opts = make_options(state_dir=tmp_path / "state", dry_run=True)
    logs = _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    ui = _ui_messages(logs)
    assert any(
        "kconfig:" in m and "localmodconfig → olddefconfig → nconfig (configured)" in m
        for m in ui
    ), f"configured kconfig summary not found in {ui}"


# ---------------------------------------------------------------------------
# B2 — Missing-PKGBUILD hint (interrupted --cleansrc)
# ---------------------------------------------------------------------------

def test_pkgbuild_path_dir_exists_but_no_pkgbuild_hints_cleansrc(tmp_path):
    from sysforge.pipeline.stages.kernel import _pkgbuild_path

    builds = tmp_path / "builds"
    (builds / "linux-git").mkdir(parents=True)  # dir exists, no PKGBUILD inside
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}

    with pytest.raises(RuntimeError, match="interrupted --cleansrc"):
        _pkgbuild_path(kernel_cfg)


def test_pkgbuild_path_dir_absent_keeps_clone_hint(tmp_path):
    from sysforge.pipeline.stages.kernel import _pkgbuild_path

    builds = tmp_path / "builds"  # nothing created
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}

    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        _pkgbuild_path(kernel_cfg)


# ---------------------------------------------------------------------------
# B3 — Kernel build lock (shared primitive)
# ---------------------------------------------------------------------------

def test_kernel_stage_refuses_when_lock_held(tmp_path):
    """A held state-dir lock makes a concurrent kernel run refuse."""
    from sysforge.primitives.build_lock import build_lock

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Hold the lock the stage will try to acquire.
    with build_lock(state_dir / "kernel-build.lock", label="kernel"):
        with pytest.raises(RuntimeError, match="Another sysforge kernel build"):
            _run_kernel_with_state(
                tmp_path, (state, p),
                opts_override=make_options(state_dir=state_dir),
            )


# ---------------------------------------------------------------------------
# C2 — Variant-stamped kconfig fragment header
# ---------------------------------------------------------------------------

def test_kconfig_fragment_header_carries_provenance(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_KVM": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    path, _, _, _, _ = _write_kconfig_fragment(
        kernel_cfg, config, dry_run=False,
        provenance="toolchain variant: pgo_llvm  cc: /usr/bin/clang",
    )
    content = path.read_text()
    assert "# toolchain variant: pgo_llvm  cc: /usr/bin/clang" in content


def test_kconfig_fragment_no_provenance_when_omitted(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_KVM": "y"})
    kernel_cfg = {"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)}
    config = {"hardware_profile": str(hw)}

    path, _, _, _, _ = _write_kconfig_fragment(kernel_cfg, config, dry_run=False)
    content = path.read_text()
    assert "toolchain variant:" not in content


# ---------------------------------------------------------------------------
# C3 — Standalone interactive nudge
# ---------------------------------------------------------------------------

def test_interactive_run_emits_nudge(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)  # interactive defaults to True
    logs = _run_kernel_with_state(tmp_path, (state, p))

    assert any("Running interactively" in m for m in _info_messages(logs)), \
        f"expected interactive nudge, got: {_info_messages(logs)}"


def test_non_interactive_run_omits_nudge(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)
    opts = make_options(state_dir=tmp_path / "state")
    opts.non_interactive = True
    logs = _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    assert not any("Running interactively" in m for m in _info_messages(logs))


def test_dry_run_omits_interactive_nudge(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)
    opts = make_options(state_dir=tmp_path / "state", dry_run=True)
    logs = _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    assert not any("Running interactively" in m for m in _info_messages(logs))


# ---------------------------------------------------------------------------
# _check_pkgname_repo_collision (pkgname shadows a pacman repo package)
# ---------------------------------------------------------------------------

def test_pkgname_collision_no_match_is_noop():
    from sysforge.pipeline.stages.kernel import _check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False):
        # No raise, no prompt.
        _check_pkgname_repo_collision("linux-sysforge", opts)


def test_pkgname_collision_dry_run_warns_no_prompt():
    from sysforge.pipeline.stages.kernel import _check_pkgname_repo_collision

    opts = make_options(dry_run=True)
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice") as mock_prompt:
        _check_pkgname_repo_collision("linux", opts)  # no raise
    mock_prompt.assert_not_called()


def test_pkgname_collision_unattended_aborts():
    from sysforge.pipeline.stages.kernel import _check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=False):
        with pytest.raises(RuntimeError, match="Aborting unattended"):
            _check_pkgname_repo_collision("linux", opts)


def test_pkgname_collision_interactive_confirm_proceeds():
    from sysforge.pipeline.stages.kernel import _check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value="y"):
        _check_pkgname_repo_collision("linux", opts)  # no raise


def test_pkgname_collision_interactive_decline_aborts():
    from sysforge.pipeline.stages.kernel import _check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value="n"):
        with pytest.raises(RuntimeError, match="not confirmed"):
            _check_pkgname_repo_collision("linux", opts)


# ---------------------------------------------------------------------------
# _resolve_base_config / _write_base_config (configurable kconfig base)
# ---------------------------------------------------------------------------

def test_resolve_base_config_pkgbuild_default_is_noop():
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    label, text = _resolve_base_config({})
    assert label == "pkgbuild"
    assert text is None


def test_resolve_base_config_running_seeds(monkeypatch):
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y\n")
    label, text = _resolve_base_config({"base_config": "running"})
    assert label == "running"
    assert text == "CONFIG_FOO=y\n"


def test_resolve_base_config_running_missing_warns_and_falls_back(monkeypatch):
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: None)
    label, text = _resolve_base_config({"base_config": "running"})
    assert label == "running"
    assert text is None  # falls back to the PKGBUILD base


def test_resolve_base_config_path(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    cfg_file = tmp_path / "my.config"
    cfg_file.write_text("CONFIG_BAR=m\n")
    label, text = _resolve_base_config({"base_config": str(cfg_file)})
    assert text == "CONFIG_BAR=m\n"


def test_resolve_base_config_missing_path_raises(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    with pytest.raises(RuntimeError, match="does not exist"):
        _resolve_base_config({"base_config": str(tmp_path / "nope.config")})


def test_resolve_base_config_invalid_value_raises():
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    with pytest.raises(RuntimeError, match="invalid kernel.toml base_config"):
        _resolve_base_config({"base_config": ""})


def test_resolve_base_config_cli_overrides_config(monkeypatch):
    # --base-config wins over kernel.toml base_config.
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y\n")
    opts = SimpleNamespace(base_config="running")
    label, text = _resolve_base_config({"base_config": "pkgbuild"}, opts)
    assert label == "running"
    assert text == "CONFIG_FOO=y\n"


def test_resolve_base_config_cli_none_falls_back_to_config():
    # options with base_config=None defers to the kernel.toml value.
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    opts = SimpleNamespace(base_config=None)
    label, text = _resolve_base_config({"base_config": "pkgbuild"}, opts)
    assert label == "pkgbuild"
    assert text is None


def test_resolve_base_config_cli_path(tmp_path):
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    cfg_file = tmp_path / "cli.config"
    cfg_file.write_text("CONFIG_CLI=y\n")
    opts = SimpleNamespace(base_config=str(cfg_file))
    label, text = _resolve_base_config({"base_config": "pkgbuild"}, opts)
    assert text == "CONFIG_CLI=y\n"


def test_resolve_base_config_cli_missing_path_raises(tmp_path):
    # A bad CLI value is reported against --base-config, not kernel.toml.
    from sysforge.pipeline.stages.kernel import _resolve_base_config

    opts = SimpleNamespace(base_config=str(tmp_path / "nope.config"))
    with pytest.raises(RuntimeError, match="--base-config path does not exist"):
        _resolve_base_config({}, opts)


def test_write_base_config_writes_file(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import _write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = {
        "pkgname": "linux-sysforge",
        "pkgbuild_src_dir": str(builds),
        "base_config": "running",
    }
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y")
    label = _write_base_config(kernel_cfg, dry_run=False)
    assert label == "running"
    out = builds / "linux-sysforge" / "sysforge.base.config"
    assert out.read_text() == "CONFIG_FOO=y\n"  # trailing newline added


def test_write_base_config_dry_run_no_file(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import _write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = {
        "pkgname": "linux-sysforge",
        "pkgbuild_src_dir": str(builds),
        "base_config": "running",
    }
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y")
    _write_base_config(kernel_cfg, dry_run=True)
    assert not (builds / "linux-sysforge" / "sysforge.base.config").exists()


def test_write_base_config_pkgbuild_default_writes_nothing(tmp_path):
    from sysforge.pipeline.stages.kernel import _write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = {"pkgname": "linux-sysforge", "pkgbuild_src_dir": str(builds)}
    label = _write_base_config(kernel_cfg, dry_run=False)
    assert label == "pkgbuild"
    assert not (builds / "linux-sysforge" / "sysforge.base.config").exists()


# ---------------------------------------------------------------------------
# B6: the pre-nconfig pause now lives inside the patched PKGBUILD's prepare()
# (pkgbuild_patcher.patch_kernel_kconfig_apply), after the merges and right
# before `make nconfig` — see tests/test_patcher.py. The stage no longer emits
# a pre-makepkg pause, which fired before the in-prepare() merges.
# ---------------------------------------------------------------------------


def test_kernel_stage_bootstraps_missing_tree_via_sync(tmp_path):
    """F40: sync runs BEFORE the PKGBUILD path is required, so a missing tree
    is cloned by the scheduler instead of aborting with 'clone it first'."""
    builds = tmp_path / "builds"
    builds.mkdir()
    p = tmp_path / "kernel.toml"
    p.write_text(
        'enabled = true\n'
        'upstream_pkgname = "linux-git"\n'
        'source = "aur"\n'
        f'pkgbuild_src_dir = "{builds}"\n'
        'bootloader = "systemd-boot"\n'
    )
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state", no_update=False)

    def clone_on_request(req):
        make_pkgbuild(builds, "linux-git")
        return _make_sync_result(status="cloned")

    scheduler_mock = MagicMock()
    scheduler_mock.request.side_effect = clone_on_request

    with patch.object(_km, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.pipeline.stages.kernel.makepkg_run") as mock_build, \
         patch("sysforge.pipeline.stages.kernel.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    scheduler_mock.request.assert_called_once()
    mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# _capture_lsmod_snapshot / _merge_lsmod — accumulating snapshot (F37)
# ---------------------------------------------------------------------------

LSMOD_HEADER = "Module                  Size  Used by\n"


def _mock_lsmod(monkeypatch, stdout):
    real_run = _km.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv == ["lsmod"]:
            return MagicMock(returncode=0, stdout=stdout)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(_km.subprocess, "run", fake_run)


def test_snapshot_accumulates_across_captures(tmp_path, monkeypatch):
    snap = tmp_path / "lsmod.snapshot"
    snap.write_text(LSMOD_HEADER + "wireguard 90112 0\n")
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    _capture_lsmod_snapshot(tmp_path, dry_run=False)
    text = snap.read_text()
    assert "wireguard" in text  # retained from prior snapshot
    assert "ext4" in text  # newly merged
    assert text.startswith("Module")
    assert text.count("wireguard") == 1  # no duplicate rows


def test_snapshot_fresh_capture_when_missing(tmp_path, monkeypatch):
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    _capture_lsmod_snapshot(tmp_path, dry_run=False)
    assert "ext4" in (tmp_path / "lsmod.snapshot").read_text()


def test_snapshot_corrupt_prior_degrades_to_fresh(tmp_path, monkeypatch):
    (tmp_path / "lsmod.snapshot").write_bytes(b"\x00\xff garbage")
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    with _capture_logs() as logs:
        _capture_lsmod_snapshot(tmp_path, dry_run=False)
    text = (tmp_path / "lsmod.snapshot").read_text()
    assert "ext4" in text
    assert "garbage" not in text
    assert _warn_messages(logs)


def test_merge_lsmod_current_wins_on_conflict():
    prior = LSMOD_HEADER + "wireguard 90112 0\n"
    current = LSMOD_HEADER + "wireguard 90112 1\next4 999424 1\n"
    merged = _merge_lsmod(prior, current)
    assert merged.startswith("Module")
    assert "wireguard 90112 1" in merged
    assert "ext4" in merged
    assert merged.count("wireguard") == 1
