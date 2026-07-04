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

_MIB = 1024 * 1024
# Tail left unallocated after the root partition. A GPT keeps a secondary
# header + partition-entry array (~17 KiB) at the end of the disk, and parted
# aligns partition ends to a MiB boundary. Sizing root all the way to the last
# byte would push its end past the last usable LBA and parted would reject it,
# so we stop one MiB short — negligible loss, always safe.
_GPT_TAIL_MIB = 1

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


def _root_size_mib(cfg: BootstrapConfig, disk_size_bytes: int) -> int:
    """Root partition size (MiB) that fills the disk after the ESP.

    Everything stays MiB-aligned: the root partition starts at a MiB boundary
    (1 MiB reserved head + the ESP) and we subtract whole MiB for the GPT tail,
    so the computed end lands on a MiB boundary parted is happy with.
    """
    disk_mib = disk_size_bytes // _MIB
    root_start_mib = 1 + cfg.esp_size_mib
    root_mib = disk_mib - root_start_mib - _GPT_TAIL_MIB
    if root_mib <= 0:
        raise ValueError(
            f"disk {cfg.device} ({disk_size_bytes} B) is too small for the "
            f"requested layout: {root_start_mib} MiB reserved for the 1 MiB head "
            f"+ {cfg.esp_size_mib} MiB ESP leaves no room for a root partition."
        )
    return root_mib


def _partitions(cfg: BootstrapConfig, disk_size_bytes: int) -> list[dict]:
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
        # start after the ESP; concrete size filling the rest of the disk.
        # archinstall's headless schema has no fill sentinel — a size of 0 would
        # become a zero-length partition and parted would reject it — so the size
        # is computed from the real disk (see _root_size_mib / probe_disk_size_bytes).
        "size": {**_sector(), "unit": "MiB", "value": _root_size_mib(cfg, disk_size_bytes)},
        "start": {**_sector(), "unit": "MiB", "value": 1 + cfg.esp_size_mib},
        "status": "create", "type": "primary",
    }
    return [esp, root]


def build_archinstall_config(cfg: BootstrapConfig, disk_size_bytes: int) -> dict:
    """Map a BootstrapConfig onto the archinstall 3.0.15 headless JSON schema.

    Pure (no archinstall import, no I/O). ``disk_size_bytes`` is the total size
    of ``cfg.device`` — required, because the root partition carries a concrete
    size (the schema has no fill-remaining sentinel); the caller probes it via
    ``_partition_plan.probe_disk_size_bytes``.
    """
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
                {"device": cfg.device, "partitions": _partitions(cfg, disk_size_bytes), "wipe": True},
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
