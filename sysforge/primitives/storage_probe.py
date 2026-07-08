# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
storage_probe.py — storage / filesystem diagnostics (doctor ``storage`` axis).

Two read-only checks:

  - free space on the build directory (``F17``) — the analog of the reconfigure
    stage's disk step, promoted to a standalone doctor axis so an ad-hoc
    ``sysforge doctor`` run surfaces a nearly-full disk.
  - ``/etc/fstab`` integrity (``F18``) — flag entries whose ``UUID=``/``LABEL=``/
    ``PARTUUID=``/``PARTLABEL=`` or bare device path no longer resolves (a stale
    mount that will fail — or hang — the next boot).

``probe_free_space`` is the **sole home** for the ``shutil.disk_usage`` call;
``reconfigure.py`` consumes it too. Every filesystem read is guarded so an
absent/unreadable file yields *no* findings rather than a crash (``run_axes``
isolates exceptions as a backstop). Never mounts anything, never writes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sysforge.primitives import diagnostics as diag

_GB = 1024 ** 3

# Warn below this many GB free on the build dir when [doctor] disk_low_gb unset.
DEFAULT_DISK_LOW_GB = 10.0

# fstab fs-spec / fs-type prefixes we never dereference: pseudo filesystems and
# network mounts (whose backing "device" is not a local /dev node).
_PSEUDO_FSTYPES = frozenset({
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "hugetlbfs",
    "debugfs", "tracefs", "securityfs", "cgroup", "cgroup2", "pstore",
    "configfs", "fusectl", "bpf", "swap", "none", "overlay", "squashfs",
    "ramfs", "efivarfs", "autofs", "binfmt_misc",
})
_NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "ncpfs", "9p", "afs",
    "ceph", "glusterfs", "fuse.sshfs", "fuse.gvfsd-fuse", "davfs",
})


def probe_free_space(path: str | Path) -> tuple[float, float] | None:
    """Free / total GB for the nearest existing ancestor mount of ``path``.

    Walks up until an existing directory is found (a not-yet-created build dir
    still reports the space of the filesystem it would live on), then reads
    ``shutil.disk_usage``. Returns ``(free_gb, total_gb)`` or ``None`` on error.

    **Sole home** for ``shutil.disk_usage`` — reconfigure's disk step consumes
    this rather than calling ``disk_usage`` directly.
    """
    try:
        p = Path(path).expanduser()
    except (OSError, RuntimeError):
        return None
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        usage = shutil.disk_usage(p)
    except OSError:
        return None
    return (usage.free / _GB, usage.total / _GB)


def _resolve_build_dir(config: dict | None) -> str | None:
    """The tree doctor measures free space on — the configured source/build dir."""
    from sysforge.primitives import config as cfgmod
    return cfgmod.resolve_pkgbuild_src_dir(config)


def _check_disk_space(config: dict | None, doctor_cfg: dict) -> list[diag.Finding]:
    """F17: warn when the build dir's free space is under ``disk_low_gb``."""
    build_dir = _resolve_build_dir(config)
    if not build_dir:
        return []
    space = probe_free_space(build_dir)
    if space is None:
        return []
    free_gb, total_gb = space
    try:
        low = float(doctor_cfg.get("disk_low_gb", DEFAULT_DISK_LOW_GB))
    except (TypeError, ValueError):
        low = DEFAULT_DISK_LOW_GB
    if free_gb >= low:
        return []
    return [diag.Finding(
        "storage", diag.SEV_WARN, "disk_low",
        f"only {free_gb:.1f} GB free of {total_gb:.1f} GB on the build dir "
        f"({build_dir}) — under the {low:.0f} GB threshold",
        remediation="free space: clear build caches (`sysforge clean` / rm -rf "
                    "~/builds/*), trim the pacman cache (`paccache -r`), or prune "
                    "old package versions",
    )]


def _iter_fstab_entries(text: str):
    """Yield ``(fs_spec, mount_point, fs_type, options)`` for real fstab lines."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3:
            continue
        yield fields[0], fields[1], fields[2], (fields[3] if len(fields) > 3 else "")


def _fs_spec_resolves(fs_spec: str) -> bool:
    """Does an fstab fs-spec still point at a present block device?"""
    by = {
        "UUID": "/dev/disk/by-uuid",
        "LABEL": "/dev/disk/by-label",
        "PARTUUID": "/dev/disk/by-partuuid",
        "PARTLABEL": "/dev/disk/by-partlabel",
        "ID": "/dev/disk/by-id",
    }
    tag, sep, value = fs_spec.partition("=")
    if sep and tag in by:
        # by-label paths are systemd-escaped, but a plain existence check on the
        # unescaped name catches the common case; fall back to a scan.
        target = Path(by[tag]) / value
        if target.exists():
            return True
        parent = Path(by[tag])
        try:
            return any(value == e.name for e in parent.iterdir())
        except OSError:
            # The by-<tag> dir itself is absent → treat as unresolved.
            return False
    # A bare path (device node or a bind-mount source directory).
    try:
        return Path(fs_spec).exists()
    except OSError:
        return False


def _check_fstab(fstab_path: str | Path = "/etc/fstab") -> list[diag.Finding]:
    """F18: flag fstab entries whose fs-spec no longer resolves."""
    try:
        text = Path(fstab_path).read_text(encoding="utf-8")
    except OSError:
        return []
    dangling: list[str] = []
    for fs_spec, mount_point, fs_type, options in _iter_fstab_entries(text):
        if fs_type in _PSEUDO_FSTYPES or fs_type in _NETWORK_FSTYPES:
            continue
        if "nofail" in options.split(","):
            continue
        if _fs_spec_resolves(fs_spec):
            continue
        dangling.append(f"{fs_spec} → {mount_point}")
    if not dangling:
        return []
    sample = "; ".join(dangling[:8])
    more = "" if len(dangling) <= 8 else f" (+{len(dangling) - 8} more)"
    return [diag.Finding(
        "storage", diag.SEV_WARN, "fstab_dangling",
        f"{len(dangling)} /etc/fstab entr"
        f"{'y' if len(dangling) == 1 else 'ies'} no longer resolve to a present "
        f"device: {sample}{more}",
        remediation="fix or remove the stale entry, or add `nofail` if the mount "
                    "is optional — otherwise the next boot may drop to emergency mode",
    )]


def collect_storage_findings(config: dict | None = None,
                             *,
                             doctor_cfg: dict | None = None,
                             fstab_path: str | Path = "/etc/fstab",
                             ) -> list[diag.Finding]:
    """Run all storage/filesystem checks; return findings (read-only)."""
    if doctor_cfg is None:
        from sysforge.primitives import config as cfgmod
        doctor_cfg = cfgmod.load_sysforge_toml().get("doctor", {}) or {}
    findings: list[diag.Finding] = []
    findings += _check_disk_space(config, doctor_cfg)
    findings += _check_fstab(fstab_path)
    return findings
