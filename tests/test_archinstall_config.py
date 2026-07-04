from sysforge.pipeline.stages._bootstrap import BootstrapConfig
from sysforge.primitives.archinstall_config import (
    build_archinstall_config, ARCHINSTALL_SCHEMA_VERSION,
)

def _vm_cfg() -> BootstrapConfig:
    # Mirror the inputs the VM fixture was generated from.
    return BootstrapConfig(
        target="/mnt", device="/dev/vda", hostname="sysforge-vm",
        locale="en_US.UTF-8", timezone="UTC", esp_size_mib=1023,
        root_fs="ext4", keymap="us", mirror_countries=["Canada"],
        root_password="root", username="builder", user_password="builder",
        shell="bash",
    )

def test_schema_version_pinned():
    assert ARCHINSTALL_SCHEMA_VERSION == "3.0.15"

def test_disk_layout_wipes_and_sets_fs():
    cfg = build_archinstall_config(_vm_cfg())
    dev = cfg["disk_config"]["device_modifications"][0]
    assert dev["device"] == "/dev/vda"
    assert dev["wipe"] is True
    parts = {p["obj_id"]: p for p in dev["partitions"]}
    assert parts["esp"]["fs_type"] == "fat32"
    assert parts["esp"]["mountpoint"] == "/boot"
    assert parts["esp"]["size"]["value"] == 1023
    assert parts["root"]["fs_type"] == "ext4"
    assert parts["root"]["mountpoint"] == "/"

def test_identity_and_users():
    cfg = build_archinstall_config(_vm_cfg())
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
    cfg = build_archinstall_config(c)
    root = next(p for p in cfg["disk_config"]["device_modifications"][0]["partitions"]
                if p["obj_id"] == "root")
    assert root["fs_type"] == "btrfs"

def test_zsh_adds_shell_packages():
    c = _vm_cfg()
    c.shell = "zsh"
    cfg = build_archinstall_config(c)
    assert "zsh" in cfg["packages"] and "zsh-completions" in cfg["packages"]
    assert any("chsh" in cc and "zsh" in cc for cc in cfg["custom_commands"])
