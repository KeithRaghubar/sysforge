# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""primitives/archinstall_config.py — pure BootstrapConfig -> archinstall JSON.

No archinstall import: emits the versioned headless config schema
(``archinstall --config <file> --silent``). The VM fixture
``tools/vm/archinstall-config.json`` is the golden source of truth.
"""
from sysforge.pipeline.stages._bootstrap import BootstrapConfig

ARCHINSTALL_SCHEMA_VERSION = "3.0.15"

# Minimal packages installed via pacstrap.
# - base-devel:       build toolchain meta-package (make, gcc, fakeroot, binutils, etc.); required for makepkg
# - base:             core userspace (glibc, bash, coreutils, systemd, pacman, ...)
# - devtools:         provides pkgctl; required to clone repo PKGBUILDs in `sysforge build/update`
# - git:              required for cloning PKGBUILDs and sysforge itself
# - linux-firmware:   hardware firmware blobs
# - linux:            default Arch kernel (replaced by custom kernel stage if configured)
# - networkmanager:   network management daemon (needed for post-boot connectivity)
# - openssh:          SSH server/client (required for remote access)
# - python:           required by sysforge itself
# - reflector:        mirror ranking tool; run during configure stage to select fastest pacman mirrors
# - sudo:             privilege escalation for the build user
# - uv:               Python package installer (required by sysforge PKGBUILD)
_BASE_PACKAGES = [
    "base",
    "base-devel",
    "bash-completion",
    "devtools",
    "git",
    "linux",
    "linux-firmware",
    "networkmanager",
    "openssh",
    "python",
    "reflector",
    "sudo",
    "uv",
]

_SSHD_CUSTOM_COMMANDS = [
    "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config",
    "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config",
    "sed -i '/^options /{/console=ttyS0/!s/$/ console=tty0 console=ttyS0,115200/}' /boot/loader/entries/*.conf",
]


def _sector() -> dict:
    return {"sector_size": {"unit": "B", "value": 512}, "total_size": None}


def _partitions(cfg: BootstrapConfig) -> list[dict]:
    esp = {
        "btrfs": [], "dev_path": None, "flags": ["Boot", "ESP"], "fs_type": "fat32",
        "mount_options": [], "mountpoint": "/boot", "obj_id": "esp",
        "size": {**_sector(), "unit": "MiB", "value": cfg.esp_size_mib},
        "start": {**_sector(), "unit": "MiB", "value": 1},
        "status": "create", "type": "primary",
    }
    root = {
        "btrfs": [], "dev_path": None, "flags": [], "fs_type": cfg.root_fs,
        "mount_options": [], "mountpoint": "/", "obj_id": "root",
        # start after the ESP; total_size None => archinstall fills remaining space
        "size": {**_sector(), "unit": "B", "value": 0},
        "start": {**_sector(), "unit": "MiB", "value": 1 + cfg.esp_size_mib},
        "status": "create", "type": "primary",
    }
    return [esp, root]


def build_archinstall_config(cfg: BootstrapConfig) -> dict:
    packages = list(_BASE_PACKAGES)
    custom = list(_SSHD_CUSTOM_COMMANDS)
    if cfg.shell == "zsh":
        packages += ["zsh", "zsh-completions"]
        custom.append(f"chsh -s /usr/bin/zsh {cfg.username}")

    lang = cfg.locale.split(".")[0]
    enc = cfg.locale.split(".")[1] if "." in cfg.locale else "UTF-8"
    users = [{
        "username": cfg.username, "!password": cfg.user_password or "",
        "sudo": True, "groups": [],
    }]
    return {
        "additional-repositories": [],
        "bootloader_config": {"bootloader": "Systemd-boot", "uki": False, "removable": True},
        "debug": False,
        "disk_config": {
            "config_type": "default_layout",
            "device_modifications": [
                {"device": cfg.device, "partitions": _partitions(cfg), "wipe": True},
            ],
        },
        "hostname": cfg.hostname,
        "locale_config": {"kb_layout": cfg.keymap, "sys_enc": enc, "sys_lang": lang},
        "mirror_config": {"mirror_regions": {c: [] for c in cfg.mirror_countries}},
        "network_config": {"type": "nm"},
        "ntp": True,
        "packages": packages,
        "profile_config": None,
        "!root-password": cfg.root_password or "",
        "users": users,
        "custom_commands": custom,
        "script": "guided",
        "services": ["sshd", "NetworkManager"],
        "swap": False,
        "timezone": cfg.timezone,
        "version": ARCHINSTALL_SCHEMA_VERSION,
    }
