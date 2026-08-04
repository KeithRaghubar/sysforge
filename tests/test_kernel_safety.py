"""
test_kernel_safety.py — unit tests for the kernel-stage boot guardrails.
Filesystem inputs are routed through fixture trees via module-level path
constants; subprocess calls (lsblk / dkms) are monkeypatched at _run.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import kernel_safety as ks
from sysforge.primitives.device_probe import Device


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


# Minimal resolved .config with everything boot-critical enabled, for an
# ext4-on-nvme box. Tests drop individual symbols to assert the audit fires.
def _good_config(**overrides):
    cfg = {
        "CONFIG_MODULES": "y", "CONFIG_BLK_DEV_INITRD": "y",
        "CONFIG_DEVTMPFS": "y", "CONFIG_TMPFS": "y", "CONFIG_PROC_FS": "y",
        "CONFIG_SYSFS": "y", "CONFIG_BINFMT_ELF": "y", "CONFIG_UNIX": "y",
        "CONFIG_CGROUPS": "y", "CONFIG_INOTIFY_USER": "y", "CONFIG_EPOLL": "y",
        "CONFIG_SIGNALFD": "y", "CONFIG_TIMERFD": "y", "CONFIG_NET": "y",
        "CONFIG_TTY": "y", "CONFIG_VT": "y",
        "CONFIG_EXT4_FS": "y", "CONFIG_BLK_DEV_NVME": "y",
    }
    cfg.update(overrides)
    return cfg


_EXT4_NVME = ks.RootTopology(root_fstype="ext4", transports=("nvme",))


# ---------------------------------------------------------------------------
# parse_kconfig
# ---------------------------------------------------------------------------

def test_parse_kconfig_text():
    text = 'CONFIG_A=y\nCONFIG_B=m\n# CONFIG_C is not set\nCONFIG_S="str"\n'
    out = ks.parse_kconfig_text(text)
    assert out == {"CONFIG_A": "y", "CONFIG_B": "m", "CONFIG_C": "n",
                   "CONFIG_S": '"str"'}


def test_parse_kconfig_file(tmp_path):
    p = tmp_path / ".config"
    p.write_text("CONFIG_X=y\n# CONFIG_Y is not set\n")
    assert ks.parse_kconfig(p) == {"CONFIG_X": "y", "CONFIG_Y": "n"}


def test_parse_kconfig_missing_returns_none(tmp_path):
    assert ks.parse_kconfig(tmp_path / "nope") is None


def test_is_enabled():
    cfg = {"CONFIG_A": "y", "CONFIG_B": "m", "CONFIG_C": "n"}
    assert ks.is_enabled(cfg, "CONFIG_A")
    assert ks.is_enabled(cfg, "CONFIG_B")
    assert not ks.is_enabled(cfg, "CONFIG_C")
    assert not ks.is_enabled(cfg, "CONFIG_MISSING")


# ---------------------------------------------------------------------------
# diff_requested_kconfig — fragment intent vs resolved .config
# ---------------------------------------------------------------------------

def test_diff_kconfig_exact_match_no_drift():
    req = {"CONFIG_A": "y", "CONFIG_B": "m", "CONFIG_S": '"str"'}
    assert ks.diff_requested_kconfig(req, dict(req)) == []


def test_diff_kconfig_disabled():
    drifts = ks.diff_requested_kconfig({"CONFIG_A": "y"}, {"CONFIG_A": "n"})
    assert len(drifts) == 1
    assert (drifts[0].option, drifts[0].requested, drifts[0].resolved,
            drifts[0].kind) == ("CONFIG_A", "y", "n", "disabled")


def test_diff_kconfig_absent_resolves_to_n_is_disabled():
    # requested y, option missing from resolved .config → treated as n (disabled)
    drifts = ks.diff_requested_kconfig({"CONFIG_A": "m"}, {})
    assert [d.kind for d in drifts] == ["disabled"]
    assert drifts[0].resolved == "n"


def test_diff_kconfig_downgrade_and_upgrade_are_changed():
    drifts = ks.diff_requested_kconfig(
        {"CONFIG_A": "y", "CONFIG_B": "m"},
        {"CONFIG_A": "m", "CONFIG_B": "y"},
    )
    assert [d.kind for d in drifts] == ["changed", "changed"]


def test_diff_kconfig_re_enabled():
    drifts = ks.diff_requested_kconfig({"CONFIG_A": "n"}, {"CONFIG_A": "y"})
    assert [d.kind for d in drifts] == ["re-enabled"]


def test_diff_kconfig_requested_n_absent_is_no_drift():
    # requested off, resolved doesn't mention it → both off, no drift
    assert ks.diff_requested_kconfig({"CONFIG_A": "n"}, {}) == []


def test_diff_kconfig_string_value_change_is_changed():
    drifts = ks.diff_requested_kconfig(
        {"CONFIG_CMDLINE": '"quiet"'}, {"CONFIG_CMDLINE": '"verbose"'},
    )
    assert [d.kind for d in drifts] == ["changed"]


def test_diff_kconfig_ignores_options_not_requested():
    # an option only in the resolved config is not sysforge's intent → ignored
    assert ks.diff_requested_kconfig({"CONFIG_A": "y"},
                                     {"CONFIG_A": "y", "CONFIG_EXTRA": "y"}) == []


# ---------------------------------------------------------------------------
# audit_resolved_config — boot-critical
# ---------------------------------------------------------------------------

def test_audit_clean_config_no_findings():
    findings = ks.audit_resolved_config(_good_config(), _EXT4_NVME)
    assert findings == []


def test_audit_root_fs_dropped_is_brick():
    cfg = _good_config()
    cfg["CONFIG_EXT4_FS"] = "n"
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME)
    bricks = [f for f in findings if f.is_brick]
    assert any(f.check_id == "boot_kconfig:CONFIG_EXT4_FS" for f in bricks)
    assert bricks[0].severity == ks.SEV_ERROR


def test_audit_nvme_controller_dropped_is_brick():
    cfg = _good_config()
    del cfg["CONFIG_BLK_DEV_NVME"]
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME)
    assert any(f.check_id == "boot_kconfig:CONFIG_BLK_DEV_NVME" and f.is_brick
               for f in findings)


def test_audit_core_infra_dropped_is_brick():
    cfg = _good_config()
    cfg["CONFIG_MODULES"] = "n"
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME)
    assert any(f.check_id == "boot_kconfig:CONFIG_MODULES" and f.is_brick
               for f in findings)


def test_audit_console_dropped_is_degraded_not_brick():
    cfg = _good_config()
    cfg["CONFIG_VT"] = "n"
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME)
    vt = [f for f in findings if f.check_id == "boot_kconfig:CONFIG_VT"]
    assert vt and vt[0].is_brick is False and vt[0].severity == ks.SEV_WARN


def test_audit_crypt_root_requires_dm_crypt():
    topo = ks.RootTopology(root_fstype="ext4", transports=("nvme",),
                           uses_crypt=True)
    cfg = _good_config()  # no DM_CRYPT
    findings = ks.audit_resolved_config(cfg, topo)
    assert any(f.check_id == "boot_kconfig:CONFIG_DM_CRYPT" and f.is_brick
               for f in findings)


def test_audit_module_value_satisfies_requirement():
    cfg = _good_config(CONFIG_EXT4_FS="m")
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME)
    assert not any("CONFIG_EXT4_FS" in f.check_id for f in findings)


def test_audit_unreadable_config_warns():
    findings = ks.audit_resolved_config("/nonexistent/.config", _EXT4_NVME)
    assert len(findings) == 1
    assert findings[0].check_id == "kconfig_unreadable"


# ---------------------------------------------------------------------------
# audit_resolved_config — device drivers (A1/A3)
# ---------------------------------------------------------------------------

def _audio_dev():
    return Device(
        bus="pci", address="0000:0d:00.4",
        modalias="pci:v00001022d00001487", class_id="0x040300",
        description="AMD HD Audio", driver=None,
        expected_modules=["snd_hda_intel"],
        suggested_kconfig=["CONFIG_SND_HDA_INTEL"],
    )


def test_audit_device_driver_missing_is_advisory():
    cfg = _good_config()  # SND_HDA_INTEL absent
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME, devices=[_audio_dev()])
    snd = [f for f in findings if f.check_id == "device_kconfig:CONFIG_SND_HDA_INTEL"]
    assert snd and snd[0].is_brick is False
    assert "0000:0d:00.4" in snd[0].message


def test_audit_device_driver_present_no_finding():
    cfg = _good_config(CONFIG_SND_HDA_INTEL="m")
    findings = ks.audit_resolved_config(cfg, _EXT4_NVME, devices=[_audio_dev()])
    assert not any("SND_HDA_INTEL" in f.check_id for f in findings)


# ---------------------------------------------------------------------------
# detect_root_topology
# ---------------------------------------------------------------------------

def test_detect_topology_ext4_nvme(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n")
    monkeypatch.setattr(ks, "_PROC_MOUNTS", mounts)
    monkeypatch.setattr(ks, "_CRYPTTAB", tmp_path / "none")
    monkeypatch.setattr(ks, "_MDSTAT", tmp_path / "none")
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(
        "nvme0n1p2 part\nnvme0n1 disk\n") if cmd[0] == "lsblk" else None)
    topo = ks.detect_root_topology()
    assert topo.root_fstype == "ext4"
    assert "nvme" in topo.transports
    assert not topo.uses_crypt


def test_detect_topology_crypt_from_crypttab(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/mapper/root / ext4 rw 0 0\n")
    crypttab = tmp_path / "crypttab"
    crypttab.write_text("# header\ncryptroot UUID=abc none luks\n")
    monkeypatch.setattr(ks, "_PROC_MOUNTS", mounts)
    monkeypatch.setattr(ks, "_CRYPTTAB", crypttab)
    monkeypatch.setattr(ks, "_MDSTAT", tmp_path / "none")
    monkeypatch.setattr(ks, "_run", lambda cmd: None)  # no lsblk
    topo = ks.detect_root_topology()
    assert topo.uses_crypt


# ---------------------------------------------------------------------------
# find_fallback_kernels
# ---------------------------------------------------------------------------

def test_find_fallback_present(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-linux").write_text("x")
    (boot / "initramfs-linux.img").write_text("x")
    (boot / "vmlinuz-linux-custom").write_text("x")
    (boot / "initramfs-linux-custom.img").write_text("x")
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    fb = ks.find_fallback_kernels(exclude_pkg="linux-custom")
    assert fb == ["linux"]


def test_find_fallback_none_when_only_custom(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-linux-custom").write_text("x")
    (boot / "initramfs-linux-custom.img").write_text("x")
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    assert ks.find_fallback_kernels(exclude_pkg="linux-custom") == []


def test_find_fallback_requires_initramfs(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-linux").write_text("x")  # no initramfs
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    assert ks.find_fallback_kernels(exclude_pkg="linux-custom") == []


# ---------------------------------------------------------------------------
# verify_boot_artifacts
# ---------------------------------------------------------------------------

def _make_bootable(boot: Path, pkgname: str, *, entry=True):
    big = "z" * (ks._MIN_IMAGE_BYTES + 1)
    (boot / f"vmlinuz-{pkgname}").write_text(big)
    (boot / f"initramfs-{pkgname}.img").write_text(big)
    (boot / f"initramfs-{pkgname}-fallback.img").write_text(big)
    if entry:
        entries = boot / "loader" / "entries"
        entries.mkdir(parents=True)
        (entries / f"{pkgname}.conf").write_text(
            f"title {pkgname}\nlinux /vmlinuz-{pkgname}\n")


def test_verify_boot_artifacts_clean(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    _make_bootable(boot, "linux-custom")
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    assert ks.verify_boot_artifacts("linux-custom") == []


def test_verify_boot_artifacts_missing_vmlinuz_is_brick(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    _make_bootable(boot, "linux-custom")
    (boot / "vmlinuz-linux-custom").unlink()
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    findings = ks.verify_boot_artifacts("linux-custom")
    assert any(f.check_id == "boot_vmlinuz_missing" and f.is_brick
               for f in findings)


def test_verify_boot_artifacts_no_entry_is_brick(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    _make_bootable(boot, "linux-custom", entry=False)
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    findings = ks.verify_boot_artifacts("linux-custom")
    assert any(f.check_id == "boot_entry_missing" and f.is_brick
               for f in findings)


def test_verify_boot_artifacts_none_bootloader_skips_entry(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    _make_bootable(boot, "linux-custom", entry=False)
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    findings = ks.verify_boot_artifacts("linux-custom", bootloader="none")
    assert not any(f.check_id == "boot_entry_missing" for f in findings)


# ---------------------------------------------------------------------------
# check_dkms_for_kernel
# ---------------------------------------------------------------------------

def test_dkms_module_not_built_for_kernel(monkeypatch):
    out = "nvidia/570.86.16, 7.0.9-arch1-1, x86_64: installed\n"
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(out))
    findings = ks.check_dkms_for_kernel("7.0.10-custom")
    assert any(f.check_id == "dkms:nvidia" for f in findings)


def test_dkms_module_built_for_kernel(monkeypatch):
    out = "nvidia/570.86.16, 7.0.10-custom, x86_64: installed\n"
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(out))
    assert ks.check_dkms_for_kernel("7.0.10-custom") == []


def test_dkms_absent_no_findings(monkeypatch):
    monkeypatch.setattr(ks, "_run", lambda cmd: None)
    assert ks.check_dkms_for_kernel("7.0.10-custom") == []


def test_dkms_built_state_with_module_in_tree_ok(monkeypatch, tmp_path):
    # Newer dkms can report a loaded, working module as "built" (not
    # "installed") for the running kernel; if the .ko is actually in the
    # kernel tree it will load, so no finding (B9 false-positive regression).
    out = "nvidia/610.43.02, 7.0.10-custom, x86_64: built\n"
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(out))
    dkms_dir = tmp_path / "7.0.10-custom" / "updates" / "dkms"
    dkms_dir.mkdir(parents=True)
    (dkms_dir / "nvidia.ko.zst").write_bytes(b"")
    monkeypatch.setattr(ks, "_MODULES_DIR", tmp_path)
    assert ks.check_dkms_for_kernel("7.0.10-custom") == []


def test_dkms_built_state_without_module_in_tree_flags(monkeypatch, tmp_path):
    # "built" but the .ko is genuinely absent from the kernel tree → it won't
    # load, so the finding must still fire.
    out = "nvidia/610.43.02, 7.0.10-custom, x86_64: built\n"
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(out))
    monkeypatch.setattr(ks, "_MODULES_DIR", tmp_path)
    findings = ks.check_dkms_for_kernel("7.0.10-custom")
    assert any(f.check_id == "dkms:nvidia" for f in findings)


# ---------------------------------------------------------------------------
# list_dkms_modules / check_mkinitcpio_hooks
# ---------------------------------------------------------------------------

def test_list_dkms_modules(monkeypatch):
    out = ("nvidia/570.86.16, 7.0.10-arch1-1, x86_64: installed\n"
           "nvidia/570.86.16, 7.0.9-arch1-1, x86_64: installed\n"
           "vboxhost/7.1.4: added\n")
    monkeypatch.setattr(ks, "_run", lambda cmd: _completed(out))
    assert ks.list_dkms_modules() == ["nvidia", "vboxhost"]


def test_list_dkms_modules_absent(monkeypatch):
    monkeypatch.setattr(ks, "_run", lambda cmd: None)
    assert ks.list_dkms_modules() == []


def test_mkinitcpio_hooks_missing_encrypt_warns(tmp_path, monkeypatch):
    conf = tmp_path / "mkinitcpio.conf"
    conf.write_text("HOOKS=(base udev autodetect block filesystems fsck)\n")
    monkeypatch.setattr(ks, "_MKINITCPIO_CONF", conf)
    topo = ks.RootTopology(uses_crypt=True)
    findings = ks.check_mkinitcpio_hooks(topo)
    assert any(f.check_id == "mkinitcpio_hook:encryption" and not f.is_brick
               for f in findings)


def test_mkinitcpio_hooks_present_no_finding(tmp_path, monkeypatch):
    conf = tmp_path / "mkinitcpio.conf"
    conf.write_text("HOOKS=(base udev autodetect encrypt lvm2 filesystems)\n")
    monkeypatch.setattr(ks, "_MKINITCPIO_CONF", conf)
    topo = ks.RootTopology(uses_crypt=True, uses_lvm=True)
    assert ks.check_mkinitcpio_hooks(topo) == []


def test_mkinitcpio_hooks_no_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_MKINITCPIO_CONF", tmp_path / "none")
    assert ks.check_mkinitcpio_hooks(ks.RootTopology(uses_crypt=True)) == []


# ---------------------------------------------------------------------------
# check_boot_mount_space
# ---------------------------------------------------------------------------

def test_boot_space_ok(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    # tmp_path almost certainly has > 1 MiB free
    assert ks.check_boot_mount_space(min_mb=1) is None


def test_boot_dir_missing_is_brick(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "_BOOT_DIR", tmp_path / "nope")
    f = ks.check_boot_mount_space()
    assert f is not None and f.check_id == "boot_dir_missing" and f.is_brick


def test_boot_low_space_is_brick(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    boot.mkdir()
    monkeypatch.setattr(ks, "_BOOT_DIR", boot)
    monkeypatch.setattr(ks, "check_boot_mount_space",
                        ks.check_boot_mount_space)  # keep real fn
    f = ks.check_boot_mount_space(min_mb=10**12)  # absurd requirement
    assert f is not None and f.check_id == "boot_low_space" and f.is_brick


# ---------------------------------------------------------------------------
# 2.6.1-F25 — build-to-build kconfig diff
# ---------------------------------------------------------------------------


def test_diff_kconfig_classifies_added_removed_and_changed():
    from sysforge.primitives.kernel_safety import diff_kconfig

    old = {"CONFIG_SMP": "y", "CONFIG_NUMA": "y", "CONFIG_OLD": "m"}
    new = {"CONFIG_SMP": "y", "CONFIG_NUMA": "n", "CONFIG_NEW": "y"}
    changes = {c.option: c for c in diff_kconfig(old, new)}

    assert "CONFIG_SMP" not in changes          # unchanged
    assert changes["CONFIG_NUMA"].kind == "changed"
    assert (changes["CONFIG_NUMA"].old, changes["CONFIG_NUMA"].new) == ("y", "n")
    assert changes["CONFIG_NEW"].kind == "added"
    assert changes["CONFIG_OLD"].kind == "removed"


def test_diff_kconfig_does_not_normalize_absent_to_n():
    """Absent and explicitly-off are different facts on the build-to-build axis.

    diff_requested_kconfig normalizes a missing option to "n" because that is
    correct kernel semantics for sysforge's own intent. Doing it here would
    fabricate thousands of n → n non-changes on a major version bump.
    """
    from sysforge.primitives.kernel_safety import diff_kconfig

    changes = diff_kconfig({"CONFIG_GONE": "n"}, {})
    assert len(changes) == 1
    assert changes[0].kind == "removed"
    assert changes[0].new == ""


def test_diff_kconfig_is_sorted_and_empty_when_identical():
    from sysforge.primitives.kernel_safety import diff_kconfig

    assert diff_kconfig({"CONFIG_A": "y"}, {"CONFIG_A": "y"}) == []
    changes = diff_kconfig({}, {"CONFIG_Z": "y", "CONFIG_A": "y"})
    assert [c.option for c in changes] == ["CONFIG_A", "CONFIG_Z"]
