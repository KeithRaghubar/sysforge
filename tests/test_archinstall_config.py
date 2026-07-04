import pytest

from sysforge.pipeline.stages._bootstrap import BootstrapConfig
from sysforge.primitives.archinstall_config import (
    build_archinstall_config, ARCHINSTALL_SCHEMA_VERSION,
)

# Mirror the VM fixture's 40 GiB virtual disk.
_DISK = 40 * 1024**3


def _vm_cfg() -> BootstrapConfig:
    # Mirror the inputs the VM fixture was generated from.
    return BootstrapConfig(
        target="/mnt", device="/dev/vda", hostname="sysforge-vm",
        locale="en_US.UTF-8", timezone="UTC", esp_size_mib=1023,
        root_fs="ext4", keymap="us", mirror_countries=["Canada"],
        root_password="root", username="builder", user_password="builder",
        shell="bash",
    )


def _build(cfg, disk_size_bytes=_DISK):
    return build_archinstall_config(cfg, disk_size_bytes=disk_size_bytes)

def test_schema_version_pinned():
    assert ARCHINSTALL_SCHEMA_VERSION == "3.0.15"

def test_disk_layout_wipes_and_sets_fs():
    cfg = _build(_vm_cfg())
    dev = cfg["disk_config"]["device_modifications"][0]
    assert dev["device"] == "/dev/vda"
    assert dev["wipe"] is True
    parts = {p["obj_id"]: p for p in dev["partitions"]}
    assert parts["esp"]["fs_type"] == "fat32"
    assert parts["esp"]["mountpoint"] == "/boot"
    assert parts["esp"]["size"]["value"] == 1023
    assert parts["root"]["fs_type"] == "ext4"
    assert parts["root"]["mountpoint"] == "/"


def test_root_partition_fills_disk_with_concrete_size():
    # archinstall has no fill sentinel: the root size must be a concrete,
    # non-zero value derived from the disk. 40 GiB disk, 1 MiB head + 1023 MiB
    # ESP + 1 MiB GPT tail => 40*1024 - 1 - 1023 - 1 MiB.
    cfg = _build(_vm_cfg())
    root = next(p for p in cfg["disk_config"]["device_modifications"][0]["partitions"]
                if p["obj_id"] == "root")
    assert root["size"]["unit"] == "MiB"
    expected = 40 * 1024 - 1 - 1023 - 1
    assert root["size"]["value"] == expected
    assert root["size"]["value"] > 0  # never zero-length (the parted crash)
    # root starts right after the 1 MiB head + ESP
    assert root["start"]["value"] == 1 + 1023


def test_disk_too_small_raises():
    cfg = _vm_cfg()  # esp 1023 MiB
    with pytest.raises(ValueError, match="too small"):
        _build(cfg, disk_size_bytes=1024 * 1024**2)  # 1 GiB < head + ESP


def test_identity_and_users():
    cfg = _build(_vm_cfg())
    assert cfg["hostname"] == "sysforge-vm"
    assert cfg["timezone"] == "UTC"
    assert cfg["locale_config"]["kb_layout"] == "us"
    assert cfg["locale_config"]["sys_lang"] == "en_US"
    assert cfg["!root-password"] == "root"
    assert cfg["users"][0] == {
        "username": "builder", "!password": "builder", "sudo": True, "groups": [],
    }
    assert "sshd" in cfg["services"] and "NetworkManager" in cfg["services"]
    assert cfg["version"] == "3.0.15"

def test_btrfs_root_fs():
    c = _vm_cfg()
    c.root_fs = "btrfs"
    cfg = _build(c)
    root = next(p for p in cfg["disk_config"]["device_modifications"][0]["partitions"]
                if p["obj_id"] == "root")
    assert root["fs_type"] == "btrfs"

def test_zsh_adds_shell_packages():
    c = _vm_cfg()
    c.shell = "zsh"
    cfg = _build(c)
    assert "zsh" in cfg["packages"] and "zsh-completions" in cfg["packages"]
    assert any("chsh" in cc and "zsh" in cc for cc in cfg["custom_commands"])
