"""
test_stage_kernel.py — unit tests for the kernel stage.

Covers all pure-logic functions. Subprocess calls (lsmod, mkinitcpio,
bootctl, makepkg) are mocked — nothing real runs.
"""
import contextlib
import types
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sysforge.pipeline.stages.base import RunOptions
import dataclasses

from sysforge.pipeline.stages.kernel.config import KernelConfig
from sysforge.pipeline.stages.kernel import (
    KernelStage,
    capture_lsmod_snapshot,
    fdo_is_llvm,
    format_kconfig_line,
    gate_fdo_llvm,
    load_hardware_kconfig,
    load_kernel_config,
    merge_lsmod,
    pkgbuild_path,
    resolve_fdo,
    resolve_keep_hotplug_drivers,
    stage_lsmod_snapshot,
    validate_manual_kconfig,
    write_hotplug_fragment,
    write_kconfig_fragment,
    resolve_kconfig_targets,
)
from sysforge.pipeline.state import PipelineState
from sysforge.primitives import device_probe, kbuild_map, kernel_fdo, kernel_safety
import sysforge.log as sysforge_log
import sysforge.pipeline.stages.kernel as _km
from sysforge.pipeline.stages.kernel import fdo as kfdo



# Stage tests patch the build with return_value=None: makepkg_run returns the
# build tree it used (3.2.0-B37), and a bare MagicMock would read as a fresh
# build with no .config, which Gate 2 refuses.
_MAKEPKG_RUN = "sysforge.pipeline.stages.kernel.stage.makepkg_run"


def kcfg(d=None, **kw) -> KernelConfig:
    """Build a :class:`KernelConfig` from kernel.toml-shaped keys.

    Tests keep expressing intent in the TOML a user actually writes while
    exercising the same ``from_toml`` path the stage uses, so a defaulting bug
    cannot pass here and fail in production (3.2.0-F6).
    """
    return KernelConfig.from_toml({**(d or {}), **kw})


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
    # 3.4.0-F1: the stage installs missing repo deps itself (pacman -T, then
    # sudo pacman -S). Never from a test; tests of that step re-patch it.
    from sysforge import build_core as _bc
    monkeypatch.setattr(_bc, "install_missing_repo_deps", lambda *a, **k: None)
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
    monkeypatch.setattr(_km.stage, "install_built_packages", lambda *a, **k: [])
    # B4/B5: the stage now warms sudo credentials at build entry and probes
    # them again before the install sentinel. Both shell out to `sudo -v`;
    # neutralize them so run()-flow tests stay hermetic. The dedicated B4 tests
    # below re-patch authenticate to drive the failure branch.
    monkeypatch.setattr(_km.stage.sudo_session, "authenticate", lambda: True)
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
# format_kconfig_line
# ---------------------------------------------------------------------------

def test_format_kconfig_y():
    assert format_kconfig_line("CONFIG_KVM", "y") == "CONFIG_KVM=y"

def test_format_kconfig_m():
    assert format_kconfig_line("CONFIG_KVM", "m") == "CONFIG_KVM=m"

def test_format_kconfig_n():
    assert format_kconfig_line("CONFIG_NOUVEAU", "n") == "# CONFIG_NOUVEAU is not set"

def test_format_kconfig_string():
    assert (format_kconfig_line("CONFIG_LOCALVERSION", "-sysforge")
            == 'CONFIG_LOCALVERSION="-sysforge"')

def test_format_kconfig_integer_string():
    assert format_kconfig_line("CONFIG_HZ", "1000") == 'CONFIG_HZ="1000"'


# ---------------------------------------------------------------------------
# validate_manual_kconfig
# ---------------------------------------------------------------------------

def test_validate_kconfig_valid():
    entries = [
        {"option": "CONFIG_HZ_1000", "value": "y"},
        {"option": "CONFIG_NOUVEAU", "value": "n"},
        {"option": "CONFIG_LOCALVERSION", "value": "-sysforge"},
    ]
    result = validate_manual_kconfig(entries)
    assert result == {
        "CONFIG_HZ_1000": "y",
        "CONFIG_NOUVEAU": "n",
        "CONFIG_LOCALVERSION": "-sysforge",
    }

def test_validate_kconfig_empty_list():
    assert validate_manual_kconfig([]) == {}

def test_validate_kconfig_missing_option():
    with pytest.raises(RuntimeError, match="missing 'option'"):
        validate_manual_kconfig([{"value": "y"}])

def test_validate_kconfig_bad_option_format():
    with pytest.raises(RuntimeError, match="invalid option"):
        validate_manual_kconfig([{"option": "hz_1000", "value": "y"}])

def test_validate_kconfig_bad_option_lowercase():
    with pytest.raises(RuntimeError, match="invalid option"):
        validate_manual_kconfig([{"option": "CONFIG_hz", "value": "y"}])

def test_validate_kconfig_missing_config_prefix():
    with pytest.raises(RuntimeError, match="invalid option"):
        validate_manual_kconfig([{"option": "HZ_1000", "value": "y"}])

def test_validate_kconfig_empty_value():
    with pytest.raises(RuntimeError, match="empty value"):
        validate_manual_kconfig([{"option": "CONFIG_HZ", "value": ""}])

def test_validate_kconfig_table_form_is_rejected_with_guidance():
    """A `[kconfig]` table (wrong schema) must name the right one, not AttributeError."""
    with pytest.raises(RuntimeError, match=r"\[\[kconfig\]\]"):
        validate_manual_kconfig({"CONFIG_HZ_1000": "y"})


def test_validate_kconfig_captured_top_level_key_names_the_key():
    """A top-level key swallowed by a live `[kconfig]` header is reported by name."""
    with pytest.raises(RuntimeError, match="kconfig_targets"):
        validate_manual_kconfig({"kconfig_targets": ["olddefconfig"]})


def test_validate_kconfig_non_table_entry_is_rejected():
    with pytest.raises(RuntimeError, match=r"entry \[0\]"):
        validate_manual_kconfig(["CONFIG_HZ_1000"])


def test_validate_kconfig_duplicate_option():
    entries = [
        {"option": "CONFIG_HZ_1000", "value": "y"},
        {"option": "CONFIG_HZ_1000", "value": "n"},
    ]
    with pytest.raises(RuntimeError, match="duplicate option"):
        validate_manual_kconfig(entries)


# ---------------------------------------------------------------------------
# resolve_kconfig_targets
# ---------------------------------------------------------------------------

def test_resolve_kconfig_targets_unset_returns_none():
    assert resolve_kconfig_targets(kcfg({}), interactive=True) is None


def test_resolve_kconfig_targets_ui_target_reordered_last():
    cfg = kcfg({"kconfig_targets": ["nconfig", "localmodconfig", "olddefconfig"]})
    assert resolve_kconfig_targets(cfg, interactive=True) == [
        "localmodconfig",
        "olddefconfig",
        "nconfig",
    ]


def test_resolve_kconfig_targets_two_ui_targets_rejected():
    cfg = kcfg({"kconfig_targets": ["nconfig", "menuconfig"]})
    with pytest.raises(ValueError, match="at most one"):
        resolve_kconfig_targets(cfg, interactive=True)


def test_resolve_kconfig_targets_randconfig_rejected():
    with pytest.raises(ValueError, match="randconfig"):
        resolve_kconfig_targets(kcfg({"kconfig_targets": ["randconfig"]}), interactive=True)


def test_resolve_kconfig_targets_unknown_target_rejected():
    with pytest.raises(ValueError, match="unknown"):
        resolve_kconfig_targets(kcfg({"kconfig_targets": ["bogusconfig"]}), interactive=True)


def test_resolve_kconfig_targets_prompting_target_rejected_when_non_interactive():
    with pytest.raises(ValueError, match="olddefconfig"):
        resolve_kconfig_targets(kcfg({"kconfig_targets": ["oldconfig"]}), interactive=False)


def test_resolve_kconfig_targets_local_target_rejected_when_non_interactive():
    with pytest.raises(ValueError, match="interactively"):
        resolve_kconfig_targets(
            kcfg({"kconfig_targets": ["localmodconfig"]}), interactive=False
        )


def test_resolve_kconfig_targets_silent_targets_pass_non_interactive():
    cfg = kcfg({"kconfig_targets": ["olddefconfig", "savedefconfig"]})
    assert resolve_kconfig_targets(cfg, interactive=False) == [
        "olddefconfig",
        "savedefconfig",
    ]


def test_resolve_kconfig_targets_localmodconfig_warns(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        sysforge_log, "warn", lambda tag, msg: warnings.append(msg)
    )
    cfg = kcfg({"kconfig_targets": ["localmodconfig"]})
    result = resolve_kconfig_targets(cfg, interactive=True)
    assert result == ["localmodconfig"]
    assert any("lsmod.snapshot" in w for w in warnings)


def test_resolve_kconfig_targets_localyesconfig_warns(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        sysforge_log, "warn", lambda tag, msg: warnings.append(msg)
    )
    cfg = kcfg({"kconfig_targets": ["localyesconfig"]})
    result = resolve_kconfig_targets(cfg, interactive=True)
    assert result == ["localyesconfig"]
    assert any("lsmod.snapshot" in w for w in warnings)


# ---------------------------------------------------------------------------
# load_hardware_kconfig
# ---------------------------------------------------------------------------

def test_load_hardware_kconfig_no_path_configured():
    result = load_hardware_kconfig({})
    assert result == ({}, {})

def test_load_hardware_kconfig_file_absent(tmp_path):
    config = {"hardware_profile": str(tmp_path / "nonexistent.toml")}
    result = load_hardware_kconfig(config)
    assert result == ({}, {})

def test_load_hardware_kconfig_no_kconfig_section(tmp_path):
    hw = tmp_path / "hardware_profile.toml"
    hw.write_text('nvidia_gpu = true\n')
    result = load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({}, {})

def test_load_hardware_kconfig_returns_kconfig_table(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True},
        kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"},
    )
    result = load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"}, {})

def test_load_hardware_kconfig_ignores_non_kconfig_keys(tmp_path):
    hw = make_hardware_profile(tmp_path,
        extra={"nvidia_gpu": True, "amd_cpu": True},
        kconfig={"CONFIG_MZEN3": "y"},
    )
    kconfig, device_kconfig = load_hardware_kconfig({"hardware_profile": str(hw)})
    assert "nvidia_gpu" not in kconfig
    assert kconfig == {"CONFIG_MZEN3": "y"}
    assert device_kconfig == {}

def test_load_hardware_kconfig_returns_device_table(tmp_path):
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    result = load_hardware_kconfig({"hardware_profile": str(hw)})
    assert result == ({"CONFIG_MZEN3": "y"}, {"CONFIG_IGC": "m"})

def test_load_hardware_kconfig_falls_back_to_state_dir(tmp_path):
    # Standalone `run kernel` after `run hardware`: config has no
    # hardware_profile key, but the hardware stage wrote the file under state_dir.
    make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    result = load_hardware_kconfig({}, state_dir=tmp_path)
    assert result == ({"CONFIG_MZEN3": "y"}, {"CONFIG_IGC": "m"})

def test_load_hardware_kconfig_state_dir_file_absent(tmp_path):
    # state_dir given but the hardware stage never ran — no file present.
    result = load_hardware_kconfig({}, state_dir=tmp_path)
    assert result == ({}, {})

def test_load_hardware_kconfig_config_key_wins_over_state_dir(tmp_path):
    # An explicit config path takes precedence over the state_dir fallback.
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    hw = make_hardware_profile(cfg_dir, kconfig={"CONFIG_FROM_CONFIG": "y"})
    make_hardware_profile(tmp_path, kconfig={"CONFIG_FROM_STATE": "y"})
    result = load_hardware_kconfig(
        {"hardware_profile": str(hw)}, state_dir=tmp_path,
    )
    assert result == ({"CONFIG_FROM_CONFIG": "y"}, {})


# ---------------------------------------------------------------------------
# load_kernel_config
# ---------------------------------------------------------------------------

def test_load_kernel_config_missing_returns_none(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    with patch.object(_km.config, "KERNEL_PATH", tmp_path / "nonexistent.toml"):
        result = load_kernel_config()
    assert result is None

def test_load_kernel_config_returns_dict(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    with patch.object(_km.config, "KERNEL_PATH", p):
        result = load_kernel_config()
    assert result["pkgname"] == "linux-git"
    assert result["bootloader"] == "systemd-boot"


# ---------------------------------------------------------------------------
# pkgbuild_path
# ---------------------------------------------------------------------------

def test_pkgbuild_path_missing_pkgbuild_src_dir():
    # KernelStage.run() stamps the effective src dir in; an empty value here
    # means neither kernel.toml nor the global [paths] had one.
    with pytest.raises(RuntimeError, match="no pkgbuild_src_dir configured"):
        pkgbuild_path(kcfg({"pkgname": "linux-git"}))

def test_pkgbuild_path_missing_pkgname(tmp_path):
    with pytest.raises(RuntimeError, match="missing pkgname"):
        pkgbuild_path(kcfg({"pkgbuild_src_dir": str(tmp_path)}))

def test_pkgbuild_path_pkgbuild_not_found(tmp_path):
    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        pkgbuild_path(kcfg({"pkgbuild_src_dir": str(tmp_path), "pkgname": "linux-git"}))

def test_pkgbuild_path_returns_path(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    result = pkgbuild_path(kcfg({"pkgbuild_src_dir": str(builds), "pkgname": "linux-git"}))
    assert result.name == "PKGBUILD"
    assert result.exists()

def test_pkgbuild_path_srcdir_override(tmp_path):
    """srcdir allows pkgname != source directory name (e.g. linux-custom in dir 'linux')."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux")   # directory is 'linux', not 'linux-custom'
    result = pkgbuild_path(kcfg({
        "pkgbuild_src_dir": str(builds),
        "pkgname": "linux-custom",
        "srcdir": "linux",
    }))
    assert result.name == "PKGBUILD"
    assert result.exists()

def test_pkgbuild_path_srcdir_not_found(tmp_path):
    """Error message when srcdir directory doesn't exist."""
    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        pkgbuild_path(kcfg({
            "pkgbuild_src_dir": str(tmp_path),
            "pkgname": "linux-custom",
            "srcdir": "linux",
        }))


def test_pkgbuild_path_upstream_pkgname_names_the_dir(tmp_path):
    """Track-upstream mode: the clone dir defaults to upstream_pkgname."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-zen")
    result = pkgbuild_path(kcfg({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
        "pkgname": "linux-mine",
    }))
    assert result.parent.name == "linux-zen"

def test_pkgbuild_path_srcdir_wins_over_upstream(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "zen-tree")
    result = pkgbuild_path(kcfg({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
        "pkgname": "linux-mine",
        "srcdir": "zen-tree",
    }))
    assert result.parent.name == "zen-tree"

def test_pkgbuild_path_pkgname_defaults_from_upstream(tmp_path):
    """pkgname omitted → upstream_pkgname satisfies the name requirement."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-zen")
    result = pkgbuild_path(kcfg({
        "pkgbuild_src_dir": str(builds),
        "upstream_pkgname": "linux-zen",
    }))
    assert result.parent.name == "linux-zen"


# ---------------------------------------------------------------------------
# resolve_names / resolve_source (F40)
# ---------------------------------------------------------------------------

def test_resolve_names_pure_local():
    up, pkg = _km.config.resolve_names(kcfg({"pkgname": "linux-sysforge"}))
    assert up is None
    assert pkg == "linux-sysforge"

def test_resolve_names_pkgname_defaults_to_upstream():
    up, pkg = _km.config.resolve_names(kcfg({"upstream_pkgname": "linux-zen"}))
    assert up == "linux-zen"
    assert pkg == "linux-zen"

def test_resolve_names_distinct():
    up, pkg = _km.config.resolve_names(
        kcfg({"upstream_pkgname": "linux-zen", "pkgname": "linux-mine"}))
    assert (up, pkg) == ("linux-zen", "linux-mine")

def test_resolve_names_neither_raises():
    with pytest.raises(RuntimeError, match="pkgname"):
        _km.config.resolve_names(kcfg({}))

def test_resolve_source_explicit_honored(tmp_path):
    for src in ("local", "repo", "aur"):
        assert _km.config.resolve_source(kcfg({"source": src}), tmp_path) == src

def test_resolve_source_git_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        _km.config.resolve_source(kcfg({"source": "git"}), tmp_path)

def test_resolve_source_auto_existing_plain_dir_is_local(tmp_path):
    d = tmp_path / "linux-sysforge"
    d.mkdir()
    assert _km.config.resolve_source(kcfg({"pkgname": "linux-sysforge"}), d) == "local"

def test_resolve_source_auto_existing_git_clone_fetches(tmp_path):
    d = tmp_path / "linux-zen"
    (d / ".git").mkdir(parents=True)
    assert _km.config.resolve_source(
        kcfg({"upstream_pkgname": "linux-zen"}), d) == "repo"

def test_resolve_source_auto_missing_dir_repo_package(tmp_path, monkeypatch):
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package",
                        lambda name: True)
    assert _km.config.resolve_source(
        kcfg({"upstream_pkgname": "linux-zen"}), tmp_path / "linux-zen") == "repo"

def test_resolve_source_auto_missing_dir_aur_package(tmp_path, monkeypatch):
    monkeypatch.setattr("sysforge.primitives.aur.is_repo_package",
                        lambda name: False)
    assert _km.config.resolve_source(
        kcfg({"upstream_pkgname": "linux-tkg"}), tmp_path / "linux-tkg") == "aur"


# ---------------------------------------------------------------------------
# write_kconfig_fragment
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_no_entries_is_noop(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, {}, dry_run=False)
    assert result is None
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_hardware_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y", "CONFIG_NOUVEAU": "n"})
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=False)

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
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})

    result, hw_c, _, _, _ = write_kconfig_fragment(
        kernel_cfg, {}, dry_run=False, state_dir=state_dir,
    )

    assert result is not None
    assert hw_c == 1
    assert "CONFIG_MZEN3=y" in result.read_text()

def test_write_kconfig_fragment_manual_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_HZ_1000", "value": "y"}],
    })

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

    assert result is not None
    content = result.read_text()
    assert "CONFIG_HZ_1000=y" in content
    assert "# source: manual" in content

def test_write_kconfig_fragment_merge_hw_and_manual(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_HZ_1000", "value": "y"}],
    })
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_MZEN3=y" in content
    assert "CONFIG_HZ_1000=y" in content

def test_write_kconfig_fragment_manual_wins_conflict(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_MZEN3", "value": "n"}],  # override hw
    })
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    # manual value wins — n, not y
    assert "# CONFIG_MZEN3 is not set" in content
    assert "CONFIG_MZEN3=y" not in content

def test_write_kconfig_fragment_conflict_emits_warn(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_MZEN3", "value": "n"}],
    })
    config = {"hardware_profile": str(hw)}

    with _capture_logs() as logs:
        write_kconfig_fragment(kernel_cfg, config, dry_run=False)
    assert any("CONFIG_MZEN3" in m and "manual override wins" in m
               for m in _warn_messages(logs))

# ---------------------------------------------------------------------------
# kconfig_merge master toggle
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_merge_disabled_is_noop(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig_merge": False,
    })
    config = {"hardware_profile": str(hw)}

    result, hw_c, man_c, dev_c, fdo_c = write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    assert result is None
    assert (hw_c, man_c, dev_c, fdo_c) == (0, 0, 0, 0)
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_merge_disabled_removes_stale(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    stale = builds / "linux-git" / "sysforge.config"
    stale.write_text("CONFIG_OLD=y\n")
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig_merge": False,
    })

    result, *_ = write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

    assert result is None
    assert not stale.exists()

# ---------------------------------------------------------------------------
# gate2_kconfig_drift — advisory post-build drift check
# ---------------------------------------------------------------------------

def test_gate2_kconfig_drift_warns_on_disabled_option(tmp_path, monkeypatch):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("# source: hardware\nCONFIG_MZEN3=y\nCONFIG_HZ_1000=y\n")
    resolved = tmp_path / ".config"
    resolved.write_text("# CONFIG_MZEN3 is not set\nCONFIG_HZ_1000=y\n")
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: resolved)

    with _capture_logs() as logs:
        _km.gates.gate2_kconfig_drift(tmp_path, fragment)

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
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: resolved)

    with _capture_logs() as logs:
        _km.gates.gate2_kconfig_drift(tmp_path, fragment)

    assert not _warn_messages(logs)
    assert any("survived" in m for m in _info_messages(logs))

def test_gate2_kconfig_drift_no_fragment_is_noop(tmp_path, monkeypatch):
    # fragment_path is None (merge disabled / no entries) → check must not run,
    # not even locate the resolved config.
    called = []
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: called.append(d))

    with _capture_logs() as logs:
        _km.gates.gate2_kconfig_drift(tmp_path, None)

    assert called == []
    assert not _warn_messages(logs)

def test_gate2_kconfig_drift_no_resolved_config_skips(tmp_path, monkeypatch):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("CONFIG_HZ_1000=y\n")
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: None)

    with _capture_logs() as logs:
        _km.gates.gate2_kconfig_drift(tmp_path, fragment)

    # B6: the skip is a WARN, not an INFO — on the AlreadyBuilt path there is
    # no build tree, so the advisory audit silently never ran. The message
    # names the why (no build tree) so the operator knows the audit is dead.
    warned = _warn_messages(logs)
    assert any("resolved .config not found" in m for m in warned)
    assert any("did not run" in m for m in warned)

def test_write_kconfig_fragment_dry_run_no_file(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=True)

    assert result is None
    assert not (builds / "linux-git" / "sysforge.config").exists()

def test_write_kconfig_fragment_invalid_manual_raises(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "bad-name", "value": "y"}],
    })
    with pytest.raises(RuntimeError, match="invalid option"):
        write_kconfig_fragment(kernel_cfg, {}, dry_run=False)

def test_write_kconfig_fragment_file_has_header(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_KVM": "y"})
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "Generated by SysForge" in content
    assert "merge_config.sh" in content


# ---------------------------------------------------------------------------
# write_kconfig_fragment — device-driven [kconfig_devices]
# ---------------------------------------------------------------------------

def test_write_kconfig_fragment_device_entries_merged(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path,
        kconfig={"CONFIG_MZEN3": "y"},
        kconfig_devices={"CONFIG_IGC": "m"},
    )
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, hw_count, manual_count, device_count, _ = write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_IGC=m" in content
    assert "# source: device" in content
    assert (hw_count, manual_count, device_count) == (1, 0, 1)

def test_write_kconfig_fragment_device_only(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig_devices={"CONFIG_IGC": "m"})
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, _, _, device_count, _ = write_kconfig_fragment(
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
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    result, hw_count, _, device_count, _ = write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "# CONFIG_DRM_NOUVEAU is not set" in content
    assert "CONFIG_DRM_NOUVEAU=m" not in content
    assert (hw_count, device_count) == (1, 0)

def test_write_kconfig_fragment_manual_wins_over_device(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig_devices={"CONFIG_IGC": "m"})
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_IGC", "value": "n"}],
    })
    config = {"hardware_profile": str(hw)}

    result, _, manual_count, device_count, _ = write_kconfig_fragment(
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
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "device_kconfig": False,
    })
    config = {"hardware_profile": str(hw)}

    result, _, _, device_count, _ = write_kconfig_fragment(
        kernel_cfg, config, dry_run=False)

    content = result.read_text()
    assert "CONFIG_IGC" not in content
    assert device_count == 0


# ---------------------------------------------------------------------------
# Sample-based FDO (AutoFDO / Propeller) — resolve_fdo / LLVM gate / fragment
# ---------------------------------------------------------------------------

def _fdo_opts(**kw):
    base = dict(kernel_fdo=None, kernel_propeller=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_fdo_none_when_unset():
    assert resolve_fdo(_fdo_opts()) == (None, False, False)


def test_resolve_fdo_valid_modes():
    assert resolve_fdo(_fdo_opts(kernel_fdo="record")) == ("record", False, True)
    assert resolve_fdo(_fdo_opts(kernel_fdo="use", kernel_propeller=True)) == (
        "use", True, True)


@pytest.mark.parametrize("cli,cfg,expect", [
    ((None, False), "autofdo", ("use", False, False)),
    ((None, False), "propeller", ("use", True, False)),
    ((None, False), "off", (None, False, False)),
    (("record", False), "propeller", ("record", False, True)),
    (("use", False), "propeller", ("use", False, True)),
    (("capture", True), "autofdo", ("capture", True, True)),
])
def test_resolve_fdo_precedence(cli, cfg, expect):
    """Explicit --autofdo wins over kernel.toml [fdo] mode; config only ever
    implies `use` (record/capture need a reboot and a human workload)."""
    opts = _fdo_opts(kernel_fdo=cli[0], kernel_propeller=cli[1])
    assert resolve_fdo(opts, KernelConfig(fdo_mode=cfg)) == expect


@pytest.mark.parametrize("table,mode", [(None, "off"), ({}, "off"),
                                        ({"mode": "off"}, "off"),
                                        ({"mode": "autofdo"}, "autofdo"),
                                        ({"mode": "propeller"}, "propeller")])
def test_fdo_mode_parse(table, mode):
    data = {} if table is None else {"fdo": table}
    assert KernelConfig.from_toml(data).fdo_mode == mode


@pytest.mark.parametrize("table", [{"mode": "pgo"}, {"mode": 1}])
def test_fdo_mode_invalid_refuses(table):
    with pytest.raises(RuntimeError, match=r"\[fdo\] mode"):
        KernelConfig.from_toml({"fdo": table})


def test_fdo_not_a_table_refuses():
    with pytest.raises(RuntimeError, match=r"\[fdo\] must be a table \(got 'autofdo'\)"):
        KernelConfig.from_toml({"fdo": "autofdo"})


def test_resolve_fdo_propeller_requires_mode_even_with_config():
    """--propeller alone stays an error even when [fdo] mode would imply use."""
    with pytest.raises(RuntimeError, match="requires --autofdo"):
        resolve_fdo(_fdo_opts(kernel_propeller=True), KernelConfig(fdo_mode="propeller"))


def test_resolve_fdo_invalid_mode_raises():
    with pytest.raises(RuntimeError, match="invalid --autofdo"):
        resolve_fdo(_fdo_opts(kernel_fdo="bogus"))


def test_resolve_fdo_propeller_requires_mode():
    with pytest.raises(RuntimeError, match="requires --autofdo"):
        resolve_fdo(_fdo_opts(kernel_propeller=True))


def test_run_fdo_capture_refuses_when_tool_missing(monkeypatch, tmp_path):
    from sysforge.primitives import kernel_fdo as kf
    monkeypatch.setattr(kf, "resolve_store", lambda *a, **k: tmp_path)
    monkeypatch.setattr(
        kf, "missing_tools", lambda **k: ["perf: install it with `pacman -S perf`"])
    fake_log = MagicMock()
    monkeypatch.setattr(_km.fdo, "_log", fake_log)
    with pytest.raises(RuntimeError, match="perf"):
        _km.fdo.run_fdo_capture("linux", False, False)
    fake_log.ui.assert_not_called()


def test_run_fdo_capture_happy_path_passes_recorded_tree(monkeypatch, tmp_path):
    from pathlib import Path
    from sysforge.primitives import fs_provision
    from sysforge.primitives import kernel_fdo as kf
    monkeypatch.setattr(kf, "resolve_store", lambda *a, **k: tmp_path)
    monkeypatch.setattr(kf, "missing_tools", lambda **k: [])
    monkeypatch.setattr(kf, "read_round", lambda store: kf.RoundInfo("1-1", Path("/rec"), None))
    seen = {}

    def fake_resolve(name, **kw):
        seen.update(kw)
        return Path("/v")
    monkeypatch.setattr(kf, "resolve_vmlinux", fake_resolve)
    monkeypatch.setattr(
        kf, "detect_branch_sampling",
        lambda *a, **k: kf.BranchSampling("amd", True, "-e ev", "n"))
    monkeypatch.setattr(fs_provision, "ensure_writable_dir", lambda p: None)
    fake_log = MagicMock()
    monkeypatch.setattr(_km.fdo, "_log", fake_log)
    _km.fdo.run_fdo_capture("linux", False, False)
    assert seen["recorded_build_dir"] == Path("/rec")
    assert seen["propeller"] is False  # 3.3.0-B17: refusals name the round's flags
    assert any("sudo perf record" in str(c) for c in fake_log.ui.call_args_list)


def test_run_fdo_capture_refuses_when_not_booted_prints_nothing_runnable(monkeypatch, tmp_path):
    from sysforge.primitives import kernel_fdo as kf
    monkeypatch.setattr(kf, "resolve_store", lambda *a, **k: tmp_path)
    monkeypatch.setattr(kf, "missing_tools", lambda **k: [])

    def boom(*a, **k):
        raise kf.KernelFdoError("not booted into X")
    monkeypatch.setattr(kf, "resolve_vmlinux", boom)
    fake_log = MagicMock()
    monkeypatch.setattr(_km.fdo, "_log", fake_log)
    with pytest.raises(RuntimeError, match="not booted"):
        _km.fdo.run_fdo_capture("linux", False, False)
    fake_log.ui.assert_not_called()


# Dual-toolchain parity: the LLVM gate passes under clang and refuses gcc.

def test_gate_fdo_llvm_explicit_llvm_passes():
    gate_fdo_llvm("use", False, "llvm", None)  # no raise


def test_gate_fdo_llvm_explicit_gcc_refuses():
    with pytest.raises(RuntimeError, match="requires the LLVM toolchain") as ei:
        gate_fdo_llvm("record", False, "gcc", "/usr/bin/gcc")
    assert "--autofdo=record" in str(ei.value)


@pytest.mark.parametrize("propeller,mode", [(False, "autofdo"), (True, "propeller")])
def test_gate_fdo_llvm_config_driven_gcc_names_fdo_mode(propeller, mode):
    """A config-driven request names the kernel.toml setting and its way out,
    never a flag the user did not pass."""
    with pytest.raises(RuntimeError, match=r"\[fdo\] mode") as ei:
        gate_fdo_llvm("use", propeller, "gcc", "/usr/bin/gcc", explicit=False)
    msg = str(ei.value)
    assert f'mode = "{mode}"' in msg and 'mode = "off"' in msg
    assert "--autofdo" not in msg


def test_gate_fdo_llvm_inherited_clang_cc_passes():
    # compiler None but the resolved cc is clang → allowed.
    gate_fdo_llvm("use", True, None, "/usr/bin/clang")


def test_fdo_is_llvm_env_cc_fallback(monkeypatch):
    monkeypatch.setenv("CC", "/usr/lib/ccache/bin/clang")
    assert fdo_is_llvm(None, None) is True
    monkeypatch.setenv("CC", "gcc")
    assert fdo_is_llvm(None, None) is False


def test_fragment_includes_fdo_entries_labeled(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    result, hw, man, dev, fdo = write_kconfig_fragment(
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
    kernel_cfg = kcfg({
        "pkgname": "linux-git",
        "pkgbuild_src_dir": str(builds),
        "kconfig": [{"option": "CONFIG_AUTOFDO_CLANG", "value": "n"}],
    })
    with _capture_logs() as logs:
        result, *_ = write_kconfig_fragment(
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

    with patch.object(_km.config, "KERNEL_PATH", tmp_path / "nonexistent.toml"), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
        mock_build.assert_not_called()
        mock_sub.assert_not_called()

def test_kernel_stage_skips_snapshot_when_disabled(tmp_path):
    """F21 regression lock: a disabled/absent kernel.toml must take the
    early-return path before the pre-build snapshot seam is reached."""
    import sysforge.pipeline.stages.kernel as _km
    state = PipelineState(tmp_path / "state")

    with patch.object(_km.config, "KERNEL_PATH", tmp_path / "nonexistent.toml"), \
         patch("sysforge.primitives.snapshot.ensure_pre_build_snapshot") as mock_snap:
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
        mock_snap.assert_not_called()

def test_kernel_stage_dry_run_calls_nothing(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        KernelStage().run({}, state, make_options(dry_run=True, state_dir=tmp_path / "state"))
        mock_build.assert_not_called()
        mock_sub.assert_not_called()

def test_kernel_stage_calls_makepkg(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run",
               side_effect=fail_mkinitcpio), \
         pytest.raises(RuntimeError, match="mkinitcpio"):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

def test_kernel_stage_writes_kconfig_fragment_when_hw_profile_present(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_MZEN3": "y"})
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    config = {"hardware_profile": str(hw)}

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run(config, state, make_options(state_dir=tmp_path / "state"))

    fragment = builds / "linux-git" / "sysforge.config"
    assert fragment.exists()
    assert "CONFIG_MZEN3=y" in fragment.read_text()


# ---------------------------------------------------------------------------
# resolve_compiler / resolve_bootloader
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
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = _state_with_toolchain(tmp_path)
    options = make_options()
    options.compiler = "llvm"
    compiler, cc, cxx = resolve_compiler(kcfg({"compiler": "gcc"}), options, state)
    assert compiler == "llvm"
    assert cc == "/usr/bin/clang"
    assert cxx == "/usr/bin/clang++"


def test_resolve_compiler_kernel_toml_wins_over_pipeline_state(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = _state_with_toolchain(tmp_path, cc="/some/pipeline/clang")
    compiler, cc, cxx = resolve_compiler(kcfg({"compiler": "gcc"}), make_options(), state)
    assert compiler == "gcc"
    assert cc == "/usr/bin/gcc"
    assert cxx == "/usr/bin/g++"


def test_resolve_compiler_falls_back_to_pipeline_state(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = _state_with_toolchain(tmp_path, cc="/usr/bin/clang", cxx="/usr/bin/clang++")
    compiler, cc, cxx = resolve_compiler(kcfg({}), make_options(), state)
    assert compiler is None
    assert cc == "/usr/bin/clang"
    assert cxx == "/usr/bin/clang++"


def test_resolve_compiler_no_override_returns_none(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = PipelineState(tmp_path / "state")
    compiler, cc, cxx = resolve_compiler(kcfg({}), make_options(), state)
    assert compiler is None and cc is None and cxx is None


def test_resolve_compiler_rejects_invalid_cli(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = PipelineState(tmp_path / "state")
    options = make_options()
    options.compiler = "icc"
    with pytest.raises(RuntimeError, match="invalid --compiler"):
        resolve_compiler(kcfg({}), options, state)


def test_resolve_compiler_rejects_invalid_kernel_toml(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_compiler
    state = PipelineState(tmp_path / "state")
    with pytest.raises(RuntimeError, match="invalid kernel.toml"):
        resolve_compiler(kcfg({"compiler": "icc"}), make_options(), state)


def test_resolve_bootloader_cli_wins():
    from sysforge.pipeline.stages.kernel import resolve_bootloader
    options = make_options()
    options.bootloader = "grub"
    assert resolve_bootloader(kcfg({"bootloader": "systemd-boot"}), options) == "grub"


def test_resolve_bootloader_uses_kernel_toml_when_no_cli():
    from sysforge.pipeline.stages.kernel import resolve_bootloader
    assert resolve_bootloader(kcfg({"bootloader": "grub"}), make_options()) == "grub"


def test_resolve_bootloader_default_is_systemd_boot():
    from sysforge.pipeline.stages.kernel import resolve_bootloader
    assert resolve_bootloader(kcfg({}), make_options()) == "systemd-boot"


def test_resolve_bootloader_rejects_invalid_cli():
    from sysforge.pipeline.stages.kernel import resolve_bootloader
    options = make_options()
    options.bootloader = "lilo"
    with pytest.raises(RuntimeError, match="invalid --bootloader"):
        resolve_bootloader(kcfg({}), options)


# ---------------------------------------------------------------------------
# resolve_boot_entries
# ---------------------------------------------------------------------------


def test_boot_entries_defaults():
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    c = kcfg()
    assert c.boot_entries == "manage" and c.boot_entries_prefix is None
    assert resolve_boot_entries(c, "systemd-boot") == (True, None)


@pytest.mark.parametrize("bl", ["grub", "none"])
def test_boot_entries_inactive_off_systemd_boot(bl):
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    assert resolve_boot_entries(kcfg(), bl) == (False, None)


def test_boot_entries_off_and_prefix():
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    assert resolve_boot_entries(kcfg(boot_entries="off"), "systemd-boot") == (False, None)
    assert resolve_boot_entries(kcfg(boot_entries_prefix="97"), "systemd-boot") == (True, "97")


def test_boot_entries_invalid_values():
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    with pytest.raises(RuntimeError, match="boot_entries"):
        resolve_boot_entries(kcfg(boot_entries="auto"), "systemd-boot")
    with pytest.raises(RuntimeError, match="boot_entries_prefix"):
        resolve_boot_entries(kcfg(boot_entries_prefix="9-7"), "systemd-boot")


def test_boot_entries_prefix_must_be_quoted_string():
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    with pytest.raises(RuntimeError, match="must be a quoted string"):
        resolve_boot_entries(kcfg(boot_entries_prefix=97), "systemd-boot")
    with pytest.raises(RuntimeError, match="must be a quoted string"):
        resolve_boot_entries(kcfg(boot_entries_prefix=False), "systemd-boot")


def test_boot_entries_prefix_empty_string_invalid():
    from sysforge.pipeline.stages.kernel import resolve_boot_entries
    with pytest.raises(RuntimeError, match="boot_entries_prefix"):
        resolve_boot_entries(kcfg(boot_entries_prefix=""), "systemd-boot")


# ---------------------------------------------------------------------------
# resolve_subpackages (headers/docs toggles)
# ---------------------------------------------------------------------------

def test_resolve_subpackages_defaults_headers_on_docs_off():
    from sysforge.pipeline.stages.kernel import resolve_subpackages
    assert resolve_subpackages(kcfg({}), make_options()) == (True, False)


def test_resolve_subpackages_kernel_toml_wins_over_default():
    from sysforge.pipeline.stages.kernel import resolve_subpackages
    cfg = kcfg({"build_headers": False, "build_docs": True})
    assert resolve_subpackages(cfg, make_options()) == (False, True)


def test_resolve_subpackages_cli_headers_off_beats_toml_on():
    from sysforge.pipeline.stages.kernel import resolve_subpackages
    options = make_options(build_headers=False)
    assert resolve_subpackages(kcfg({"build_headers": True}), options) == (False, False)


def test_resolve_subpackages_cli_docs_on_beats_toml_off():
    from sysforge.pipeline.stages.kernel import resolve_subpackages
    options = make_options(build_docs=True)
    assert resolve_subpackages(kcfg({"build_docs": False}), options) == (True, True)


def test_resolve_subpackages_cli_none_falls_through_to_toml():
    from sysforge.pipeline.stages.kernel import resolve_subpackages
    # RunOptions defaults build_headers/build_docs to None (flag unset).
    options = make_options()
    cfg = kcfg({"build_headers": False, "build_docs": True})
    assert resolve_subpackages(cfg, options) == (False, True)


def test_kernel_stage_threads_subpackages_into_build_options(tmp_path):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    opts = make_options(state_dir=tmp_path / "state", build_headers=False, build_docs=True)
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    build_opts = mock_build.call_args.kwargs["options"]
    assert build_opts.kernel_build_headers is True
    assert build_opts.kernel_build_docs is False


def test_gate1_warns_when_headers_disabled(monkeypatch):
    """Disabling headers must warn about the DKMS / out-of-tree module risk,
    naming any present DKMS modules."""
    from sysforge.pipeline.stages.kernel import gate1_preflight
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels", lambda *a, **k: ["linux"])
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space", lambda *a, **k: None)
    monkeypatch.setattr(kernel_safety, "detect_root_topology",
                        lambda *a, **k: kernel_safety.RootTopology())
    monkeypatch.setattr(kernel_safety, "check_mkinitcpio_hooks", lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "list_dkms_modules", lambda: ["nvidia"])

    cfg = kcfg({"build_headers": False, "capture_lsmod_snapshot": False})
    with _capture_logs() as logs:
        gate1_preflight(cfg, make_options(), "linux-custom", dry_run=False)

    joined = "\n".join(_warn_messages(logs))
    assert "-headers subpackage disabled" in joined
    assert "nvidia" in joined


def _gate1_quiet(monkeypatch):
    monkeypatch.setattr(kernel_safety, "find_fallback_kernels", lambda *a, **k: ["linux"])
    monkeypatch.setattr(kernel_safety, "check_boot_mount_space", lambda *a, **k: None)
    monkeypatch.setattr(kernel_safety, "detect_root_topology",
                        lambda *a, **k: kernel_safety.RootTopology())
    monkeypatch.setattr(kernel_safety, "check_mkinitcpio_hooks", lambda *a, **k: [])
    monkeypatch.setattr(kernel_safety, "list_dkms_modules", lambda: [])


@pytest.mark.parametrize("targets", [["olddefconfig", "localmodconfig"],
                                     ["localyesconfig", "nconfig"]])
def test_gate1_warns_minimizer_strips_when_configured(monkeypatch, tmp_path, targets):
    from sysforge.pipeline.stages.kernel import gate1_preflight
    _gate1_quiet(monkeypatch)
    cfg = kcfg({"kconfig_targets": targets})
    with _capture_logs() as logs:
        gate1_preflight(cfg, make_options(state_dir=tmp_path), "linux-custom", dry_run=False)
    assert any("drivers for hardware never active" in m for m in _warn_messages(logs))


@pytest.mark.parametrize("targets", [None, ["olddefconfig", "nconfig"]])
def test_gate1_no_strip_warning_without_configured_minimizer(monkeypatch, tmp_path, targets):
    # 3.2.0-B35: the warning fired on every run with capture on, implying a
    # minimization that never happened — the snapshot only reaches a
    # configured localmodconfig/localyesconfig.
    from sysforge.pipeline.stages.kernel import gate1_preflight
    _gate1_quiet(monkeypatch)
    cfg = kcfg({"kconfig_targets": targets} if targets else {})
    with _capture_logs() as logs:
        gate1_preflight(cfg, make_options(state_dir=tmp_path), "linux-custom", dry_run=False)
    assert not any("lsmod" in m for m in _warn_messages(logs))
    assert any("not minimized" in m for m in _info_messages(logs))


# ---------------------------------------------------------------------------
# Interactive default, --non-interactive, BuildOptions plumbing
# ---------------------------------------------------------------------------


def test_kernel_stage_passes_interactive_true_by_default(tmp_path):
    # B8: "by default" now means config-default AND a TTY — pin the TTY.
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler") as mock_sched, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock) as mock_sched, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run"), \
         pytest.raises(RuntimeError, match="source sync failed"):
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.primitives.aur.classify_head_vs_upstream",
               return_value=("diverged_upstream", 1, 2)), \
         patch("sysforge.primitives.prompt.is_interactive",
               return_value=interactive), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value=answer), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock), \
         patch("sysforge.primitives.aur.classify_head_vs_upstream",
               return_value=("diverged_upstream", 1, 2)), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=False), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run"), \
         pytest.raises(RuntimeError, match="diverged source unattended"):
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
    from sysforge.pipeline.stages.kernel import kernel_recovery_command
    cmd = kernel_recovery_command()
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages",
                      side_effect=check_sentinel_during_install), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    assert seen_during_install["present"] is True
    assert seen_during_install["stage"] == "kernel"
    # Cleared on clean exit
    assert StageSentinel(state_dir).get_active() is None


# ---------------------------------------------------------------------------
# 3.1.0-B4 / 3.1.0-B5 — sudo credential lifetime around the install window
# ---------------------------------------------------------------------------

def test_kernel_stage_auth_failure_aborts_without_writing_a_sentinel(tmp_path):
    """B4: a sudo prompt that times out means pacman never ran and nothing was
    installed. That must abort cleanly *outside* the sentinel scope — otherwise
    the operator has to run a pointless mkinitcpio to clear a sentinel guarding
    a mutation that never began."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    install_mock = MagicMock(return_value=[])
    # First call is the B5 entry warm-up (succeeds, so the build proceeds);
    # the second is the B4 pre-install probe, which times out.
    auth = MagicMock(side_effect=[True, False])

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage.sudo_session, "authenticate", auth), \
         patch.object(_km.stage, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         pytest.raises(RuntimeError, match="sudo authentication failed or timed out"):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    assert StageSentinel(state_dir).get_active() is None, \
        "a pre-install auth failure must leave no sentinel to recover from"
    install_mock.assert_not_called()


def test_kernel_stage_auth_failure_message_names_the_plain_rerun(tmp_path):
    """The abort has to tell the operator the system is unchanged and how to
    get back in — the whole point is that no recovery ritual is needed."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage.sudo_session, "authenticate",
                      MagicMock(side_effect=[True, False])), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         pytest.raises(RuntimeError) as exc:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    msg = str(exc.value)
    assert "nothing was installed" in msg
    assert "sysforge run kernel" in msg


def test_kernel_stage_install_failure_still_retains_the_sentinel(tmp_path):
    """B4 must stay narrow: a pacman -U that actually ran and failed is the
    case the sentinel exists for, and keeps leaving one behind."""
    from sysforge.primitives.stage_sentinel import StageSentinel

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage.sudo_session, "authenticate", lambda: True), \
         patch.object(_km.stage, "install_built_packages",
                      side_effect=RuntimeError("pacman -U failed (exit 1)")), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         pytest.raises(RuntimeError, match="pacman -U failed"):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    record = StageSentinel(state_dir).get_active()
    assert record is not None
    assert record["stage"] == "kernel"


def test_kernel_stage_runs_a_sudo_keepalive_over_the_build(tmp_path):
    """B5: the build → audit → install window is covered by the shared
    keepalive, so the final install prompt cannot go stale."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    seen = {}

    @contextlib.contextmanager
    def fake_keepalive(*, tag, enabled=True):
        seen["tag"] = tag
        seen["enabled"] = enabled
        seen["installed_inside"] = False
        yield
        seen["exited"] = True

    def note_install(*_a, **_kw):
        seen["installed_inside"] = True
        return []

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage.sudo_session, "keepalive", fake_keepalive), \
         patch.object(_km.stage, "install_built_packages", side_effect=note_install), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=state_dir))

    assert seen["tag"] == "KERNEL"
    assert seen["enabled"] is True
    assert seen["installed_inside"] is True, "install must run inside the keepalive"
    assert seen.get("exited") is True


def test_kernel_stage_dry_run_skips_sudo_entirely(tmp_path):
    """A dry run mutates nothing, so it neither authenticates nor keeps alive."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state = PipelineState(state_dir)

    auth = MagicMock(return_value=True)
    enabled_seen = {}

    @contextlib.contextmanager
    def fake_keepalive(*, tag, enabled=True):
        enabled_seen["enabled"] = enabled
        yield

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage.sudo_session, "authenticate", auth), \
         patch.object(_km.stage.sudo_session, "keepalive", fake_keepalive), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state,
                          make_options(state_dir=state_dir, dry_run=True))

    auth.assert_not_called()
    assert enabled_seen["enabled"] is False


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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch.object(_km.stage, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: tmp_path / ".config")
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: [brick])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: tmp_path / ".config")
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda *a, **k: [brick])
    install_mock = MagicMock(return_value=[])

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: tree / ".config")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages", install_mock), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run",
               side_effect=fail_mkinitcpio), pytest.raises(RuntimeError, match="mkinitcpio"):
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run",
               side_effect=fail_mkinitcpio), pytest.raises(RuntimeError):
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run",
               side_effect=fail_mkinitcpio), pytest.raises(RuntimeError):
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_run, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    mock_run.assert_called_once()
    opts = mock_run.call_args.kwargs["options"]
    assert opts.source == "local"
    assert opts.owner_stage == "kernel"


def test_kernel_stage_hands_the_reported_build_dir_to_every_gate(tmp_path):
    """3.2.0-B37: the tree makepkg_run reports (profile BUILDDIR + post-rename
    pkgbase) is where Gate 2, the drift check, the kconfig history and Gate 3
    look — never the system conf keyed on the checkout name."""
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
    reported = tmp_path / "profile-builds" / "linux-custom-sysforge"
    seen = []

    def spy(d, **kw):
        seen.append(kw.get("build_dir"))
        return None

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch.object(_km.gates, "resolve_built_config", spy), \
         patch(_MAKEPKG_RUN, return_value=reported), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="Gate 2"):
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    # The fresh build reported a tree with no .config in it → Gate 2 refused.
    assert seen and all(b == reported for b in seen)


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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler") as mock_sched, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         pytest.raises(RuntimeError, match="invalid kernel.toml source"):
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         _capture_logs() as logs, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.pipeline.stages.kernel.source.probe_installed_bootloader",
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
    from sysforge.pipeline.stages.kernel import probe_installed_bootloader

    def fake_exists(self):
        return str(self) == "/boot/loader/loader.conf"

    with patch("pathlib.Path.exists", fake_exists):
        assert probe_installed_bootloader() == {"systemd-boot"}


def test_probe_bootloader_grub_via_grub_cfg(tmp_path):
    from sysforge.pipeline.stages.kernel import probe_installed_bootloader

    def fake_exists(self):
        return str(self) == "/boot/grub/grub.cfg"

    with patch("pathlib.Path.exists", fake_exists):
        assert probe_installed_bootloader() == {"grub"}


def test_probe_bootloader_dual_boot(tmp_path):
    from sysforge.pipeline.stages.kernel import probe_installed_bootloader

    def fake_exists(self):
        return str(self) in ("/boot/loader/loader.conf", "/boot/grub/grub.cfg")

    with patch("pathlib.Path.exists", fake_exists):
        assert probe_installed_bootloader() == {"systemd-boot", "grub"}


def test_run_warns_when_bootloader_mismatch(tmp_path):
    """kernel.toml bootloader = grub, only systemd-boot detected → WARN, build proceeds."""
    import sysforge.pipeline.stages.kernel as _km

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, bootloader="grub")
    state = PipelineState(tmp_path / "state")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         _capture_logs() as logs, \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.pipeline.stages.kernel.source.probe_installed_bootloader",
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
    from sysforge.pipeline.stages.kernel import validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgbase=linux-custom\n"
        "pkgname=(linux-custom linux-custom-headers)\n"
        "pkgver=6.10\npkgrel=1\n"
    )
    # No raise.
    validate_pkgname_matches_pkgbuild(pb, "linux-custom")


def test_validate_pkgname_match_simple(tmp_path):
    from sysforge.pipeline.stages.kernel import validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgname=linux-custom\npkgver=6.10\npkgrel=1\n"
    )
    validate_pkgname_matches_pkgbuild(pb, "linux-custom")


def test_validate_pkgname_typo_raises(tmp_path):
    from sysforge.pipeline.stages.kernel import validate_pkgname_matches_pkgbuild

    builds = tmp_path / "builds"
    pb = _write_pkgbuild_with(builds, "linux-custom",
        "pkgbase=linux-custom\n"
        "pkgname=(linux-custom linux-custom-headers)\n"
        "pkgver=6.10\npkgrel=1\n"
    )
    with pytest.raises(RuntimeError, match="does not match.*pkgbase"):
        validate_pkgname_matches_pkgbuild(pb, "linux-custm")


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
    with patch.object(_km.config, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_run, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel.source.probe_installed_bootloader",
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
    with patch.object(_km.config, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_run, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel.source.probe_installed_bootloader",
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
    with patch.object(_km.config, "KERNEL_PATH", p), \
         _capture_logs(), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_run, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.pipeline.stages.kernel.source.probe_installed_bootloader",
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
    from sysforge.pipeline.stages.kernel import pkgbuild_path

    builds = tmp_path / "builds"
    (builds / "linux-git").mkdir(parents=True)  # dir exists, no PKGBUILD inside
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})

    with pytest.raises(RuntimeError, match="interrupted --cleansrc"):
        pkgbuild_path(kernel_cfg)


def test_pkgbuild_path_dir_absent_keeps_clone_hint(tmp_path):
    from sysforge.pipeline.stages.kernel import pkgbuild_path

    builds = tmp_path / "builds"  # nothing created
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})

    with pytest.raises(RuntimeError, match="PKGBUILD not found"):
        pkgbuild_path(kernel_cfg)


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
    with build_lock(state_dir / "kernel-build.lock", label="kernel", noun="build"), \
         pytest.raises(RuntimeError, match="Another sysforge kernel build"):
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
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    path, _, _, _, _ = write_kconfig_fragment(
        kernel_cfg, config, dry_run=False,
        provenance="toolchain variant: pgo_llvm  cc: /usr/bin/clang",
    )
    content = path.read_text()
    assert "# toolchain variant: pgo_llvm  cc: /usr/bin/clang" in content


def test_kconfig_fragment_no_provenance_when_omitted(tmp_path):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    hw = make_hardware_profile(tmp_path, kconfig={"CONFIG_KVM": "y"})
    kernel_cfg = kcfg({"pkgname": "linux-git", "pkgbuild_src_dir": str(builds)})
    config = {"hardware_profile": str(hw)}

    path, _, _, _, _ = write_kconfig_fragment(kernel_cfg, config, dry_run=False)
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


def test_kernel_stage_invokes_pre_build_snapshot(tmp_path):
    """F21: KernelStage.run wires the pre-build snapshot seam after the
    enabled check, so a disabled/absent kernel.toml never triggers it."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    state = PipelineState(tmp_path / "state")
    p = make_kernel_toml(tmp_path, builds)
    opts = make_options(state_dir=tmp_path / "state")

    with patch("sysforge.primitives.snapshot.ensure_pre_build_snapshot") as mock_snap:
        _run_kernel_with_state(tmp_path, (state, p), opts_override=opts)

    mock_snap.assert_called_once_with({}, dry_run=opts.dry_run)


# ---------------------------------------------------------------------------
# check_pkgname_repo_collision (pkgname shadows a pacman repo package)
# ---------------------------------------------------------------------------

def test_pkgname_collision_no_match_is_noop():
    from sysforge.pipeline.stages.kernel import check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False):
        # No raise, no prompt.
        check_pkgname_repo_collision("linux-sysforge", opts)


def test_pkgname_collision_dry_run_warns_no_prompt():
    from sysforge.pipeline.stages.kernel import check_pkgname_repo_collision

    opts = make_options(dry_run=True)
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice") as mock_prompt:
        check_pkgname_repo_collision("linux", opts)  # no raise
    mock_prompt.assert_not_called()


def test_pkgname_collision_unattended_aborts():
    from sysforge.pipeline.stages.kernel import check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=False), \
         pytest.raises(RuntimeError, match="Aborting unattended"):
        check_pkgname_repo_collision("linux", opts)


def test_pkgname_collision_interactive_confirm_proceeds():
    from sysforge.pipeline.stages.kernel import check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value="y"):
        check_pkgname_repo_collision("linux", opts)  # no raise


def test_pkgname_collision_interactive_decline_aborts():
    from sysforge.pipeline.stages.kernel import check_pkgname_repo_collision

    opts = make_options()
    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice", return_value="n"), \
         pytest.raises(RuntimeError, match="not confirmed"):
        check_pkgname_repo_collision("linux", opts)


# ---------------------------------------------------------------------------
# resolve_base_config / write_base_config (configurable kconfig base)
# ---------------------------------------------------------------------------

def test_resolve_base_config_pkgbuild_default_is_noop():
    from sysforge.pipeline.stages.kernel import resolve_base_config

    label, text = resolve_base_config(kcfg({}))
    assert label == "pkgbuild"
    assert text is None


def test_resolve_base_config_running_seeds(monkeypatch):
    from sysforge.pipeline.stages.kernel import resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y\n")
    label, text = resolve_base_config(kcfg({"base_config": "running"}))
    assert label == "running"
    assert text == "CONFIG_FOO=y\n"


def test_resolve_base_config_running_missing_warns_and_falls_back(monkeypatch):
    from sysforge.pipeline.stages.kernel import resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: None)
    label, text = resolve_base_config(kcfg({"base_config": "running"}))
    assert label == "running"
    assert text is None  # falls back to the PKGBUILD base


def test_resolve_base_config_path(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_base_config

    cfg_file = tmp_path / "my.config"
    cfg_file.write_text("CONFIG_BAR=m\n")
    label, text = resolve_base_config(kcfg({"base_config": str(cfg_file)}))
    assert text == "CONFIG_BAR=m\n"


def test_resolve_base_config_missing_path_raises(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_base_config

    with pytest.raises(RuntimeError, match="does not exist"):
        resolve_base_config(kcfg({"base_config": str(tmp_path / "nope.config")}))


def test_resolve_base_config_invalid_value_raises():
    from sysforge.pipeline.stages.kernel import resolve_base_config

    with pytest.raises(RuntimeError, match="invalid kernel.toml base_config"):
        resolve_base_config(kcfg({"base_config": ""}))


def test_resolve_base_config_cli_overrides_config(monkeypatch):
    # --base-config wins over kernel.toml base_config.
    from sysforge.pipeline.stages.kernel import resolve_base_config

    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y\n")
    opts = SimpleNamespace(base_config="running")
    label, text = resolve_base_config(kcfg({"base_config": "pkgbuild"}), opts)
    assert label == "running"
    assert text == "CONFIG_FOO=y\n"


def test_resolve_base_config_cli_none_falls_back_to_config():
    # options with base_config=None defers to the kernel.toml value.
    from sysforge.pipeline.stages.kernel import resolve_base_config

    opts = SimpleNamespace(base_config=None)
    label, text = resolve_base_config(kcfg({"base_config": "pkgbuild"}), opts)
    assert label == "pkgbuild"
    assert text is None


def test_resolve_base_config_cli_path(tmp_path):
    from sysforge.pipeline.stages.kernel import resolve_base_config

    cfg_file = tmp_path / "cli.config"
    cfg_file.write_text("CONFIG_CLI=y\n")
    opts = SimpleNamespace(base_config=str(cfg_file))
    label, text = resolve_base_config(kcfg({"base_config": "pkgbuild"}), opts)
    assert text == "CONFIG_CLI=y\n"


def test_resolve_base_config_cli_missing_path_raises(tmp_path):
    # A bad CLI value is reported against --base-config, not kernel.toml.
    from sysforge.pipeline.stages.kernel import resolve_base_config

    opts = SimpleNamespace(base_config=str(tmp_path / "nope.config"))
    with pytest.raises(RuntimeError, match="--base-config path does not exist"):
        resolve_base_config(kcfg({}), opts)


def test_write_base_config_writes_file(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = kcfg({
        "pkgname": "linux-sysforge",
        "pkgbuild_src_dir": str(builds),
        "base_config": "running",
    })
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y")
    label = write_base_config(kernel_cfg, dry_run=False)
    assert label == "running"
    out = builds / "linux-sysforge" / "sysforge.base.config"
    assert out.read_text() == "CONFIG_FOO=y\n"  # trailing newline added


def test_write_base_config_dry_run_no_file(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = kcfg({
        "pkgname": "linux-sysforge",
        "pkgbuild_src_dir": str(builds),
        "base_config": "running",
    })
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y")
    write_base_config(kernel_cfg, dry_run=True)
    assert not (builds / "linux-sysforge" / "sysforge.base.config").exists()


def test_write_base_config_pkgbuild_default_writes_nothing(tmp_path):
    from sysforge.pipeline.stages.kernel import write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    kernel_cfg = kcfg({"pkgname": "linux-sysforge", "pkgbuild_src_dir": str(builds)})
    label = write_base_config(kernel_cfg, dry_run=False)
    assert label == "pkgbuild"
    assert not (builds / "linux-sysforge" / "sysforge.base.config").exists()


# ---------------------------------------------------------------------------
# B6: the pre-nconfig pause now lives inside the patched PKGBUILD's prepare()
# (kconfig_plan.review_step, installed via KconfigPlan.install), after the
# merges and right before `make nconfig` — see tests/test_kconfig_plan.py. The
# stage no longer emits a pre-makepkg pause, which fired before the
# in-prepare() merges.
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

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.source.get_scheduler",
               return_value=scheduler_mock), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)

    scheduler_mock.request.assert_called_once()
    mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# capture_lsmod_snapshot / merge_lsmod — accumulating snapshot (F37)
# ---------------------------------------------------------------------------

LSMOD_HEADER = "Module                  Size  Used by\n"


def _mock_lsmod(monkeypatch, stdout):
    real_run = run_seam.subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv == ["lsmod"]:
            return MagicMock(returncode=0, stdout=stdout)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)


def test_snapshot_accumulates_across_captures(tmp_path, monkeypatch):
    snap = tmp_path / "lsmod.snapshot"
    snap.write_text(LSMOD_HEADER + "wireguard 90112 0\n")
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    capture_lsmod_snapshot(tmp_path, dry_run=False)
    text = snap.read_text()
    assert "wireguard" in text  # retained from prior snapshot
    assert "ext4" in text  # newly merged
    assert text.startswith("Module")
    assert text.count("wireguard") == 1  # no duplicate rows


def test_snapshot_fresh_capture_when_missing(tmp_path, monkeypatch):
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    capture_lsmod_snapshot(tmp_path, dry_run=False)
    assert "ext4" in (tmp_path / "lsmod.snapshot").read_text()


def test_snapshot_corrupt_prior_degrades_to_fresh(tmp_path, monkeypatch):
    (tmp_path / "lsmod.snapshot").write_bytes(b"\x00\xff garbage")
    _mock_lsmod(monkeypatch, LSMOD_HEADER + "ext4 999424 1\n")
    with _capture_logs() as logs:
        capture_lsmod_snapshot(tmp_path, dry_run=False)
    text = (tmp_path / "lsmod.snapshot").read_text()
    assert "ext4" in text
    assert "garbage" not in text
    assert _warn_messages(logs)


def test_merge_lsmod_current_wins_on_conflict():
    prior = LSMOD_HEADER + "wireguard 90112 0\n"
    current = LSMOD_HEADER + "wireguard 90112 1\next4 999424 1\n"
    merged = merge_lsmod(prior, current)
    assert merged.startswith("Module")
    assert "wireguard 90112 1" in merged
    assert "ext4" in merged
    assert merged.count("wireguard") == 1


# ---------------------------------------------------------------------------
# stage_lsmod_snapshot — hand the accumulated snapshot to localmodconfig (3.2.0-B35)
# ---------------------------------------------------------------------------


def _lsmod_dirs(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    pb = tmp_path / "src" / "PKGBUILD"
    pb.parent.mkdir()
    pb.write_text("pkgbase=linux-custom\n")
    monkeypatch.setattr(_km.config, "pkgbuild_path", lambda cfg: pb)
    return state, pb.parent / "sysforge.lsmod"


def test_stage_lsmod_snapshot_copies_next_to_pkgbuild(tmp_path, monkeypatch):
    # prepare() can only reach $startdir, never <state_dir>; without the copy
    # the minimizer falls back to live lsmod and the accumulation is lost.
    state, staged = _lsmod_dirs(tmp_path, monkeypatch)
    (state / "lsmod.snapshot").write_text(LSMOD_HEADER + "wireguard 90112 0\n")
    assert stage_lsmod_snapshot(kcfg({}), state, dry_run=False) == staged
    assert "wireguard" in staged.read_text()


def test_stage_lsmod_snapshot_removes_stale_when_capture_off(tmp_path, monkeypatch):
    state, staged = _lsmod_dirs(tmp_path, monkeypatch)
    (state / "lsmod.snapshot").write_text(LSMOD_HEADER + "wireguard 90112 0\n")
    staged.write_text(LSMOD_HEADER + "stale 1 0\n")
    cfg = kcfg({"capture_lsmod_snapshot": False})
    assert stage_lsmod_snapshot(cfg, state, dry_run=False) is None
    assert not staged.exists()  # "off" means live lsmod, not last run's copy


def test_stage_lsmod_snapshot_removes_stale_when_snapshot_missing(tmp_path, monkeypatch):
    state, staged = _lsmod_dirs(tmp_path, monkeypatch)
    staged.write_text(LSMOD_HEADER + "stale 1 0\n")
    assert stage_lsmod_snapshot(kcfg({}), state, dry_run=False) is None
    assert not staged.exists()


def test_stage_lsmod_snapshot_dry_run_writes_nothing(tmp_path, monkeypatch):
    state, staged = _lsmod_dirs(tmp_path, monkeypatch)
    (state / "lsmod.snapshot").write_text(LSMOD_HEADER + "wireguard 90112 0\n")
    assert stage_lsmod_snapshot(kcfg({}), state, dry_run=True) is None
    assert not staged.exists()


def test_kernel_stage_run_stages_lsmod_snapshot(tmp_path):
    # End-to-end wiring: a stage run leaves the merged snapshot at
    # $startdir/sysforge.lsmod, where the rendered minimizer guard reads it.
    builds = tmp_path / "builds"
    pb = make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "lsmod.snapshot").write_text(LSMOD_HEADER + "wireguard 90112 0\n")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        # install.subprocess IS the shared module, so this one patch also
        # answers kconfig's lsmod call; a nested monkeypatch would restore
        # the mock as the "original" and leak it into later tests.
        mock_sub.side_effect = lambda argv, *a, **k: MagicMock(
            returncode=0,
            stdout=LSMOD_HEADER + "ext4 999424 1\n" if argv == ["lsmod"] else "",
        )
        KernelStage().run({}, PipelineState(state_dir), make_options(state_dir=state_dir))

    staged = (pb.parent / "sysforge.lsmod").read_text()
    assert "wireguard" in staged and "ext4" in staged


def _hotplug_opts(**kw):
    ns = types.SimpleNamespace(keep_hotplug_drivers=None, dry_run=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_resolve_keep_hotplug_cli_overrides_toml():
    # CLI --no-keep-hotplug-drivers (False) beats toml true.
    assert resolve_keep_hotplug_drivers(
        kcfg({"keep_hotplug_drivers": True}), _hotplug_opts(keep_hotplug_drivers=False)
    ) is False
    # CLI --keep-hotplug-drivers (True) beats toml false.
    assert resolve_keep_hotplug_drivers(
        kcfg({"keep_hotplug_drivers": False}), _hotplug_opts(keep_hotplug_drivers=True)
    ) is True


def test_resolve_keep_hotplug_falls_through_to_toml():
    assert resolve_keep_hotplug_drivers(
        kcfg({"keep_hotplug_drivers": True}), _hotplug_opts()
    ) is True


def test_resolve_keep_hotplug_default_off():
    assert resolve_keep_hotplug_drivers(kcfg({}), _hotplug_opts()) is False


def test_write_hotplug_fragment_writes_when_on(tmp_path, monkeypatch):
    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgbase=linux-custom\n")
    monkeypatch.setattr(_km.config, "pkgbuild_path", lambda cfg: pb)
    path = write_hotplug_fragment(
        kcfg({"keep_hotplug_drivers": True}), _hotplug_opts(), dry_run=False
    )
    assert path == pb.parent / "sysforge.hotplug.config"
    body = path.read_text()
    # Tristate symbols land as =m.
    assert "CONFIG_USB=m" in body
    assert "CONFIG_MMC=m" in body
    assert "CONFIG_PCCARD=m" in body
    # Bool symbols land as =y — kconfig rejects 'm' for them (2.6.1-B17).
    assert "CONFIG_HOTPLUG_PCI=y" in body
    # No `is not set` lines — this fragment only ever enables.
    assert "is not set" not in body


# Kconfig symbols in HOTPLUG_KCONFIG that are declared `bool`, not `tristate`,
# in the kernel tree. Writing "m" for these makes kconfig discard the whole
# assignment with `symbol value 'm' invalid for X`, silently losing the F2
# intent (2.6.1-B17). Extend when adding a bool symbol to the curated set.
_BOOL_HOTPLUG_SYMBOLS = frozenset(
    {"CONFIG_HOTPLUG_PCI", "CONFIG_HOTPLUG_PCI_PCIE", "CONFIG_CARDBUS"}
)


def test_hotplug_kconfig_bool_symbols_are_not_modules():
    for symbol in _BOOL_HOTPLUG_SYMBOLS:
        assert symbol in _km.kconfig.HOTPLUG_KCONFIG, f"{symbol} dropped from curated set"
        assert _km.kconfig.HOTPLUG_KCONFIG[symbol] == "y", (
            f"{symbol} is a bool kconfig symbol; 'm' is rejected by kconfig"
        )


def test_hotplug_kconfig_values_are_legal():
    for symbol, value in _km.kconfig.HOTPLUG_KCONFIG.items():
        expected = "y" if symbol in _BOOL_HOTPLUG_SYMBOLS else "m"
        assert value == expected, f"{symbol}={value} (expected {expected})"


def test_hotplug_kconfig_keeps_removable_media_filesystems():
    # 3.2.0-B35: localmodconfig strips a filesystem module nobody mounted
    # while capturing, so a USB stick / SD card / optical disc stops mounting.
    # NLS_UTF8 is exFAT's default iocharset — without it the mount fails.
    for symbol in ("CONFIG_FAT_FS", "CONFIG_VFAT_FS", "CONFIG_EXFAT_FS",
                   "CONFIG_ISO9660_FS", "CONFIG_UDF_FS", "CONFIG_NTFS3_FS",
                   "CONFIG_NLS_UTF8", "CONFIG_NLS_ISO8859_1"):
        assert _km.kconfig.HOTPLUG_KCONFIG.get(symbol) == "m", symbol


def test_hotplug_kconfig_has_no_removed_symbols():
    # CONFIG_THUNDERBOLT was renamed to CONFIG_USB4 in 5.6; a fragment line for
    # a symbol the tree no longer declares is silently dropped (2.6.1-B17).
    assert "CONFIG_THUNDERBOLT" not in _km.kconfig.HOTPLUG_KCONFIG
    assert "CONFIG_USB4" in _km.kconfig.HOTPLUG_KCONFIG


def test_write_hotplug_fragment_removes_stale_when_off(tmp_path, monkeypatch):
    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgbase=linux-custom\n")
    stale = pb.parent / "sysforge.hotplug.config"
    stale.write_text("CONFIG_USB=m\n")
    monkeypatch.setattr(_km.config, "pkgbuild_path", lambda cfg: pb)
    result = write_hotplug_fragment(kcfg({}), _hotplug_opts(), dry_run=False)
    assert result is None
    assert not stale.exists()  # "off" means off


def test_write_hotplug_fragment_dry_run_is_noop(tmp_path, monkeypatch):
    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgbase=linux-custom\n")
    monkeypatch.setattr(_km.config, "pkgbuild_path", lambda cfg: pb)
    result = write_hotplug_fragment(
        kcfg({"keep_hotplug_drivers": True}), _hotplug_opts(), dry_run=True
    )
    assert result is None
    assert not (pb.parent / "sysforge.hotplug.config").exists()


def test_cli_keep_hotplug_flag_parses():
    from sysforge.cli import _build_parser

    parser = _build_parser()
    assert parser.parse_args(
        ["run", "kernel", "--keep-hotplug-drivers"]
    ).keep_hotplug_drivers is True
    assert parser.parse_args(
        ["run", "kernel", "--no-keep-hotplug-drivers"]
    ).keep_hotplug_drivers is False
    assert parser.parse_args(
        ["run", "kernel"]
    ).keep_hotplug_drivers is None


# ---------------------------------------------------------------------------
# AlreadyBuilt × interactive semantics (2.5.1-B5)
#
# makepkg exit 13 (stale same-version package in PKGDEST) skips prepare()
# entirely — so the interactive `make nconfig` review the stage promised
# never ran. An interactive run must say so and ask (install as-built /
# rebuild with -f to review / abort); unattended runs keep the proceed
# behaviour. The build itself was skipped, so "rebuild" re-invokes makepkg
# with -f to force a real build (and with it the in-prepare() review).
# ---------------------------------------------------------------------------

def _run_stage_already_built(tmp_path, *, prompt_ret=None, tty=True,
                             non_interactive=False, rebuild_raises=False):
    """Drive KernelStage.run() with makepkg raising AlreadyBuilt on the first
    call. Returns (mock_build, mock_prompt, mock_install, logs, excinfo)."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.makepkg_invoke import AlreadyBuilt

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    if non_interactive:
        opts.non_interactive = True

    calls = []

    def fake_makepkg(pkgbuild, *, options):
        calls.append(options)
        if len(calls) == 1:
            raise AlreadyBuilt(pkgbuild)
        if rebuild_raises:
            raise AlreadyBuilt(pkgbuild)

    excinfo = None
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.stage.makepkg_run",
               side_effect=fake_makepkg) as mock_build, \
         patch("sysforge.primitives.prompt.prompt_choice",
               return_value=prompt_ret) as mock_prompt, \
         patch("sysforge.primitives.prompt.is_interactive",
               return_value=tty), \
         patch.object(_km.stage, "install_built_packages") as mock_install, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         patch("sysforge.build.makepkg_wrapper.built_kernel_artifact_release",
               return_value=None), \
         _capture_logs() as logs:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        try:
            KernelStage().run({}, state, opts)
        except RuntimeError as e:
            excinfo = e
    return mock_build, mock_prompt, mock_install, logs, excinfo


def test_already_built_interactive_abort_raises(tmp_path):
    """Interactive run, operator picks abort → RuntimeError, nothing installed."""
    mock_build, mock_prompt, mock_install, logs, exc = _run_stage_already_built(
        tmp_path, prompt_ret="a")
    assert exc is not None and "review" in str(exc).lower()
    assert mock_build.call_count == 1
    mock_install.assert_not_called()


def test_already_built_interactive_warns_review_skipped(tmp_path):
    """The prompt is preceded by a WARN that the config review did not run."""
    _, _, _, logs, _ = _run_stage_already_built(tmp_path, prompt_ret="a")
    warned = " ".join(str(c.args[1]) for c in logs.warn.call_args_list)
    assert "review" in warned and "already built" in warned


def test_already_built_interactive_rebuild_forces_makepkg(tmp_path):
    """Operator picks rebuild → makepkg re-invoked with -f, then install."""
    mock_build, mock_prompt, mock_install, logs, exc = _run_stage_already_built(
        tmp_path, prompt_ret="r")
    assert exc is None
    assert mock_build.call_count == 2
    second_opts = mock_build.call_args_list[1].kwargs["options"]
    assert "-f" in (second_opts.extra_flags or [])
    mock_install.assert_called_once()


def test_already_built_interactive_install_proceeds(tmp_path):
    """Operator picks install-as-built → no rebuild, install proceeds."""
    mock_build, mock_prompt, mock_install, logs, exc = _run_stage_already_built(
        tmp_path, prompt_ret="i")
    assert exc is None
    assert mock_build.call_count == 1
    mock_install.assert_called_once()


def test_already_built_unattended_proceeds_without_prompt(tmp_path):
    """--non-interactive keeps today's behaviour: proceed, never prompt."""
    mock_build, mock_prompt, mock_install, logs, exc = _run_stage_already_built(
        tmp_path, non_interactive=True)
    assert exc is None
    mock_prompt.assert_not_called()
    assert mock_build.call_count == 1
    mock_install.assert_called_once()


def test_already_built_no_tty_proceeds_without_prompt(tmp_path):
    """Interactive per config but no TTY → treated as unattended (no hang)."""
    mock_build, mock_prompt, mock_install, logs, exc = _run_stage_already_built(
        tmp_path, tty=False)
    assert exc is None
    mock_prompt.assert_not_called()
    mock_install.assert_called_once()


def test_already_built_install_failure_suggests_fresh_build(tmp_path):
    """B7 loop breaker: AlreadyBuilt → install-as-built → pacman failure is
    the state that reproduces itself on every re-run. The error must point at
    the way out (rebuild / remove the stale PKGDEST package)."""
    import sysforge.pipeline.stages.kernel as _km
    from sysforge.primitives.makepkg_invoke import AlreadyBuilt

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    opts.non_interactive = True

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.stage.makepkg_run",
               side_effect=AlreadyBuilt(builds / "PKGBUILD")), \
         patch.object(_km.stage, "install_built_packages",
                      side_effect=RuntimeError("pacman -U failed (exit 1)")), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         pytest.raises(RuntimeError, match="stale"):
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)


# ---------------------------------------------------------------------------
# Unified interactivity resolution (2.5.1-B8)
#
# The kconfig gate resolved interactive from config + --non-interactive only,
# never consulting the TTY — so a piped/captured run "promised" an nconfig
# review that could never render, then silently EOF'd through it. The gate
# now matches the stage's sibling decisions (collision/diverged prompts):
# interactive requires config ∧ ¬flag ∧ TTY, and a config-requested review
# downgraded by a missing TTY says so at WARN.
# ---------------------------------------------------------------------------

def _run_stage_tty(tmp_path, *, tty):
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None) as mock_build, \
         patch("sysforge.primitives.prompt.is_interactive", return_value=tty), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         _capture_logs() as logs:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    return mock_build.call_args.kwargs["options"], logs


def test_kernel_stage_no_tty_resolves_non_interactive(tmp_path):
    """Config-default interactive but no TTY → build runs non-interactive."""
    build_opts, logs = _run_stage_tty(tmp_path, tty=False)
    assert build_opts.interactive is False


def test_kernel_stage_no_tty_warns_review_downgraded(tmp_path):
    """The TTY-forced downgrade of a config-requested review is WARNed."""
    _, logs = _run_stage_tty(tmp_path, tty=False)
    warned = " ".join(str(c.args[1]) for c in logs.warn.call_args_list)
    assert "no TTY" in warned and "review" in warned


def test_kernel_stage_tty_keeps_interactive(tmp_path):
    """With a TTY, config-default interactive stays interactive."""
    build_opts, logs = _run_stage_tty(tmp_path, tty=True)
    assert build_opts.interactive is True


def test_kernel_stage_non_interactive_flag_no_tty_warn(tmp_path):
    """--non-interactive is an explicit request — no downgrade warn."""
    import sysforge.pipeline.stages.kernel as _km
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    opts.non_interactive = True
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch("sysforge.primitives.prompt.is_interactive", return_value=False), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub, \
         _capture_logs() as logs:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)
    warned = " ".join(str(c.args[1]) for c in logs.warn.call_args_list)
    assert "no TTY" not in warned


# ---------------------------------------------------------------------------
# 2.6.1-F25 — kconfig change-summary blocks
# ---------------------------------------------------------------------------


def _change(option, old, new, kind):
    from sysforge.primitives.kernel_safety import KconfigChange

    return KconfigChange(option=option, old=old, new=new, kind=kind)


def test_kconfig_diff_lines_render_each_kind():
    from sysforge.pipeline.stages.kernel import kconfig_diff_lines

    lines = kconfig_diff_lines("6.14.0", [
        _change("CONFIG_NEW", "", "y", "added"),
        _change("CONFIG_GONE", "m", "", "removed"),
        _change("CONFIG_NUMA", "y", "n", "changed"),
    ])
    assert lines[0] == "3 symbol(s) changed since 6.14.0:"
    assert "  +CONFIG_NEW=y" in lines
    assert "  -CONFIG_GONE (was m)" in lines
    assert "  CONFIG_NUMA: y → n" in lines


def test_kconfig_diff_lines_say_so_when_nothing_moved():
    """An identical config is a real, reportable answer — not silence."""
    from sysforge.pipeline.stages.kernel import kconfig_diff_lines

    assert kconfig_diff_lines("6.14.0", []) == ["no kconfig changes since 6.14.0"]


def test_kconfig_diff_lines_cap_at_40_symbols():
    """A major bump changes thousands; the inline block must not bury the rows."""
    from sysforge.pipeline.stages.kernel.gates import (
        KCONFIG_DIFF_CAP,
        kconfig_diff_lines,
    )

    changes = [_change(f"CONFIG_S{n}", "n", "y", "changed") for n in range(100)]
    lines = kconfig_diff_lines("6.14.0", changes)
    # header + cap + the "and N more" pointer
    assert len(lines) == 1 + KCONFIG_DIFF_CAP + 1
    assert lines[-1].endswith("and 60 more (full list in the run log)")


def test_kconfig_diff_lines_glyphs_degrade_under_the_ascii_gate(monkeypatch):
    from sysforge import log
    from sysforge.pipeline.stages.kernel import kconfig_diff_lines

    monkeypatch.setattr(log, "use_unicode", lambda: False)
    changes = [_change(f"CONFIG_S{n}", "n", "y", "changed") for n in range(50)]
    lines = kconfig_diff_lines("6.14.0", changes)
    assert "  CONFIG_S0: n -> y" in lines
    assert lines[-1].startswith("  ... and 10 more")


def test_kconfig_drift_lines_report_a_check_that_never_ran():
    """B6: on the AlreadyBuilt path this must say so, not render silence."""
    from sysforge.pipeline.stages.kernel import kconfig_drift_lines

    lines = kconfig_drift_lines(None)
    assert len(lines) == 1
    assert "did NOT run" in lines[0]


def test_kconfig_drift_lines_distinguish_no_drift_from_no_check():
    from sysforge.pipeline.stages.kernel import kconfig_drift_lines

    assert kconfig_drift_lines([]) == [
        "all merged options survived into the resolved .config"
    ]


def test_kconfig_drift_lines_render_each_drift():
    from sysforge.pipeline.stages.kernel import kconfig_drift_lines
    from sysforge.primitives.kernel_safety import KconfigDrift

    lines = kconfig_drift_lines([
        KconfigDrift(option="CONFIG_X", requested="y", resolved="n", kind="disabled"),
    ])
    assert lines == ["  CONFIG_X: y → n (disabled)"]


def test_change_extras_is_empty_before_the_merge_check_is_reached():
    """A stage that no-opped early must not claim the drift check was skipped."""
    from sysforge.pipeline.stages.kernel import KernelStage

    stage = KernelStage()
    stage._kconfig_diff = None
    stage._kconfig_drift = None
    stage._reported_kconfig_merge = False
    assert stage.change_extras({}, MagicMock(), MagicMock()) == []


def test_change_extras_emits_both_blocks():
    from sysforge.pipeline.stages.kernel import KernelStage

    stage = KernelStage()
    stage._kconfig_diff = ("6.14.0", [_change("CONFIG_NUMA", "y", "n", "changed")])
    stage._kconfig_drift = []
    stage._reported_kconfig_merge = True

    blocks = stage.change_extras({}, MagicMock(), MagicMock())
    assert [b.label for b in blocks] == [
        "Kconfig vs previous build:", "Kconfig merge drift:",
    ]


def test_change_extras_sends_the_overflow_to_the_log(monkeypatch):
    """Capped inline, complete in the run log — the cap must not lose data."""
    from sysforge.pipeline.stages import kernel as kernel_mod

    # The Logger's methods are read-only, so swap the whole module-level logger
    # for a recorder rather than patching a bound method.
    logged = []
    monkeypatch.setattr(
        kernel_mod.stage, "_log", SimpleNamespace(info=logged.append, warn=lambda m: None)
    )

    stage = kernel_mod.KernelStage()
    changes = [_change(f"CONFIG_S{n}", "n", "y", "changed") for n in range(45)]
    stage._kconfig_diff = ("6.14.0", changes)
    stage._reported_kconfig_merge = False
    stage.change_extras({}, MagicMock(), MagicMock())

    assert len(logged) == 5
    assert "CONFIG_S44" in logged[-1]


def test_record_and_diff_kconfig_returns_none_without_a_build_tree(tmp_path, monkeypatch):
    """The AlreadyBuilt path has no resolved .config — nothing to diff."""
    from sysforge.pipeline.stages import kernel as kernel_mod

    monkeypatch.setattr(kernel_mod.gates, "resolve_built_config", lambda d, **kw: None)
    assert kernel_mod.gates.record_and_diff_kconfig(tmp_path, "linux-custom", tmp_path) is None


def test_record_and_diff_kconfig_archives_then_diffs(tmp_path, monkeypatch):
    from sysforge.pipeline.stages import kernel as kernel_mod
    from sysforge.primitives import kconfig_history

    state = tmp_path / "state"
    old = tmp_path / "old.config"
    old.write_text("CONFIG_SMP=y\nCONFIG_NUMA=y\n", encoding="utf-8")
    kconfig_history.archive(state, "linux-custom", "6.14.0", old)

    new = tmp_path / "new.config"
    new.write_text("CONFIG_SMP=y\nCONFIG_NUMA=n\n", encoding="utf-8")
    monkeypatch.setattr(kernel_mod.gates, "resolve_built_config", lambda d, **kw: new)
    monkeypatch.setattr(kernel_mod.gates, "built_kernel_release", lambda p: "6.15.0")

    result = kernel_mod.gates.record_and_diff_kconfig(state, "linux-custom", tmp_path)
    assert result is not None
    prev_release, changes = result
    assert prev_release == "6.14.0"
    assert [c.option for c in changes] == ["CONFIG_NUMA"]
    # and this build is now archived for the next run to compare against
    assert kconfig_history.archive_path(state, "linux-custom", "6.15.0").exists()


def test_record_and_diff_kconfig_never_raises(tmp_path, monkeypatch):
    """Advisory reporting must not be able to break a kernel build."""
    from sysforge.pipeline.stages import kernel as kernel_mod

    def boom(_, **kw):
        raise RuntimeError("build tree vanished")

    monkeypatch.setattr(kernel_mod.gates, "resolve_built_config", boom)
    assert kernel_mod.gates.record_and_diff_kconfig(tmp_path, "linux-custom", tmp_path) is None


# ---------------------------------------------------------------------------
# KernelConfig — one home for every default (3.2.0-F6)
# ---------------------------------------------------------------------------

def test_kernel_config_from_toml_none_is_none():
    """No kernel.toml keeps meaning 'stage is a clean no-op'."""
    assert KernelConfig.from_toml(None) is None


def test_kernel_config_empty_toml_gets_documented_defaults():
    cfg = KernelConfig.from_toml({})
    assert cfg.enabled is False          # opt-in
    assert cfg.interactive is True
    assert cfg.build_headers is True     # headers are useful; docs are not, by default
    assert cfg.build_docs is False
    assert cfg.base_config == "pkgbuild"
    assert cfg.kconfig_merge is True
    assert cfg.boot_audit is True        # boot safety is on unless disabled
    assert cfg.require_fallback_kernel is True
    assert cfg.min_boot_free_mb == 200
    assert cfg.bootloader == "systemd-boot"
    assert cfg.manual_kconfig == ()


def test_kernel_config_compiler_unset_is_none_not_a_default():
    """The distinction resolve_compiler depends on.

    An unset ``compiler`` must stay ``None`` rather than acquiring a default
    here, because that is the signal to fall through to the toolchain stage's
    result in pipeline state. Defaulting it to "gcc" at parse time would make a
    machine that ran ``run toolchain`` with compiler = "llvm" silently build its
    kernel with gcc.
    """
    assert KernelConfig.from_toml({}).compiler is None
    assert KernelConfig.from_toml({"compiler": ""}).compiler is None
    assert KernelConfig.from_toml({"compiler": "llvm"}).compiler == "llvm"


def test_kernel_config_is_frozen():
    """A stage's configuration is decided at entry and cannot drift mid-run.

    The stage used to stamp the resolved pkgbuild_src_dir back into the config
    dict in place; it now rebinds to a replaced instance instead.
    """
    cfg = KernelConfig.from_toml({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.enabled = True


def test_kernel_config_replace_carries_raw_alongside_the_field():
    """The pkgbuild_src_dir hand-off the stage performs.

    ``raw`` is still read by resolve_pkgbuild_src_dir, so a replaced instance
    that updated only the field would hand the next reader a stale table.
    """
    cfg = KernelConfig.from_toml({"pkgname": "linux-custom"})
    updated = dataclasses.replace(
        cfg, pkgbuild_src_dir="/builds", raw={**cfg.raw, "pkgbuild_src_dir": "/builds"},
    )
    assert updated.pkgbuild_src_dir == "/builds"
    assert updated.raw["pkgbuild_src_dir"] == "/builds"
    assert cfg.pkgbuild_src_dir is None      # the original is untouched


def test_kernel_config_manual_kconfig_section_is_carried():
    entries = [{"option": "CONFIG_SMP", "value": "y"}]
    cfg = KernelConfig.from_toml({"kconfig": entries})
    assert list(cfg.manual_kconfig) == entries


# ---------------------------------------------------------------------------
# 3.2.0-F17 — base_config_merge
# ---------------------------------------------------------------------------

def _running_base(tmp_path, monkeypatch, **extra):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text",
        lambda: "CONFIG_FOO=y")
    cfg = kcfg({"pkgname": "linux-sysforge", "pkgbuild_src_dir": str(builds),
                "base_config": "running", **extra})
    return cfg, builds / "linux-sysforge"


def test_base_config_merge_defaults_to_replace(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch)
    assert write_base_config(cfg, dry_run=False) == "running"
    assert (d / "sysforge.base.config").is_file()
    assert not (d / "sysforge.base.overlay.config").exists()


def test_base_config_merge_overlay_writes_the_overlay_file(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch, base_config_merge="overlay")
    (d / "sysforge.base.config").write_text("stale\n")  # a prior replace run
    label = write_base_config(cfg, dry_run=False)
    assert label == "running (overlay)"
    assert (d / "sysforge.base.overlay.config").read_text() == "CONFIG_FOO=y\n"
    assert not (d / "sysforge.base.config").exists(), "stale replace seed removed"


def test_base_config_merge_replace_removes_a_stale_overlay(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch)
    (d / "sysforge.base.overlay.config").write_text("stale\n")
    write_base_config(cfg, dry_run=False)
    assert not (d / "sysforge.base.overlay.config").exists()


def test_base_config_merge_cli_overrides_kernel_toml(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch)
    label = write_base_config(
        cfg, dry_run=False, options=SimpleNamespace(base_config=None,
                                                    base_config_merge="overlay"))
    assert label == "running (overlay)"


def test_base_config_merge_rejects_an_unknown_mode(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, _ = _running_base(tmp_path, monkeypatch, base_config_merge="blend")
    with pytest.raises(RuntimeError, match="base_config_merge"):
        write_base_config(cfg, dry_run=False)


def test_base_config_summary_says_when_a_cooperating_pkgbuild_ignores_it(
        tmp_path, monkeypatch):
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch, base_config_merge="overlay")
    (d / "PKGBUILD").write_text(
        (d / "PKGBUILD").read_text()
        + "\nprepare() {\n  scripts/kconfig/merge_config.sh -m .config x\n}\n")
    label = write_base_config(cfg, dry_run=False)
    assert "inert" in label and "merge_config.sh" in label


def test_cli_parses_base_config_merge():
    from sysforge.cli import _build_parser

    args = _build_parser().parse_args(["run", "kernel", "--base-config-merge", "overlay"])
    assert args.base_config_merge == "overlay"


# ---------------------------------------------------------------------------
# 3.3.0-B1 — a stale base seed must not outlive the base_config that wrote it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stale", ["sysforge.base.config", "sysforge.base.overlay.config"])
def test_pkgbuild_base_removes_a_stale_seed(tmp_path, stale):
    """Switching back to "pkgbuild" must mean the packager's config — the
    file-guarded seed would otherwise keep applying the old running config."""
    from sysforge.pipeline.stages.kernel import write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    seed = builds / "linux-sysforge" / stale
    seed.write_text("CONFIG_OLD=y\n")
    cfg = kcfg({"pkgname": "linux-sysforge", "pkgbuild_src_dir": str(builds)})
    assert write_base_config(cfg, dry_run=False) == "pkgbuild"
    assert not seed.exists()


def test_unavailable_running_base_removes_a_stale_seed(tmp_path, monkeypatch):
    """The fallback warning says "PKGBUILD base" — it must be true."""
    from sysforge.pipeline.stages.kernel import write_base_config

    cfg, d = _running_base(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sysforge.primitives.dep_analysis.read_running_kconfig_text", lambda: None)
    (d / "sysforge.base.config").write_text("CONFIG_OLD=y\n")
    write_base_config(cfg, dry_run=False)
    assert not (d / "sysforge.base.config").exists()


def test_dry_run_leaves_a_stale_seed_alone(tmp_path):
    from sysforge.pipeline.stages.kernel import write_base_config

    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-sysforge")
    seed = builds / "linux-sysforge" / "sysforge.base.config"
    seed.write_text("CONFIG_OLD=y\n")
    cfg = kcfg({"pkgname": "linux-sysforge", "pkgbuild_src_dir": str(builds)})
    write_base_config(cfg, dry_run=True)
    assert seed.exists()


# ---------------------------------------------------------------------------
# 3.3.0-F5: managed systemd-boot entries wired into the kernel stage
# ---------------------------------------------------------------------------

from sysforge.pipeline.stages.kernel import install as kinstall  # noqa: E402
from sysforge.primitives import boot_entries as be  # noqa: E402
from sysforge.primitives import run as run_seam

OPERATOR_STAGE = (
    "title Arch Linux Sysforge\nlinux /vmlinuz-linux-sysforge\n"
    "initrd /initramfs-linux-sysforge.img\noptions root=UUID=abc rw\n"
)


def test_fdo_role():
    assert kinstall.fdo_role(None, False) == "plain"
    assert kinstall.fdo_role("record", True) == "profiling"
    assert kinstall.fdo_role("use", False) == "autofdo"
    assert kinstall.fdo_role("use", True) == "propeller"


def _be_tree(tmp_path, monkeypatch, entries=None, images=("linux-sysforge",)):
    boot = tmp_path / "boot"
    d = boot / "loader" / "entries"
    d.mkdir(parents=True)
    for name, text in (entries or {}).items():
        (d / name).write_text(text)
    for k in images:
        (boot / f"vmlinuz-{k}").write_bytes(b"x")
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    monkeypatch.setattr(be, "read_selected_entry", lambda *a, **k: "97-arch-custom.conf")
    monkeypatch.setattr(be, "read_pretty_name", lambda *a, **k: "Arch Linux")
    return d


def test_boot_entries_preflight_noop_when_not_managed(monkeypatch):
    monkeypatch.setattr(be, "check_boot_path", lambda **k: (_ for _ in ()).throw(AssertionError))
    kinstall.preflight_boot_entries("linux-sysforge", manage=False)


def test_boot_entries_preflight_refuses_without_template(tmp_path, monkeypatch):
    _be_tree(tmp_path, monkeypatch, entries={})
    monkeypatch.setattr(be, "check_boot_path", lambda **k: None)
    with pytest.raises(RuntimeError, match='boot_entries = "off"'):
        kinstall.preflight_boot_entries("linux-sysforge", manage=True)


def test_boot_entries_preflight_unreadable_entries_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "BOOT_DIR", tmp_path / "nope")
    monkeypatch.setattr(be, "check_boot_path", lambda **k: None)
    with pytest.raises(RuntimeError, match="cannot read"):
        kinstall.preflight_boot_entries("linux-sysforge", manage=True)


def test_boot_entries_sync_writes_entry_for_new_kernel(tmp_path, monkeypatch):
    _be_tree(tmp_path, monkeypatch, entries={"97-arch-custom.conf": OPERATOR_STAGE},
             images=("linux-sysforge", "linux-sysforge-fdo"))
    applied = {}
    monkeypatch.setattr(be, "apply_plan",
                        lambda plan, dry_run, **k: applied.setdefault("plan", plan) and [])
    kinstall.sync_boot_entries("linux-sysforge", "linux-sysforge-fdo", "profiling", "7.2.7",
                               manage=True, prefix=None, dry_run=False)
    names = [w[0] for w in applied["plan"].writes]
    assert names == ["sysforge-linux-sysforge-fdo.conf"]


def test_boot_entries_sync_raises_when_wanted_kernel_skipped_but_applies_rest(
        tmp_path, monkeypatch):
    hdr = "# sysforge-managed (template: 97-arch-custom.conf, role: propeller) — r\n"
    _be_tree(
        tmp_path, monkeypatch,
        entries={
            "97-arch-custom.conf": OPERATOR_STAGE,
            # operator-owned file squatting on the wanted kernel's entry name
            "sysforge-linux-sysforge-fdo.conf": "linux /vmlinuz-other\n",
            "sysforge-linux-sysforge-propeller.conf":
                hdr + "linux /vmlinuz-linux-sysforge-propeller\n",
        },
        images=("linux-sysforge", "linux-sysforge-fdo", "linux-sysforge-propeller"))
    applied = {}
    monkeypatch.setattr(be, "apply_plan",
                        lambda plan, dry_run, **k: applied.setdefault("plan", plan) and [])
    with pytest.raises(RuntimeError, match=r"^\[KERNEL\] cannot write sysforge-linux-sysforge-fdo"):
        kinstall.sync_boot_entries("linux-sysforge", "linux-sysforge-fdo", "plain", "7.2.7",
                                   manage=True, prefix=None, dry_run=False)
    assert [w[0] for w in applied["plan"].writes] == [
        "sysforge-linux-sysforge-propeller.conf"]


def test_boot_entries_sync_raises_when_rendered_paths_missing(tmp_path, monkeypatch):
    _be_tree(tmp_path, monkeypatch,
             entries={"97-arch-custom.conf":
                      "linux /vmlinuz-linux-sysforge\ninitrd /booster-linux-sysforge.img\n"},
             images=("linux-sysforge", "linux-sysforge-fdo"))
    applied = {}
    monkeypatch.setattr(be, "apply_plan",
                        lambda plan, dry_run, **k: applied.setdefault("plan", plan) and [])
    with pytest.raises(RuntimeError, match=r"/booster-linux-sysforge\.img"):
        kinstall.sync_boot_entries("linux-sysforge", "linux-sysforge-fdo", "plain", "7.2.7",
                                   manage=True, prefix=None, dry_run=False)
    assert applied["plan"].writes == ()


def test_boot_entries_stage_call_order(tmp_path, monkeypatch):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    order = []

    monkeypatch.setattr(kinstall, "preflight_boot_entries",
                        lambda *a, **k: order.append("preflight"))
    monkeypatch.setattr(kinstall, "prune_boot_entries",
                        lambda *a, **k: order.append("prune"))
    monkeypatch.setattr(kinstall, "run_mkinitcpio", lambda *a, **k: order.append("mkinitcpio"))
    monkeypatch.setattr(kinstall, "update_bootloader",
                        lambda *a, **k: order.append("update_bootloader"))
    monkeypatch.setattr(kinstall, "sync_boot_entries",
                        lambda *a, **k: order.append("sync"))
    monkeypatch.setattr(_km.gates, "gate3_verify", lambda *a, **k: order.append("gate3"))
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda *a, **k: None)
    monkeypatch.setattr(_km.gates, "built_kernel_release", lambda *a, **k: "7.2.7")

    def fake_build(*a, **k):
        order.append("build")

    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, side_effect=fake_build), \
         patch.object(_km.stage, "install_built_packages", MagicMock(return_value=[])):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))

    assert order.index("preflight") < order.index("build")
    assert order.index("prune") < order.index("build")
    tail = [s for s in order if s in ("mkinitcpio", "update_bootloader", "sync", "gate3")]
    assert tail == ["mkinitcpio", "update_bootloader", "sync", "gate3"]


def test_boot_entries_preflight_returns_template(tmp_path, monkeypatch):
    _be_tree(tmp_path, monkeypatch, entries={"97-arch-custom.conf": OPERATOR_STAGE})
    monkeypatch.setattr(be, "check_boot_path", lambda **k: None)
    tpl = kinstall.preflight_boot_entries("linux-sysforge", manage=True)
    assert isinstance(tpl, be.Template)
    assert kinstall.preflight_boot_entries("linux-sysforge", manage=False) is None


class _StopAfterAdvisory(Exception):
    pass


def _record_advisory_text(tmp_path, monkeypatch, template, prefix, plan=None):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    warns = []
    monkeypatch.setattr(_km.fdo, "resolve_fdo", lambda o, c=None: ("record", False, True))
    if plan is not None:
        monkeypatch.setattr(_km.fdo, "plan_fdo_build", lambda *a, **k: plan)
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm", lambda *a, **k: None)
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("clang", "clang", None))
    monkeypatch.setattr(_km.config, "resolve_boot_entries", lambda c, b: (True, prefix))
    monkeypatch.setattr(kinstall, "preflight_boot_entries", lambda *a, **k: template)
    monkeypatch.setattr(kinstall, "prune_boot_entries", lambda *a, **k: None)
    monkeypatch.setattr(kernel_fdo, "detect_branch_sampling",
                        lambda *a, **k: SimpleNamespace(supported=True, note="NOTE"))
    monkeypatch.setattr(_km.stage, "_log", SimpleNamespace(
        warn=lambda m, *a, **k: warns.append(m),
        info=lambda *a, **k: None, ui=lambda *a, **k: None,
        debug=lambda *a, **k: None))

    def stop(*a, **k):
        raise _StopAfterAdvisory

    monkeypatch.setattr(_km.gates, "gate1_preflight", stop)
    with patch.object(_km.config, "KERNEL_PATH", p), pytest.raises(_StopAfterAdvisory):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    return next(w for w in warns if "profiling kernel" in w)


def test_record_advisory_oneshot_ignores_prefix_with_sort_key(tmp_path, monkeypatch):
    tpl = be.Template(
        be.parse_entry("a.conf", "title A\nsort-key arch\nlinux /vmlinuz-linux\n"), "linux")
    text = _record_advisory_text(tmp_path, monkeypatch, tpl, "pre")
    eff = kernel_fdo.record_pkgname("linux-git")
    assert f"sudo bootctl set-oneshot sysforge-{eff}.conf" in text


def test_record_advisory_oneshot_uses_prefix_without_sort_key(tmp_path, monkeypatch):
    tpl = be.Template(be.parse_entry("a.conf", OPERATOR_STAGE), "linux-sysforge")
    text = _record_advisory_text(tmp_path, monkeypatch, tpl, "pre")
    eff = kernel_fdo.record_pkgname("linux-git")
    assert f"sudo bootctl set-oneshot pre-sysforge-{eff}.conf" in text


def test_boot_entries_sync_failure_explains_installed_state(tmp_path, monkeypatch):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    gate3 = MagicMock()

    def boom(*a, **k):
        raise RuntimeError("[KERNEL] boot entry x: cp failed")

    monkeypatch.setattr(kinstall, "sync_boot_entries", boom)
    monkeypatch.setattr(_km.gates, "gate3_verify", gate3)
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda *a, **k: None)
    monkeypatch.setattr(_km.gates, "built_kernel_release", lambda *a, **k: "7.2.7")
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages", MagicMock(return_value=[])), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as sub:
        sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError) as ei:
            KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    msg = str(ei.value)
    assert "is installed" in msg and "could not be written" in msg
    assert "boot entry x: cp failed" in msg
    gate3.assert_not_called()


# ---------------------------------------------------------------------------
# FdoPlan — two-round Propeller wiring (3.3.0-B14)
# ---------------------------------------------------------------------------


@pytest.fixture
def fdo_stores(tmp_path, monkeypatch):
    tcfg = {"profile_store": str(tmp_path / "store")}
    monkeypatch.setattr(kernel_fdo, "_load_tcfg", lambda: tcfg)
    afdo = kernel_fdo.resolve_store("linux-sysforge", propeller=False, tcfg=tcfg)
    prop = kernel_fdo.resolve_store("linux-sysforge", propeller=True, tcfg=tcfg)
    return afdo, prop


def test_plan_record_round1(fdo_stores):
    afdo, _ = fdo_stores
    p = kfdo.plan_fdo_build("record", False, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert (p.eff_pkgname, p.env, p.build_mode, p.round_store) == (
        "linux-sysforge-profiling", None, None, afdo)


def test_plan_record_round2_pins_and_applies(fdo_stores):
    afdo, prop = fdo_stores
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    p = kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.env == {kernel_fdo.ENV_AUTOFDO: str(prop / "kernel.afdo")}
    assert (prop / "kernel.afdo").read_bytes() == b"A"
    assert p.afdo_sha256 == kernel_fdo.file_sha256(afdo / "kernel.afdo")
    assert p.round_store == prop and p.eff_pkgname == "linux-sysforge-profiling"


def test_plan_record_round2_without_round1_refuses(fdo_stores):
    with pytest.raises(RuntimeError, match="round 1"):
        kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")


def test_plan_record_round2_dry_run_writes_nothing(fdo_stores):
    afdo, prop = fdo_stores
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                        building_pkgver="x", dry_run=True)
    assert not prop.exists()


def _ready_round2(prop, pkgver="7.2.7.arch1-1"):
    prop.mkdir(parents=True, exist_ok=True)
    (prop / "kernel.afdo").write_bytes(b"A")
    (prop / "propeller_cc_profile.txt").write_text("c")
    (prop / "propeller_ld_profile.txt").write_text("l")
    kernel_fdo.write_round(prop, pkgver=pkgver, build_dir=None,
                           afdo_sha256=kernel_fdo.file_sha256(prop / "kernel.afdo"))


def test_plan_use_propeller_explicit(fdo_stores):
    _, prop = fdo_stores
    _ready_round2(prop)
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.eff_pkgname == "linux-sysforge-propeller"
    assert p.env[kernel_fdo.ENV_AUTOFDO] == str(prop / "kernel.afdo")
    assert p.build_mode == kernel_fdo.BUILD_MODE_PROPELLER


def test_plan_use_propeller_explicit_pkgver_mismatch_refuses(fdo_stores):
    _, prop = fdo_stores
    _ready_round2(prop, pkgver="7.2.6.arch1-1")
    with pytest.raises(RuntimeError, match="7.2.6.arch1-1"):
        kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")


def _rec_plan(store, sha=None):
    return kfdo.FdoPlan(mode="record", propeller=False, eff_pkgname="linux-sysforge-profiling",
                        env=None, build_mode=None, round_store=store, afdo_sha256=sha)


def test_finish_record_writes_round(fdo_stores, tmp_path):
    afdo, _ = fdo_stores
    p = _rec_plan(afdo)
    kfdo.finish_fdo_record(p, built_dir=tmp_path / "b", pkgver="7.2.7.arch1-1")
    assert kernel_fdo.read_round(afdo) == kernel_fdo.RoundInfo(
        "7.2.7.arch1-1", tmp_path / "b", None)


def test_finish_record_already_built_keeps_previous_build_dir(fdo_stores, tmp_path):
    afdo, _ = fdo_stores
    kernel_fdo.write_round(afdo, pkgver="7.2.7.arch1-1", build_dir=tmp_path / "b")
    p = _rec_plan(afdo)
    kfdo.finish_fdo_record(p, built_dir=None, pkgver="7.2.7.arch1-1")
    assert kernel_fdo.read_round(afdo).build_dir == tmp_path / "b"


def test_finish_noop_for_use(fdo_stores):
    afdo, _ = fdo_stores
    p = kfdo.FdoPlan(mode="use", propeller=False, eff_pkgname="linux-sysforge-fdo", env={},
                     build_mode="autofdo_kernel", round_store=None, afdo_sha256=None)
    kfdo.finish_fdo_record(p, built_dir=None, pkgver="x")
    assert not (afdo / "round.toml").exists()


def test_plan_building_pkgver_none_for_vcs_pkgbuild(tmp_path):
    """A pkgver() function means the static pkgver is the *previous* build's;
    built_pkgver, with no patched copy or manifest, falls back to that literal."""
    pb = make_pkgbuild(tmp_path, "linux-git")
    pb.write_text("pkgname=linux-git\npkgver=7.2.7\npkgrel=1\n"
                  "pkgver() {\n  echo 7.2.8\n}\n")
    assert kfdo.building_pkgver(pb) is None
    assert kfdo.built_pkgver(pb) == "7.2.7-1"


def test_plan_built_pkgver_static(tmp_path):
    pb = make_pkgbuild(tmp_path, "linux-git")
    assert kfdo.built_pkgver(pb) == "6.10-1"
    assert kfdo.built_pkgver(tmp_path / "missing" / "PKGBUILD") is None


def test_plan_building_pkgver_reads_pkgver_pkgrel(tmp_path):
    pb = make_pkgbuild(tmp_path, "linux-git")
    assert kfdo.building_pkgver(pb) == "6.10-1"
    assert kfdo.building_pkgver(tmp_path / "missing" / "PKGBUILD") is None
    pb.write_text("pkgname=linux-git\npkgver=${_ver}.arch1\npkgrel=1\n")
    assert kfdo.building_pkgver(pb) is None  # unresolved: skip the compare, never refuse


def test_plan_record_advisory_uses_plan_eff_pkgname(tmp_path, monkeypatch):
    plan = kfdo.FdoPlan(mode="record", propeller=False, eff_pkgname="linux-git-planned",
                        env=None, build_mode=None, round_store=None, afdo_sha256=None)
    tpl = be.Template(
        be.parse_entry("a.conf", "title A\nsort-key arch\nlinux /vmlinuz-linux\n"), "linux")
    text = _record_advisory_text(tmp_path, monkeypatch, tpl, "pre", plan=plan)
    assert "sudo bootctl set-oneshot sysforge-linux-git-planned.conf" in text


@pytest.mark.parametrize("propeller", [False, True])
def test_record_advisory_capture_command_matches_round(tmp_path, monkeypatch, propeller):
    # Round 2's record advisory must point at the round-2 capture, or the
    # operator runs round 1's capture (llvm-profgen) on the round-2 kernel.
    plan = kfdo.FdoPlan(mode="record", propeller=propeller,
                        eff_pkgname="linux-git-sysforge-profiling", env=None,
                        build_mode=None, round_store=None, afdo_sha256=None)
    tpl = be.Template(
        be.parse_entry("a.conf", "title A\nsort-key arch\nlinux /vmlinuz-linux\n"), "linux")
    text = _record_advisory_text(tmp_path, monkeypatch, tpl, "pre", plan=plan)
    if propeller:
        assert "sysforge run kernel --autofdo=capture --propeller" in text
    else:
        assert "sysforge run kernel --autofdo=capture" in text
        assert "--propeller" not in text


class _StopAtBuild(Exception):
    pass


def test_plan_record_round2_build_carries_kconfig_and_pinned_env(tmp_path, monkeypatch):
    """Round-2 record build: CONFIG_AUTOFDO_CLANG + CONFIG_PROPELLER_CLANG in the
    fragment, and CLANG_AUTOFDO_PROFILE=<pinned copy> through extra_env."""
    tcfg = {"profile_store": str(tmp_path / "store")}
    monkeypatch.setattr(kernel_fdo, "_load_tcfg", lambda: tcfg)
    afdo = kernel_fdo.resolve_store("linux-git", propeller=False, tcfg=tcfg)
    prop = kernel_fdo.resolve_store("linux-git", propeller=True, tcfg=tcfg)
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(_km.fdo, "resolve_fdo", lambda o, c=None: ("record", True, True))
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm", lambda *a, **k: None)
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("llvm", "clang", None))
    seen = {}

    def frag(*a, extra_kconfig=None, **k):
        seen["kconfig"] = extra_kconfig
        return None, 0, 0, 0, len(extra_kconfig or {})

    def build(pkgbuild, *, options):
        seen["options"] = options
        raise _StopAtBuild

    monkeypatch.setattr(_km.kconfig, "write_kconfig_fragment", frag)
    with patch.object(_km.config, "KERNEL_PATH", p), patch(_MAKEPKG_RUN, build), \
         pytest.raises(_StopAtBuild):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    assert seen["kconfig"] == {kernel_fdo.CONFIG_AUTOFDO: "y", kernel_fdo.CONFIG_PROPELLER: "y"}
    opts = seen["options"]
    assert opts.extra_env == {kernel_fdo.ENV_AUTOFDO: str(prop / "kernel.afdo")}
    assert opts.optimization_build_mode is None
    assert opts.rename_pkgbase_to == "linux-git-sysforge-profiling"


def test_plan_use_propeller_unknown_building_pkgver_warns_and_proceeds(fdo_stores, monkeypatch):
    _, prop = fdo_stores
    _ready_round2(prop)
    warns = []
    monkeypatch.setattr(kfdo, "_log", SimpleNamespace(
        warn=lambda m, *a, **k: warns.append(m), info=lambda *a, **k: None,
        ui=lambda *a, **k: None, debug=lambda *a, **k: None))
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=True,
                            building_pkgver=None)
    assert p.eff_pkgname == "linux-sysforge-propeller" and p.propeller
    assert p.profile_store == prop
    assert any("cannot verify the round-2 Propeller profile" in w for w in warns)


def test_plan_use_autofdo_carries_profile_store(fdo_stores):
    afdo, _ = fdo_stores
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.profile_store == afdo and p.eff_pkgname == "linux-sysforge-fdo"


# ---------------------------------------------------------------------------
# kernel.toml [fdo] mode — config-driven `use` (3.3.0-F6)
# ---------------------------------------------------------------------------


@pytest.fixture
def fdo_warns(monkeypatch):
    warns = []
    monkeypatch.setattr(kfdo, "_log", SimpleNamespace(
        warn=lambda m, *a, **k: warns.append(m), info=lambda *a, **k: None,
        ui=lambda *a, **k: None, debug=lambda *a, **k: None))
    return warns


def _live_afdo(afdo, pkgver=None):
    afdo.mkdir(parents=True, exist_ok=True)
    (afdo / "kernel.afdo").write_bytes(b"live")
    if pkgver:
        kernel_fdo.write_round(afdo, pkgver=pkgver, build_dir=None)


def test_config_mode_set_no_profile_refuses(fdo_stores):
    with pytest.raises(RuntimeError, match='mode = "off"') as ei:
        kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    msg = str(ei.value)
    assert "--autofdo=record" in msg and "sysforge run kernel`" in msg
    assert "--autofdo=use" not in msg


def test_explicit_use_no_profile_has_no_config_hint(fdo_stores):
    with pytest.raises(RuntimeError, match="no AutoFDO profile") as ei:
        kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert "[fdo]" not in str(ei.value)


def test_config_autofdo_minor_mismatch_warns_and_builds(fdo_stores, fdo_warns):
    afdo, _ = fdo_stores
    _live_afdo(afdo, pkgver="7.1.9.arch1-1")
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert (p.eff_pkgname, p.profile_store) == ("linux-sysforge-fdo", afdo)
    assert any("7.1" in w and "7.2" in w and "new round" in w for w in fdo_warns)


def test_config_autofdo_same_minor_silent(fdo_stores, fdo_warns):
    afdo, _ = fdo_stores
    _live_afdo(afdo, pkgver="7.2.1.arch1-1")
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert p.build_mode == kernel_fdo.BUILD_MODE_AUTOFDO
    assert fdo_warns == []


def test_config_autofdo_no_round_warns_unknown(fdo_stores, fdo_warns):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=False,
                        building_pkgver="7.2.7.arch1-1")
    assert any("collected version unknown" in w for w in fdo_warns)


def test_config_propeller_matching_round_builds_propeller(fdo_stores, fdo_warns):
    _, prop = fdo_stores
    _ready_round2(prop)
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert (p.propeller, p.eff_pkgname, p.build_mode, p.profile_store) == (
        True, "linux-sysforge-propeller", kernel_fdo.BUILD_MODE_PROPELLER, prop)
    assert fdo_warns == []


def test_config_propeller_stale_falls_back_to_autofdo(fdo_stores, fdo_warns):
    afdo, prop = fdo_stores
    _live_afdo(afdo, pkgver="7.2.7.arch1-1")
    _ready_round2(prop, pkgver="7.2.6.arch1-1")
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert (p.propeller, p.eff_pkgname, p.build_mode) == (
        False, "linux-sysforge-fdo", kernel_fdo.BUILD_MODE_AUTOFDO)
    assert p.env == {kernel_fdo.ENV_AUTOFDO: str(afdo / "kernel.afdo")}
    assert p.profile_store == afdo
    assert any("If linux-sysforge-propeller is installed" in w and "7.2.6.arch1-1" in w
               for w in fdo_warns)
    assert prop.exists()  # never auto-removes anything


def test_config_propeller_unknown_building_pkgver_falls_back(fdo_stores, fdo_warns):
    """R12: a VCS kernel's building version is unknown before makepkg runs
    pkgver(), so the round-2 profile cannot be verified: config-driven
    Propeller treats that as stale and builds AutoFDO only."""
    afdo, prop = fdo_stores
    _live_afdo(afdo, pkgver="7.2.7.arch1-1")
    _ready_round2(prop)
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver=None)
    assert (p.propeller, p.eff_pkgname, p.build_mode) == (
        False, "linux-sysforge-fdo", kernel_fdo.BUILD_MODE_AUTOFDO)
    assert p.env == {kernel_fdo.ENV_AUTOFDO: str(afdo / "kernel.afdo")}
    assert p.profile_store == afdo
    assert any("unknown" in w and "If linux-sysforge-propeller is installed" in w
               for w in fdo_warns)


def test_config_propeller_round2_never_done_refuses(fdo_stores):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    with pytest.raises(RuntimeError, match='mode = "autofdo"'):
        kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")


def test_config_propeller_fallback_without_autofdo_profile_refuses(fdo_stores):
    _, prop = fdo_stores
    _ready_round2(prop, pkgver="7.2.6.arch1-1")
    with pytest.raises(RuntimeError, match='mode = "off"'):
        kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")


def test_config_fdo_mode_drives_plain_run(tmp_path, monkeypatch):
    """A plain `run kernel` with [fdo] mode = "propeller" plans a config-driven
    (explicit=False) Propeller `use` build."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    p.write_text(p.read_text() + '[fdo]\nmode = "propeller"\n')
    state = PipelineState(tmp_path / "state")
    gate = {}
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm",
                        lambda *a, explicit=True, **k: gate.update(explicit=explicit))
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("llvm", "clang", None))
    seen = {}

    def plan(mode, propeller, pkgname, **kw):
        seen.update(mode=mode, propeller=propeller, explicit=kw["explicit"])
        raise _StopAtBuild

    monkeypatch.setattr(_km.fdo, "plan_fdo_build", plan)
    with patch.object(_km.config, "KERNEL_PATH", p), pytest.raises(_StopAtBuild):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    assert seen == {"mode": "use", "propeller": True, "explicit": False}
    assert gate == {"explicit": False}


def test_plan_record_round2_dry_run_without_round1_refuses(fdo_stores):
    with pytest.raises(RuntimeError, match="round 1"):
        kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                            building_pkgver="x", dry_run=True)


def test_plan_record_provisions_round_store(fdo_stores, monkeypatch):
    from sysforge.primitives import fs_provision
    afdo, _ = fdo_stores
    calls = []
    monkeypatch.setattr(fs_provision, "ensure_writable_dir",
                        lambda path, **k: calls.append(path) or path)
    kfdo.plan_fdo_build("record", False, "linux-sysforge", explicit=True,
                        building_pkgver="x")
    assert calls == [afdo]
    calls.clear()
    kfdo.plan_fdo_build("record", False, "linux-sysforge", explicit=True,
                        building_pkgver="x", dry_run=True)
    assert calls == []


def test_plan_record_provision_failure_warns(fdo_stores, monkeypatch):
    from sysforge.primitives import fs_provision
    warns = []
    monkeypatch.setattr(kfdo, "_log", SimpleNamespace(
        warn=lambda m, *a, **k: warns.append(m), info=lambda *a, **k: None,
        ui=lambda *a, **k: None, debug=lambda *a, **k: None))

    def boom(path, **k):
        raise fs_provision.FsProvisionError("no sudo")

    monkeypatch.setattr(fs_provision, "ensure_writable_dir", boom)
    p = kfdo.plan_fdo_build("record", False, "linux-sysforge", explicit=True,
                            building_pkgver="x")
    assert p.mode == "record"
    assert any("no sudo" in w for w in warns)


def test_plan_record_round2_pin_permission_error_is_clean_refusal(fdo_stores, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(kernel_fdo, "pin_autofdo_profile", denied)
    with pytest.raises(RuntimeError, match=r"\[KERNEL\] cannot pin the AutoFDO profile"):
        kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                            building_pkgver="x")


def test_finish_record_write_oserror_only_warns(fdo_stores, tmp_path, monkeypatch):
    afdo, _ = fdo_stores
    warns = []
    monkeypatch.setattr(kfdo, "_log", SimpleNamespace(
        warn=lambda m, *a, **k: warns.append(m), info=lambda *a, **k: None,
        ui=lambda *a, **k: None, debug=lambda *a, **k: None))

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(kernel_fdo, "write_round", denied)
    kfdo.finish_fdo_record(_rec_plan(afdo), built_dir=tmp_path / "b", pkgver="1-1")
    assert len(warns) == 1
    assert str(afdo) in warns[0] and "--autofdo=record" in warns[0]
    assert "vmlinux" in warns[0]


def test_finish_record_already_built_keeps_previous_pin_hash(fdo_stores, tmp_path):
    afdo, _ = fdo_stores
    kernel_fdo.write_round(afdo, pkgver="7.2.7.arch1-1", build_dir=tmp_path / "x",
                           afdo_sha256="aa" * 32)
    kfdo.finish_fdo_record(_rec_plan(afdo, sha="bb" * 32), built_dir=None,
                           pkgver="7.2.7.arch1-1")
    assert kernel_fdo.read_round(afdo) == kernel_fdo.RoundInfo(
        "7.2.7.arch1-1", tmp_path / "x", "aa" * 32)


def test_finish_record_already_built_without_prior_round_writes_no_hash(fdo_stores):
    afdo, _ = fdo_stores
    kfdo.finish_fdo_record(_rec_plan(afdo, sha="bb" * 32), built_dir=None,
                           pkgver="7.2.7.arch1-1")
    assert kernel_fdo.read_round(afdo).afdo_sha256 is None


def test_plan_record_stage_sidecar_failure_does_not_strand_sentinel(tmp_path, monkeypatch):
    """A round.toml write failure after a successful install warns; the stage
    completes and the install sentinel is cleared."""
    from sysforge.primitives import stage_sentinel
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(_km.fdo, "resolve_fdo", lambda o, c=None: ("record", False, True))
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm", lambda *a, **k: None)
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("llvm", "clang", None))
    monkeypatch.setattr(_km.gates, "gate3_verify", MagicMock())
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda *a, **k: None)
    monkeypatch.setattr(_km.gates, "built_kernel_release", lambda *a, **k: "7.2.7")
    monkeypatch.setattr(kinstall, "sync_boot_entries", lambda *a, **k: None)
    seen = {}

    def denied(store, **k):
        seen["store"] = store
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(kernel_fdo, "write_round", denied)
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=None), \
         patch.object(_km.stage, "install_built_packages", MagicMock(return_value=[])), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as sub:
        sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    assert "store" in seen
    assert stage_sentinel.StageSentinel(tmp_path / "state").get_active() is None


# ---------------------------------------------------------------------------
# Forced rebuilds: record always (R20), use on a changed profile (R21)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("propeller", [False, True])
def test_plan_record_always_forces_rebuild(fdo_stores, propeller):
    afdo, _ = fdo_stores
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    for dry_run in (False, True):
        p = kfdo.plan_fdo_build("record", propeller, "linux-sysforge", explicit=True,
                                building_pkgver="7.2.7.arch1-1", dry_run=dry_run)
        assert p.force_rebuild is True


def test_plan_off_does_not_force(fdo_stores):
    p = kfdo.plan_fdo_build(None, False, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is False


def test_plan_use_unchanged_profile_reuses(fdo_stores):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    kernel_fdo.write_applied(afdo, kernel_fdo.applied_fingerprint(afdo, propeller=False))
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is False
    assert p.applied_fingerprint == kernel_fdo.read_applied(afdo)


def test_plan_use_changed_profile_forces(fdo_stores):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    kernel_fdo.write_applied(afdo, kernel_fdo.applied_fingerprint(afdo, propeller=False))
    (afdo / "kernel.afdo").write_bytes(b"recaptured")
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is True
    assert p.applied_fingerprint == kernel_fdo.applied_fingerprint(afdo, propeller=False)


def test_plan_use_missing_applied_forces(fdo_stores):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    p = kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is True


def test_plan_use_propeller_cc_change_forces(fdo_stores):
    _, prop = fdo_stores
    _ready_round2(prop)
    kernel_fdo.write_applied(prop, kernel_fdo.applied_fingerprint(prop, propeller=True))
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is False
    (prop / "propeller_cc_profile.txt").write_text("c2")
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=True,
                            building_pkgver="7.2.7.arch1-1")
    assert p.force_rebuild is True


def test_plan_use_fallback_fingerprints_the_autofdo_store(fdo_stores, fdo_warns):
    afdo, prop = fdo_stores
    _live_afdo(afdo, pkgver="7.2.7.arch1-1")
    _ready_round2(prop, pkgver="7.2.6.arch1-1")
    kernel_fdo.write_applied(afdo, kernel_fdo.applied_fingerprint(afdo, propeller=False))
    p = kfdo.plan_fdo_build("use", True, "linux-sysforge", explicit=False,
                            building_pkgver="7.2.7.arch1-1")
    assert p.profile_store == afdo and p.force_rebuild is False


def test_plan_use_dry_run_writes_nothing(fdo_stores):
    afdo, _ = fdo_stores
    _live_afdo(afdo)
    before = sorted(x.name for x in afdo.iterdir())
    kfdo.plan_fdo_build("use", False, "linux-sysforge", explicit=True,
                        building_pkgver="7.2.7.arch1-1", dry_run=True)
    assert sorted(x.name for x in afdo.iterdir()) == before


def _use_plan(store, fp="cd" * 32):
    return kfdo.FdoPlan(mode="use", propeller=False, eff_pkgname="linux-sysforge-fdo",
                        env={}, build_mode="autofdo_kernel", round_store=None,
                        afdo_sha256=None, profile_store=store, applied_fingerprint=fp)


def test_finish_build_use_writes_applied(fdo_stores, tmp_path):
    afdo, _ = fdo_stores
    kfdo.finish_fdo_build(_use_plan(afdo), built_dir=tmp_path / "b", pkgver="1-1")
    assert kernel_fdo.read_applied(afdo) == "cd" * 32
    assert not (afdo / "round.toml").exists()


# 3.3.0-B16: applied.toml means "last applied", so a same-profile reuse leaves it alone.
def test_finish_build_use_unchanged_fingerprint_leaves_applied_mtime(fdo_stores, tmp_path):
    import os
    afdo, _ = fdo_stores
    path = kernel_fdo.write_applied(afdo, "cd" * 32)
    os.utime(path, (1_000_000, 1_000_000))
    kfdo.finish_fdo_build(_use_plan(afdo), built_dir=None, pkgver="1-1")
    assert path.stat().st_mtime == 1_000_000


def test_finish_build_use_changed_fingerprint_rewrites_applied(fdo_stores, tmp_path):
    import os
    afdo, _ = fdo_stores
    path = kernel_fdo.write_applied(afdo, "ab" * 32)
    os.utime(path, (1_000_000, 1_000_000))
    kfdo.finish_fdo_build(_use_plan(afdo), built_dir=None, pkgver="1-1")
    assert kernel_fdo.read_applied(afdo) == "cd" * 32
    assert path.stat().st_mtime > 1_000_000


def test_finish_build_record_writes_no_applied(fdo_stores, tmp_path):
    afdo, _ = fdo_stores
    kfdo.finish_fdo_build(_rec_plan(afdo), built_dir=tmp_path / "b", pkgver="1-1")
    assert kernel_fdo.read_round(afdo) is not None
    assert kernel_fdo.read_applied(afdo) is None


def test_finish_build_use_oserror_only_warns(fdo_stores, tmp_path, fdo_warns, monkeypatch):
    afdo, _ = fdo_stores

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(kernel_fdo, "write_applied", denied)
    kfdo.finish_fdo_build(_use_plan(afdo), built_dir=None, pkgver="1-1")
    assert len(fdo_warns) == 1 and str(afdo) in fdo_warns[0]


def _drive_build(tmp_path, monkeypatch, fdo, plan=None):
    """Run the stage until the first makepkg_run call; return its options."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(_km.fdo, "resolve_fdo", lambda o, c=None: fdo)
    if plan is not None:
        monkeypatch.setattr(_km.fdo, "plan_fdo_build", lambda *a, **k: plan)
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm", lambda *a, **k: None)
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("llvm", "clang", None))
    seen = {}

    def frag(*a, extra_kconfig=None, **k):
        seen["kconfig"] = extra_kconfig
        return None, 0, 0, 0, len(extra_kconfig or {})

    def build(pkgbuild, *, options):
        seen["options"] = options
        raise _StopAtBuild

    monkeypatch.setattr(_km.kconfig, "write_kconfig_fragment", frag)
    with patch.object(_km.config, "KERNEL_PATH", p), patch(_MAKEPKG_RUN, build), \
         pytest.raises(_StopAtBuild):
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    return seen


def test_stage_round2_record_first_build_forces(tmp_path, monkeypatch):
    tcfg = {"profile_store": str(tmp_path / "store")}
    monkeypatch.setattr(kernel_fdo, "_load_tcfg", lambda: tcfg)
    afdo = kernel_fdo.resolve_store("linux-git", propeller=False, tcfg=tcfg)
    afdo.mkdir(parents=True)
    (afdo / "kernel.afdo").write_bytes(b"A")
    seen = _drive_build(tmp_path, monkeypatch, ("record", True, True))
    assert "-f" in (seen["options"].extra_flags or [])


def test_stage_plain_first_build_does_not_force(tmp_path, monkeypatch):
    seen = _drive_build(tmp_path, monkeypatch, (None, False, False))
    assert "-f" not in (seen["options"].extra_flags or [])


def test_plan_record_round2_provisions_before_pinning(fdo_stores, monkeypatch):
    from sysforge.primitives import fs_provision
    _, prop = fdo_stores
    order = []
    monkeypatch.setattr(fs_provision, "ensure_writable_dir",
                        lambda path, **k: order.append(("provision", path)) or path)
    monkeypatch.setattr(kernel_fdo, "pin_autofdo_profile",
                        lambda *a, **k: order.append(("pin",)) or (prop / "kernel.afdo", "s"))
    kfdo.plan_fdo_build("record", True, "linux-sysforge", explicit=True,
                        building_pkgver="x")
    assert order == [("provision", prop), ("pin",)]


# ---------------------------------------------------------------------------
# Stage-level use path: Gate 1 name, build options, boot-entry role (R23)
# ---------------------------------------------------------------------------


def _drive_install(tmp_path, monkeypatch, fdo):
    """Run the stage end to end (stubbed build/install); return what the
    build, Gate 1, the kconfig fragment and the boot-entry sync received."""
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    monkeypatch.setattr(_km.fdo, "resolve_fdo", lambda o, c=None: fdo)
    monkeypatch.setattr(_km.fdo, "gate_fdo_llvm", lambda *a, **k: None)
    monkeypatch.setattr(_km.config, "resolve_compiler", lambda *a, **k: ("llvm", "clang", None))
    monkeypatch.setattr(_km.gates, "gate3_verify", MagicMock())
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda *a, **k: None)
    monkeypatch.setattr(_km.gates, "built_kernel_release", lambda *a, **k: "7.2.7")
    seen = {}
    real_gate1 = _km.gates.gate1_preflight

    def gate1(kernel_cfg, options, pkgname, **k):
        seen["gate1"] = pkgname
        return real_gate1(kernel_cfg, options, pkgname, **k)

    def frag(*a, extra_kconfig=None, **k):
        seen["kconfig"] = extra_kconfig
        return None, 0, 0, 0, len(extra_kconfig or {})

    def build(pkgbuild, *, options):
        seen["options"] = options

    monkeypatch.setattr(_km.gates, "gate1_preflight", gate1)
    monkeypatch.setattr(_km.kconfig, "write_kconfig_fragment", frag)
    monkeypatch.setattr(kinstall, "sync_boot_entries",
                        lambda pkg, eff, role, *a, **k: seen.update(role=role, eff=eff))
    with patch.object(_km.config, "KERNEL_PATH", p), patch(_MAKEPKG_RUN, build), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as sub:
        sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    return seen


def _git_stores(tmp_path, monkeypatch):
    tcfg = {"profile_store": str(tmp_path / "store")}
    monkeypatch.setattr(kernel_fdo, "_load_tcfg", lambda: tcfg)
    return (kernel_fdo.resolve_store("linux-git", propeller=False, tcfg=tcfg),
            kernel_fdo.resolve_store("linux-git", propeller=True, tcfg=tcfg))


def test_stage_gate1_checks_the_role_kernel(tmp_path, monkeypatch):
    afdo, _ = _git_stores(tmp_path, monkeypatch)
    _live_afdo(afdo)
    seen = _drive_install(tmp_path, monkeypatch, ("use", False, True))
    assert seen["gate1"] == "linux-git-sysforge-fdo"


def test_stage_explicit_use_drives_build_and_boot_entry(tmp_path, monkeypatch):
    afdo, _ = _git_stores(tmp_path, monkeypatch)
    _live_afdo(afdo)
    seen = _drive_install(tmp_path, monkeypatch, ("use", False, True))
    opts = seen["options"]
    assert opts.rename_pkgbase_to == "linux-git-sysforge-fdo"
    assert opts.optimization_build_mode == kernel_fdo.BUILD_MODE_AUTOFDO
    assert opts.extra_env == {kernel_fdo.ENV_AUTOFDO: str(afdo / "kernel.afdo")}
    assert "-f" in (opts.extra_flags or [])  # no applied.toml yet
    assert seen["kconfig"] == {kernel_fdo.CONFIG_AUTOFDO: "y"}
    assert (seen["role"], seen["eff"]) == ("autofdo", "linux-git-sysforge-fdo")
    assert kernel_fdo.read_applied(afdo) == kernel_fdo.applied_fingerprint(
        afdo, propeller=False)


def test_stage_config_propeller_fallback_builds_autofdo(tmp_path, monkeypatch):
    afdo, prop = _git_stores(tmp_path, monkeypatch)
    _live_afdo(afdo, pkgver="6.10-1")
    _ready_round2(prop, pkgver="6.9-1")  # stale against the PKGBUILD's 6.10-1
    seen = _drive_install(tmp_path, monkeypatch, ("use", True, False))
    opts = seen["options"]
    assert opts.rename_pkgbase_to == "linux-git-sysforge-fdo"
    assert opts.optimization_build_mode == kernel_fdo.BUILD_MODE_AUTOFDO
    assert seen["kconfig"] == {kernel_fdo.CONFIG_AUTOFDO: "y"}
    assert kernel_fdo.CONFIG_PROPELLER not in seen["kconfig"]
    assert (seen["role"], seen["eff"]) == ("autofdo", "linux-git-sysforge-fdo")
    assert seen["gate1"] == "linux-git-sysforge-fdo"
    assert kernel_fdo.read_applied(prop) is None


# ---------------------------------------------------------------------------
# Post-build pkgver: the file makepkg rewrote, not the stale PKGBUILD (R23)
# ---------------------------------------------------------------------------


def test_built_pkgver_prefers_rewritten_build_file(tmp_path):
    pb = make_pkgbuild(tmp_path, "linux-git")  # 6.10-1 (stale: makepkg -p the copy)
    (pb.parent / "PKGBUILD.sysforge").write_text(
        "pkgname=linux-git-sysforge-profiling\npkgver=6.11.r3.gabc\npkgrel=1\n")
    assert kfdo.built_pkgver(pb, pkgname="linux-git-sysforge-profiling") == "6.11.r3.gabc-1"


def test_built_pkgver_reads_manifest_when_build_file_cleaned_up(tmp_path):
    pb = make_pkgbuild(tmp_path, "linux-git")
    (pb.parent / ".sysforge-built.list").write_text(
        "linux-git-sysforge-profiling-6.11.r3.gabc-1-x86_64.pkg.tar.zst\n"
        "linux-git-sysforge-profiling-headers-6.11.r3.gabc-1-x86_64.pkg.tar.zst\n")
    assert kfdo.built_pkgver(pb, pkgname="linux-git-sysforge-profiling") == "6.11.r3.gabc-1"
    # Another package's manifest entry never matches.
    assert kfdo.built_pkgver(pb, pkgname="linux-git-sysforge-fdo") == "6.10-1"


def test_stage_record_round_pkgver_comes_from_the_build(tmp_path, monkeypatch):
    afdo, _ = _git_stores(tmp_path, monkeypatch)
    (tmp_path / "builds" / "linux-git").mkdir(parents=True)
    (tmp_path / "builds" / "linux-git" / ".sysforge-built.list").write_text(
        "linux-git-sysforge-profiling-6.11-2-x86_64.pkg.tar.zst\n")
    _drive_install(tmp_path, monkeypatch, ("record", False, True))
    assert kernel_fdo.read_round(afdo).pkgver == "6.11-2"


# ---------------------------------------------------------------------------
# 3.3.0-B11 — a reused (AlreadyBuilt) kernel's own config feeds Gate 2 + drift
# ---------------------------------------------------------------------------

_REL = "7.2.7-arch1-1-sysforge"


def _reuse_tree(root, release, body="CONFIG_EXT4_FS=y\nCONFIG_HZ_1000=y\n"):
    tree = root / "src" / "linux-7.2.7"
    (tree / "include" / "config").mkdir(parents=True)
    (tree / ".config").write_text(body)
    (tree / "include" / "config" / "kernel.release").write_text(release + "\n")
    return tree


def _artifact_release(monkeypatch, release):
    from sysforge.build import makepkg_wrapper
    monkeypatch.setattr(makepkg_wrapper, "built_kernel_artifact_release",
                        lambda d: release)


def test_reused_config_matching_tree_is_used(tmp_path, monkeypatch):
    _artifact_release(monkeypatch, _REL)
    tree = _reuse_tree(tmp_path / "builds" / "linux-sysforge", _REL)
    got = _km.gates.resolve_reused_config(
        tmp_path / "state", "linux-sysforge", tmp_path / "pkg",
        build_dir=tmp_path / "builds" / "linux-sysforge")
    assert got is not None and got.path == tree / ".config"
    assert got.source.startswith("build tree") and got.release == _REL
    assert got.config["CONFIG_HZ_1000"] == "y"


def test_reused_config_mismatched_tree_falls_back_to_archive(tmp_path, monkeypatch):
    from sysforge.primitives import kconfig_history
    _artifact_release(monkeypatch, _REL)
    _reuse_tree(tmp_path / "builds" / "linux-sysforge", "7.2.8-arch1-1-sysforge")
    archived = tmp_path / "archived.config"
    archived.write_text("CONFIG_EXT4_FS=y\nCONFIG_ARCHIVED=y\n")
    kconfig_history.archive(tmp_path / "state", "linux-sysforge", _REL, archived)
    got = _km.gates.resolve_reused_config(
        tmp_path / "state", "linux-sysforge", tmp_path / "pkg",
        build_dir=tmp_path / "builds" / "linux-sysforge")
    assert got is not None and got.path is None
    assert "kconfig-history" in got.source
    assert got.config["CONFIG_ARCHIVED"] == "y"


def test_reused_config_none_without_tree_or_archive(tmp_path, monkeypatch):
    _artifact_release(monkeypatch, _REL)
    _reuse_tree(tmp_path / "builds" / "linux-sysforge", "7.2.8-arch1-1-sysforge")
    assert _km.gates.resolve_reused_config(
        tmp_path / "state", "linux-sysforge", tmp_path / "pkg",
        build_dir=tmp_path / "builds" / "linux-sysforge") is None


def test_reused_config_none_when_artifact_release_unknown(tmp_path, monkeypatch):
    _artifact_release(monkeypatch, None)
    _reuse_tree(tmp_path / "builds" / "linux-sysforge", _REL)
    assert _km.gates.resolve_reused_config(
        tmp_path / "state", "linux-sysforge", tmp_path / "pkg",
        build_dir=tmp_path / "builds" / "linux-sysforge") is None


def test_gate2_audits_reused_config_names_source_no_e2(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(kernel_safety, "audit_resolved_config",
                        lambda cfg, *a, **k: seen.setdefault("cfg", cfg) and [])
    monkeypatch.setattr(kernel_safety, "running_kernel_release", lambda: _REL)
    monkeypatch.setattr(device_probe, "enumerate_devices", lambda **k: [])
    reused = _km.gates.ReusedConfig({"CONFIG_EXT4_FS": "y"}, _REL, "build tree /t")
    with _capture_logs() as logs:
        _km.gates.gate2_audit(tmp_path, None, skip_boot_audit=False, reused=reused)
    assert seen["cfg"] == {"CONFIG_EXT4_FS": "y"}
    assert any("build tree /t" in m for m in _info_messages(logs))
    assert not any("matches the running kernel" in m for m in _warn_messages(logs))
    assert not any("not found" in m for m in _warn_messages(logs))


def test_gate2_kconfig_drift_checks_reused_config(tmp_path):
    fragment = tmp_path / "sysforge.config"
    fragment.write_text("CONFIG_MZEN3=y\nCONFIG_HZ_1000=y\n")
    reused = _km.gates.ReusedConfig(
        {"CONFIG_HZ_1000": "y"}, _REL, "kconfig-history archive /a")
    with _capture_logs() as logs:
        drifts = _km.gates.gate2_kconfig_drift(tmp_path, fragment, reused=reused)
    assert drifts is not None and [d.option for d in drifts] == ["CONFIG_MZEN3"]
    assert any("kconfig-history archive /a" in m for m in _warn_messages(logs))


def test_already_built_install_resolves_reused_tree_from_exception(tmp_path):
    """The stage hands AlreadyBuilt.build_dir (the wrapper's build_root) to the
    resolver and threads the result into both checks."""
    from sysforge.primitives.makepkg_invoke import AlreadyBuilt
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    opts.non_interactive = True
    tree_root = tmp_path / "profile-builds" / "linux-git"

    def fake_makepkg(pkgbuild, *, options):
        e = AlreadyBuilt(pkgbuild)
        e.build_dir = tree_root
        raise e

    reused = _km.gates.ReusedConfig({"CONFIG_EXT4_FS": "y"}, _REL, "build tree x")
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch("sysforge.pipeline.stages.kernel.stage.makepkg_run", side_effect=fake_makepkg), \
         patch.object(_km.gates, "resolve_reused_config", return_value=reused) as res, \
         patch.object(_km.gates, "gate2_audit") as g2, \
         patch.object(_km.stage, "install_built_packages"), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)
    assert res.call_args.kwargs["build_dir"] == tree_root
    assert g2.call_args.kwargs["reused"] is reused
    assert g2.call_args.kwargs["build_dir"] is None


def test_fresh_build_never_consults_reused_config(tmp_path, monkeypatch):
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, return_value=tmp_path / "fresh"), \
         patch.object(_km.gates, "resolve_reused_config") as res, \
         patch.object(_km.gates, "gate2_audit") as g2, \
         patch.object(_km.stage, "install_built_packages"), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, make_options(state_dir=tmp_path / "state"))
    res.assert_not_called()
    assert g2.call_args.kwargs["reused"] is None


# ---------------------------------------------------------------------------
# 3.0.0-F1 — refuse a CONFIG_RUST=y build the host cannot do
# ---------------------------------------------------------------------------

def _failing_rust_check():
    from sysforge.primitives.toolchain_preflight import ToolchainCheck
    return ToolchainCheck("rust:kernel", False, "rustc 1.80.0 < 1.85.0", "rustup update", False)


def test_rust_preflight_noop_without_config_rust(tmp_path, monkeypatch):
    from sysforge.primitives import toolchain_preflight as tp
    frag = tmp_path / "sysforge.config"
    frag.write_text("CONFIG_HZ_1000=y\n# CONFIG_RUST is not set\n")
    monkeypatch.setattr(tp, "probe_rust_kernel",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed")))
    _km.gates.gate_rust_preflight(frag, tmp_path, dry_run=False)
    _km.gates.gate_rust_preflight(None, tmp_path, dry_run=False)


def test_rust_preflight_uses_cached_tree_minimums(tmp_path, monkeypatch):
    from sysforge.primitives import toolchain_preflight as tp
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts" / "min-tool-version.sh").write_text(
        "rustc)\n\techo 1.85.0\n\t;;\nbindgen)\n\techo 0.71.1\n\t;;\n")
    _km.gates.cache_rust_minimums(tmp_path, tree, "7.2.7-arch1-1")
    seen = {}

    def ok(mins, source):
        seen.update(mins=mins, source=source)
        return tp.ToolchainCheck("rust:kernel", True, "fine", None, False)
    monkeypatch.setattr(tp, "probe_rust_kernel", ok)
    frag = tmp_path / "sysforge.config"
    frag.write_text("CONFIG_RUST=y\n")
    _km.gates.gate_rust_preflight(frag, tmp_path, dry_run=False)
    assert seen == {"mins": {"rustc": "1.85.0", "bindgen": "0.71.1"},
                    "source": "the 7.2.7-arch1-1 kernel tree"}


def test_rust_preflight_dry_run_reports_without_raising(tmp_path, monkeypatch):
    from sysforge.primitives import toolchain_preflight as tp
    monkeypatch.setattr(tp, "probe_rust_kernel", lambda *a, **k: _failing_rust_check())
    frag = tmp_path / "sysforge.config"
    frag.write_text("CONFIG_RUST=y\n")
    with _capture_logs() as logs:
        _km.gates.gate_rust_preflight(frag, tmp_path, dry_run=True)
    assert any("would abort" in str(c) for c in logs.ui.call_args_list)


@pytest.mark.parametrize("compiler", ["gcc", "llvm"])
def test_rust_preflight_refuses_before_build_on_both_toolchains(tmp_path, monkeypatch, compiler):
    """Dual-toolchain parity: CONFIG_RUST is orthogonal to LLVM=1, so the
    refusal is identical on the gcc and llvm kernel paths."""
    from sysforge.primitives import toolchain_preflight as tp
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds, kconfig=[{"option": "CONFIG_RUST", "value": "y"}])
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    opts.compiler = compiler
    monkeypatch.setattr(tp, "probe_rust_kernel", lambda *a, **k: _failing_rust_check())
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN) as mock_build, \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        with pytest.raises(RuntimeError, match="CONFIG_RUST=y"):
            KernelStage().run({}, state, opts)
    mock_build.assert_not_called()


def test_gate2_caches_tree_rust_minimums(tmp_path, monkeypatch):
    """Gate 2 harvests the built tree's minimums for the next run's preflight."""
    tree = tmp_path / "b" / "src" / "linux-7.2.7"
    (tree / "scripts").mkdir(parents=True)
    (tree / "include" / "config").mkdir(parents=True)
    (tree / ".config").write_text("CONFIG_EXT4_FS=y\n")
    (tree / "include" / "config" / "kernel.release").write_text("7.2.7-sf\n")
    (tree / "scripts" / "min-tool-version.sh").write_text(
        "rustc)\n\techo 1.85.0\n\t;;\nbindgen)\n\techo 0.71.1\n\t;;\n")
    monkeypatch.setattr(kernel_safety, "audit_resolved_config", lambda *a, **k: [])
    monkeypatch.setattr(device_probe, "enumerate_devices", lambda **k: [])
    monkeypatch.setattr(_km.gates, "resolve_built_config", lambda d, **kw: tree / ".config")
    state = tmp_path / "state"
    state.mkdir()
    _km.gates.gate2_audit(tmp_path, None, skip_boot_audit=False, state_dir=state,
                          build_dir=tmp_path / "b")
    assert _km.gates.load_rust_minimums(state) == (
        {"rustc": "1.85.0", "bindgen": "0.71.1"}, "the 7.2.7-sf kernel tree")



# 3.4.0-F1 — makepkg never escalates on the kernel path either
@pytest.mark.parametrize("compiler", ["gcc", "llvm"])
def test_kernel_build_strips_syncdeps_and_installs_deps_first(tmp_path, monkeypatch, compiler):
    """Dual-toolchain parity: the profile's --syncdeps never reaches makepkg;
    sysforge installs missing repo deps first, excluding the kernel itself."""
    from sysforge import build_core as _bc
    from sysforge.primitives.makepkg_flags import SYNC_FLAGS
    builds = tmp_path / "builds"
    make_pkgbuild(builds, "linux-git")
    p = make_kernel_toml(tmp_path, builds)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path / "state")
    opts.compiler = compiler
    order = []
    monkeypatch.setattr(_bc, "install_missing_repo_deps",
                        lambda paths, **k: order.append(("deps", k["exclude"])))
    with patch.object(_km.config, "KERNEL_PATH", p), \
         patch(_MAKEPKG_RUN, side_effect=lambda pb, options: order.append(
             ("build", options.strip_flags))), \
         patch("sysforge.pipeline.stages.kernel.install.subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        KernelStage().run({}, state, opts)
    (dk, exclude), (bk, strip) = order[:2]
    assert (dk, bk) == ("deps", "build")
    assert "linux-git" in exclude
    assert set(SYNC_FLAGS) <= set(strip)
