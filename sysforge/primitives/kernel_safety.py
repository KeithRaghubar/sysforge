# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel_safety.py — guardrails so the kernel stage can never leave the
machine unbootable.

This is the primitive behind the kernel stage's three safety gates and the
``doctor --hardware`` boot-readiness view. Everything here is read-only and
pure enough to unit-test against fixture trees; the kernel stage owns the
policy (what aborts vs warns), this module owns the *facts*.

Capabilities:
  - parse_kconfig / parse_kconfig_text   — read a resolved kernel .config
  - detect_root_topology                 — root FS / storage transport / crypt-lvm-raid
  - audit_resolved_config                — boot-critical + device-driver coverage check
  - find_fallback_kernels                — is there another bootable kernel?
  - verify_boot_artifacts                — vmlinuz + initramfs present & referenced by an entry
  - check_dkms_for_kernel                — DKMS modules rebuilt for the new kernel
  - check_boot_mount_space               — /boot mounted with headroom

Severity model: each KernelFinding carries ``is_brick`` — True means "the
machine will not boot / the build is dangerous to install". The kernel stage
hard-fails on brick findings and warns on the rest.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"

# Filesystem roots — module-level so tests can repoint them at a fixture tree.
_BOOT_DIR = Path("/boot")
_MODULES_DIR = Path("/usr/lib/modules")
_PROC_MOUNTS = Path("/proc/mounts")
_CRYPTTAB = Path("/etc/crypttab")
_MDSTAT = Path("/proc/mdstat")
_MKINITCPIO_CONF = Path("/etc/mkinitcpio.conf")

# A kernel/initramfs image smaller than this is almost certainly truncated.
_MIN_IMAGE_BYTES = 1_000_000


@dataclass(frozen=True)
class KernelFinding:
    severity: str          # SEV_ERROR | SEV_WARN | SEV_INFO
    check_id: str          # short stable id, e.g. "boot_kconfig:CONFIG_EXT4_FS"
    message: str
    remediation: str = ""
    is_brick: bool = False  # True → unbootable / dangerous to install


@dataclass(frozen=True)
class RootTopology:
    root_fstype: str | None = None
    transports: tuple[str, ...] = ()    # e.g. ("nvme",), ("virtio",)
    uses_crypt: bool = False
    uses_lvm: bool = False
    uses_raid: bool = False


# ---------------------------------------------------------------------------
# Low-level helpers (same idiom as graphics_probe / device_probe)
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# kconfig parsing  (the shared .config line parser)
# ---------------------------------------------------------------------------

def parse_kconfig_text(text: str) -> dict[str, str]:
    """Parse kernel .config text into {CONFIG_KEY: value}.

    ``CONFIG_X=y|m|"str"|123`` → that raw value; ``# CONFIG_X is not set`` → "n".
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            key, val = line.split("=", 1)
            result[key.strip()] = val.strip()
        elif line.startswith("# CONFIG_") and line.endswith("is not set"):
            parts = line.split()
            if len(parts) >= 2:
                result[parts[1]] = "n"
    return result


def parse_kconfig(path: Path) -> dict[str, str] | None:
    """Parse a resolved kernel .config file. None if unreadable/missing."""
    text = _read_text(Path(path))
    if text is None:
        return None
    return parse_kconfig_text(text)


def is_enabled(config: dict[str, str], symbol: str) -> bool:
    """True when ``symbol`` is built-in (=y) or a module (=m)."""
    return config.get(symbol) in ("y", "m")


# ---------------------------------------------------------------------------
# kconfig drift  (what sysforge merged vs what survived into the resolved .config)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KconfigDrift:
    """One option sysforge requested in the fragment that the resolved
    ``.config`` did not honour.

    ``requested``/``resolved`` use the same forms ``parse_kconfig_text``
    yields ("y", "m", "n", or a quoted ``"str"``); an option absent from the
    resolved config is normalised to "n" (kernel semantics: not-set == off).
    """
    option: str
    requested: str
    resolved: str
    kind: str          # "disabled" | "re-enabled" | "changed"


def diff_requested_kconfig(
    requested: dict[str, str], resolved: dict[str, str],
) -> list[KconfigDrift]:
    """Compare the options sysforge merged (``requested``) against the resolved
    ``.config`` (``resolved``) and classify every divergence.

    Pure fact-finder — only iterates keys in ``requested`` (sysforge's intent),
    so options the base config or kconfig auto-select added are ignored. A
    missing resolved option is treated as "n". Classification:

      - requested y/m → resolved n         → ``disabled``
      - requested y/m → resolved m/y (≠)   → ``changed`` (built-in↔module)
      - requested n   → resolved y/m       → ``re-enabled``
      - any other value mismatch (string/int) → ``changed``

    Returns drifts in ``requested`` insertion order; empty when nothing drifted.
    """
    drifts: list[KconfigDrift] = []
    for option, req in requested.items():
        res = resolved.get(option, "n")
        if res == req:
            continue
        req_on = req in ("y", "m")
        res_on = res in ("y", "m")
        if req_on and res == "n":
            kind = "disabled"
        elif req == "n" and res_on:
            kind = "re-enabled"
        else:
            kind = "changed"
        drifts.append(KconfigDrift(option=option, requested=req, resolved=res, kind=kind))
    return drifts


# ---------------------------------------------------------------------------
# Curated boot-critical kconfig tables
#
# (symbol, human reason). All entries here are brick-class unless noted.
# ---------------------------------------------------------------------------

# B3 — core boot infrastructure.
_CORE_BOOT_KCONFIG: tuple[tuple[str, str], ...] = (
    ("CONFIG_MODULES", "loadable module support"),
    ("CONFIG_BLK_DEV_INITRD", "initramfs support"),
    ("CONFIG_DEVTMPFS", "/dev population (devtmpfs)"),
    ("CONFIG_TMPFS", "tmpfs"),
    ("CONFIG_PROC_FS", "/proc filesystem"),
    ("CONFIG_SYSFS", "/sys filesystem"),
    ("CONFIG_BINFMT_ELF", "ELF binary support"),
    ("CONFIG_UNIX", "UNIX domain sockets"),
)

# B6 — systemd (PID 1) prerequisites.
_SYSTEMD_KCONFIG: tuple[tuple[str, str], ...] = (
    ("CONFIG_CGROUPS", "control groups (systemd requires)"),
    ("CONFIG_INOTIFY_USER", "inotify (systemd requires)"),
    ("CONFIG_EPOLL", "epoll (systemd requires)"),
    ("CONFIG_SIGNALFD", "signalfd (systemd requires)"),
    ("CONFIG_TIMERFD", "timerfd (systemd requires)"),
    ("CONFIG_NET", "networking core (systemd requires)"),
)

# B5 — console / framebuffer (degraded, not brick: boots but no local console).
_CONSOLE_KCONFIG: tuple[tuple[str, str], ...] = (
    ("CONFIG_TTY", "TTY layer"),
    ("CONFIG_VT", "virtual terminal / console"),
)

# B1 — root filesystem driver, keyed by fstype.
_ROOT_FS_KCONFIG: dict[str, str] = {
    "ext4": "CONFIG_EXT4_FS",
    "ext3": "CONFIG_EXT4_FS",
    "ext2": "CONFIG_EXT4_FS",
    "btrfs": "CONFIG_BTRFS_FS",
    "xfs": "CONFIG_XFS_FS",
    "f2fs": "CONFIG_F2FS_FS",
    "vfat": "CONFIG_VFAT_FS",
}

# B2 — root storage controller, keyed by transport.
_STORAGE_KCONFIG: dict[str, str] = {
    "nvme": "CONFIG_BLK_DEV_NVME",
    "ahci": "CONFIG_SATA_AHCI",
    "virtio": "CONFIG_VIRTIO_BLK",
    "mmc": "CONFIG_MMC_BLOCK",
    "usb": "CONFIG_USB_STORAGE",
    "xen": "CONFIG_XEN_BLKDEV_FRONTEND",
}

# B4 — root-on-crypt/LVM/RAID stacking drivers.
_CRYPT_KCONFIG = (("CONFIG_DM_CRYPT", "dm-crypt (encrypted root)"),
                  ("CONFIG_BLK_DEV_DM", "device-mapper"))
_LVM_KCONFIG = (("CONFIG_BLK_DEV_DM", "device-mapper (LVM root)"),)
_RAID_KCONFIG = (("CONFIG_BLK_DEV_MD", "md/RAID block device (RAID root)"),
                 ("CONFIG_MD", "multiple-device / RAID support"))


# ---------------------------------------------------------------------------
# Root topology detection
# ---------------------------------------------------------------------------

def _root_source() -> str | None:
    """Resolve the device backing ``/`` from /proc/mounts (findmnt fallback)."""
    text = _read_text(_PROC_MOUNTS)
    if text:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "/":
                return fields[0]
    r = _run(["findmnt", "-no", "SOURCE", "/"])
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def _root_fstype() -> str | None:
    text = _read_text(_PROC_MOUNTS)
    if text:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "/":
                return fields[2]
    return None


def _transport_from_name(name: str) -> str | None:
    base = name.rsplit("/", 1)[-1]
    if base.startswith("nvme"):
        return "nvme"
    if base.startswith("vd"):
        return "virtio"
    if base.startswith("xvd"):
        return "xen"
    if base.startswith("mmcblk"):
        return "mmc"
    if base.startswith(("sd", "sr")):
        return "ahci"
    return None


def detect_root_topology() -> RootTopology:
    """Best-effort root storage topology from sysfs/procfs/lsblk.

    Degrades gracefully: if ``lsblk -s`` is unavailable, the transport is
    inferred from the root device name alone and crypt/raid are still picked
    up from /etc/crypttab and /proc/mdstat.
    """
    fstype = _root_fstype()
    source = _root_source()

    transports: list[str] = []
    uses_crypt = uses_lvm = uses_raid = False

    # `lsblk -s` walks from the device up through its parents, giving the
    # full stack (crypt → lvm → part → disk) with one TYPE per line.
    if source:
        r = _run(["lsblk", "-nso", "NAME,TYPE", source])
        if r and r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                name, typ = parts[0], parts[1]
                typ = typ.lower()
                if typ == "crypt":
                    uses_crypt = True
                elif typ == "lvm":
                    uses_lvm = True
                elif typ.startswith("raid"):
                    uses_raid = True
                elif typ in ("disk", "part"):
                    t = _transport_from_name(name)
                    if t and t not in transports:
                        transports.append(t)
        else:
            t = _transport_from_name(source)
            if t:
                transports.append(t)

    # Corroborating signals independent of lsblk.
    crypttab = _read_text(_CRYPTTAB)
    if crypttab and any(
        ln.strip() and not ln.strip().startswith("#")
        for ln in crypttab.splitlines()
    ):
        uses_crypt = True
    mdstat = _read_text(_MDSTAT)
    if mdstat and any(line.startswith("md") for line in mdstat.splitlines()):
        uses_raid = True

    return RootTopology(
        root_fstype=fstype,
        transports=tuple(transports),
        uses_crypt=uses_crypt,
        uses_lvm=uses_lvm,
        uses_raid=uses_raid,
    )


# ---------------------------------------------------------------------------
# Resolved-.config audit (boot-critical + device-driver coverage)
# ---------------------------------------------------------------------------

def _required_boot_symbols(topology: RootTopology) -> list[tuple[str, str, bool]]:
    """Return (symbol, reason, is_brick) for everything boot-critical given
    the root topology. De-duplicated, stable order."""
    out: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    def add(symbol, reason, is_brick):
        if symbol in seen:
            return
        seen.add(symbol)
        out.append((symbol, reason, is_brick))

    for sym, reason in _CORE_BOOT_KCONFIG:
        add(sym, reason, True)
    for sym, reason in _SYSTEMD_KCONFIG:
        add(sym, reason, True)
    for sym, reason in _CONSOLE_KCONFIG:
        add(sym, reason, False)  # degraded, not brick

    if topology.root_fstype:
        fs_sym = _ROOT_FS_KCONFIG.get(topology.root_fstype)
        if fs_sym:
            add(fs_sym, f"root filesystem ({topology.root_fstype})", True)
    for transport in topology.transports:
        st_sym = _STORAGE_KCONFIG.get(transport)
        if st_sym:
            add(st_sym, f"root storage controller ({transport})", True)
    if topology.uses_crypt:
        for sym, reason in _CRYPT_KCONFIG:
            add(sym, reason, True)
    if topology.uses_lvm:
        for sym, reason in _LVM_KCONFIG:
            add(sym, reason, True)
    if topology.uses_raid:
        for sym, reason in _RAID_KCONFIG:
            add(sym, reason, True)

    return out


def audit_resolved_config(
    config,
    topology: RootTopology | None = None,
    devices=None,
) -> list[KernelFinding]:
    """Audit a resolved kernel .config against the running system.

    ``config`` may be a path to a .config or an already-parsed dict.
    Covers boot-critical symbols (B1–B6, keyed off ``topology``) and present
    devices' driver symbols (A1/A3, advisory) from ``devices``
    (``device_probe.Device`` list). Brick-class drops are flagged
    ``is_brick=True``; the kernel stage decides what to do with them.
    """
    if isinstance(config, dict):
        parsed = config
    else:
        parsed = parse_kconfig(Path(config))
    if parsed is None:
        return [KernelFinding(
            SEV_WARN, "kconfig_unreadable",
            f"could not read resolved kernel .config at {config}",
            "Boot-critical config could not be validated before install.",
            is_brick=False,
        )]

    topo = topology if topology is not None else detect_root_topology()
    findings: list[KernelFinding] = []

    required = _required_boot_symbols(topo)
    required_syms = {sym for sym, _, _ in required}
    for sym, reason, is_brick in required:
        if is_enabled(parsed, sym):
            continue
        sev = SEV_ERROR if is_brick else SEV_WARN
        findings.append(KernelFinding(
            sev, f"boot_kconfig:{sym}",
            f"{sym} is not enabled — needed for {reason}.",
            f"Set {sym}=y (or =m) before building this kernel.",
            is_brick=is_brick,
        ))

    # Device-driver coverage (advisory). Skip symbols already covered as
    # brick-class boot requirements so we don't double-report.
    for dev in (devices or []):
        for sym in getattr(dev, "suggested_kconfig", []):
            if sym in required_syms or is_enabled(parsed, sym):
                continue
            required_syms.add(sym)  # dedupe across devices
            findings.append(KernelFinding(
                SEV_WARN, f"device_kconfig:{sym}",
                f"{sym} is not enabled — {getattr(dev, 'description', '')} "
                f"({getattr(dev, 'address', '?')}) would have no driver.",
                f"Enable {sym} to support this device.",
                is_brick=False,
            ))

    return findings


# ---------------------------------------------------------------------------
# Fallback-kernel guarantee
# ---------------------------------------------------------------------------

def find_fallback_kernels(exclude_pkg: str | None = None) -> list[str]:
    """Return the suffixes of other bootable kernels present in /boot.

    A bootable kernel = a ``vmlinuz-<suffix>`` image with a matching
    ``initramfs-<suffix>.img``. The custom kernel under construction
    (``exclude_pkg``) is excluded, so an empty result means "no recovery
    kernel exists".
    """
    try:
        entries = list(_BOOT_DIR.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return []

    fallbacks: list[str] = []
    for entry in sorted(entries):
        name = entry.name
        if not name.startswith("vmlinuz-"):
            continue
        suffix = name[len("vmlinuz-"):]
        if exclude_pkg and suffix == exclude_pkg:
            continue
        initramfs = _BOOT_DIR / f"initramfs-{suffix}.img"
        if initramfs.exists():
            fallbacks.append(suffix)
    return fallbacks


# ---------------------------------------------------------------------------
# Post-install boot-artifact verification
# ---------------------------------------------------------------------------

def _boot_entry_references(pkgname: str) -> bool:
    """True if any systemd-boot loader entry or grub.cfg references the kernel."""
    needle = f"vmlinuz-{pkgname}"
    entries_dir = _BOOT_DIR / "loader" / "entries"
    try:
        loader_entries = list(entries_dir.glob("*.conf"))
    except (OSError,):
        loader_entries = []
    for conf in loader_entries:
        text = _read_text(conf) or ""
        if needle in text or pkgname in text:
            return True
    grub_cfg = _read_text(_BOOT_DIR / "grub" / "grub.cfg")
    if grub_cfg and needle in grub_cfg:
        return True
    return False


def verify_boot_artifacts(pkgname: str, bootloader: str = "systemd-boot") -> list[KernelFinding]:
    """Confirm the kernel is actually bootable after install.

    Checks vmlinuz + initramfs are present and non-trivial in /boot, and that
    at least one boot entry references the kernel. Missing artifacts or a
    missing boot entry are brick-class — the install ran but the machine
    cannot select the new kernel.
    """
    findings: list[KernelFinding] = []

    vmlinuz = _BOOT_DIR / f"vmlinuz-{pkgname}"
    try:
        vsize = vmlinuz.stat().st_size if vmlinuz.exists() else 0
    except OSError:
        vsize = 0
    if vsize < _MIN_IMAGE_BYTES:
        findings.append(KernelFinding(
            SEV_ERROR, "boot_vmlinuz_missing",
            f"kernel image {vmlinuz} is missing or truncated "
            f"({vsize} bytes).",
            "The package installed but no usable kernel image landed in "
            f"{_BOOT_DIR}. Check that /boot/ESP is mounted and rebuild.",
            is_brick=True,
        ))

    initramfs = _BOOT_DIR / f"initramfs-{pkgname}.img"
    try:
        isize = initramfs.stat().st_size if initramfs.exists() else 0
    except OSError:
        isize = 0
    if isize < _MIN_IMAGE_BYTES:
        findings.append(KernelFinding(
            SEV_ERROR, "boot_initramfs_missing",
            f"initramfs {initramfs} is missing or truncated ({isize} bytes).",
            "Run `sudo mkinitcpio -P`; if it still fails the kernel cannot boot.",
            is_brick=True,
        ))

    # Fallback initramfs is recommended but not brick-class.
    fallback = _BOOT_DIR / f"initramfs-{pkgname}-fallback.img"
    if not fallback.exists():
        findings.append(KernelFinding(
            SEV_WARN, "boot_fallback_initramfs_missing",
            f"no fallback initramfs ({fallback}) — a broken main initramfs "
            "would have no autodetect-free safety image.",
            "Add a `-fallback` preset to /etc/mkinitcpio.d/<preset>.preset.",
            is_brick=False,
        ))

    if bootloader != "none" and not _boot_entry_references(pkgname):
        findings.append(KernelFinding(
            SEV_ERROR, "boot_entry_missing",
            f"no boot entry references vmlinuz-{pkgname} — the kernel is "
            "installed but cannot be selected at boot.",
            "For systemd-boot run `kernel-install add` or create a loader "
            "entry; for grub run `sudo grub-mkconfig -o /boot/grub/grub.cfg`.",
            is_brick=True,
        ))

    return findings


# ---------------------------------------------------------------------------
# DKMS coverage
# ---------------------------------------------------------------------------

def list_dkms_modules() -> list[str]:
    """Distinct DKMS module names registered on the system (empty if none)."""
    r = _run(["dkms", "status"])
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return []
    mods: list[str] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        mod = line.split(":", 1)[0].split(",", 1)[0].split("/", 1)[0]
        if mod and mod not in mods:
            mods.append(mod)
    return mods


def check_mkinitcpio_hooks(topology: RootTopology) -> list[KernelFinding]:
    """Warn when mkinitcpio HOOKS don't cover the root topology.

    sysforge does not own ``mkinitcpio.conf``; these are advisory (the user
    may use the systemd hook variants ``sd-encrypt`` / ``lvm2`` etc.), so
    nothing here is brick-class.
    """
    text = _read_text(_MKINITCPIO_CONF)
    if text is None:
        return []
    hooks_line = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("HOOKS="):
            hooks_line = s
            break
    if not hooks_line:
        return []

    findings: list[KernelFinding] = []

    def _want(present_any, names, layer):
        if present_any:
            return
        findings.append(KernelFinding(
            SEV_WARN, f"mkinitcpio_hook:{layer}",
            f"root uses {layer} but mkinitcpio HOOKS has none of "
            f"{', '.join(names)} — the initramfs may not assemble root.",
            f"Add a {layer} hook (one of {', '.join(names)}) to "
            f"{_MKINITCPIO_CONF} and run `sudo mkinitcpio -P`.",
            is_brick=False,
        ))

    if topology.uses_crypt:
        _want(("encrypt" in hooks_line or "sd-encrypt" in hooks_line),
              ("encrypt", "sd-encrypt"), "encryption")
    if topology.uses_lvm:
        _want("lvm2" in hooks_line, ("lvm2",), "LVM")
    if topology.uses_raid:
        _want(("mdadm_udev" in hooks_line or "mdadm" in hooks_line),
              ("mdadm_udev",), "RAID")
    return findings


def _dkms_module_in_tree(mod: str, kver: str) -> bool:
    """True when a DKMS-managed ``.ko`` for ``mod`` exists in ``kver``'s module
    tree — the fact behind a merely-"built" dkms state actually being loadable."""
    dkms_dir = _MODULES_DIR / kver / "updates" / "dkms"
    try:
        return any(dkms_dir.glob(f"{mod}*.ko*"))
    except OSError:
        return False


def check_dkms_for_kernel(kver: str) -> list[KernelFinding]:
    """Flag DKMS modules not built+installed for kernel release ``kver``.

    A DKMS module that hasn't rebuilt against the new kernel won't load on
    reboot — for nvidia that's a black screen, for zfs-on-root a brick. We
    can't tell root-criticality here, so these are degraded (warn) findings.
    Returns nothing when dkms is absent or reports no modules.
    """
    r = _run(["dkms", "status"])
    if r is None or r.returncode != 0 or not r.stdout.strip():
        return []

    # Map module → set of kernel versions it is present for. A module counts as
    # present for a kver when dkms reports it "installed", OR reports it merely
    # "built" but its .ko is actually in that kernel's module tree: newer dkms
    # (3.x) can leave a loaded, working module at "built" (e.g. after the .ko
    # was placed by the package rather than a fresh `dkms install`). Trusting
    # the literal state word alone false-flags such healthy modules.
    present_for: dict[str, set[str]] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Formats: "nvidia/570.x, 7.0.10-arch1-1, x86_64: installed"
        #          "nvidia/570.x, 7.0.10-arch1-1, x86_64: built"
        #          "nvidia/570.x: added"  (no kernel → not built for any)
        head = line.split(":", 1)[0]
        state = line.split(":", 1)[1].strip() if ":" in line else ""
        fields = [f.strip() for f in head.split(",")]
        mod = fields[0].split("/", 1)[0]
        present_for.setdefault(mod, set())
        if len(fields) < 2:
            continue
        mod_kver = fields[1]
        if state == "installed" or (
                state == "built" and _dkms_module_in_tree(mod, mod_kver)):
            present_for[mod].add(mod_kver)

    findings: list[KernelFinding] = []
    for mod, kvers in sorted(present_for.items()):
        if kver not in kvers:
            findings.append(KernelFinding(
                SEV_WARN, f"dkms:{mod}",
                f"DKMS module {mod!r} is not built for kernel {kver} — it "
                "will not load on the new kernel.",
                f"Install `{kver}` headers and run "
                f"`sudo dkms install {mod} -k {kver}` (or reinstall the "
                "DKMS package). nvidia → black screen; zfs root → unbootable.",
                is_brick=False,
            ))
    return findings


# ---------------------------------------------------------------------------
# /boot mount + free space
# ---------------------------------------------------------------------------

def check_boot_mount_space(min_mb: int = 200) -> KernelFinding | None:
    """Verify /boot exists and has at least ``min_mb`` free.

    A too-full or unmounted ESP is a classic way for a kernel install to
    write a truncated image. Brick-class when it trips.
    """
    if not _BOOT_DIR.exists():
        return KernelFinding(
            SEV_ERROR, "boot_dir_missing",
            f"{_BOOT_DIR} does not exist — kernel images have nowhere to land.",
            f"Mount the boot/ESP partition at {_BOOT_DIR} before building.",
            is_brick=True,
        )
    try:
        usage = shutil.disk_usage(_BOOT_DIR)
    except OSError:
        return None
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_mb:
        return KernelFinding(
            SEV_ERROR, "boot_low_space",
            f"{_BOOT_DIR} has only {free_mb} MiB free (need ≥ {min_mb} MiB) — "
            "a kernel + initramfs install may be truncated.",
            f"Free space on {_BOOT_DIR} (remove stale kernels) before building.",
            is_brick=True,
        )
    return None


# ---------------------------------------------------------------------------
# Running-kernel release helper
# ---------------------------------------------------------------------------

def running_kernel_release() -> str:
    """The running kernel's release string (``uname -r`` equivalent)."""
    return os.uname().release
