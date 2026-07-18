# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
restart_probe.py — detect running processes still using replaced files.

When a package upgrade replaces a file that a running process has mapped, the
kernel marks that mapping ``(deleted)`` in ``/proc/<pid>/maps``. That mapping is
direct evidence the process is executing superseded code — the basis for
answering "did that upgrade actually take effect?" from fact rather than from a
curated list of restart-worthy packages.

Pure detection: no rendering, no privilege escalation, no mutation. Consumers
(``doctor``'s ``restart`` axis, the end-of-``update`` summary) render the
returned :class:`StaleReport`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives import pacman


# Prefixes whose files belong to packages. Everything else — /dev/shm and /tmp
# scratch, memfd, anonymous and pseudo mappings — is application churn, not an
# un-applied upgrade. This filter is load-bearing: on a live desktop every
# deleted mapping was /dev/shm scratch (Steam), so without it the report is
# 100% false positives.
_PKG_PREFIXES = ("/usr/", "/opt/")

_DELETED_SUFFIX = " (deleted)"

# maps line: addr perms offset dev inode [path]. The path may contain spaces, so
# split on whitespace a fixed number of times and keep the remainder. Real
# ``maps`` lines pad with multiple spaces before the path, and ``str.split``
# with no separator collapses that run of whitespace into the single split
# point — which is what makes a fixed field count line up with the actual
# path start rather than eating into it.
_MAPS_FIELDS = 5


def _mapping_path(line: str) -> str | None:
    """Return the deleted-file path from a maps line, or None if the line has no
    path or is not a deleted mapping."""
    stripped = line.rstrip("\n")
    parts = stripped.split(None, _MAPS_FIELDS)
    if len(parts) <= _MAPS_FIELDS:
        return None
    path = parts[_MAPS_FIELDS].strip()
    if not path.endswith(_DELETED_SUFFIX):
        return None
    path = path[: -len(_DELETED_SUFFIX)].strip()
    if not path.startswith(_PKG_PREFIXES):
        return None
    return path


def _scan_deleted_mappings(proc_root: Path) -> tuple[dict[str, list[int]], int]:
    """Walk ``proc_root`` for package-owned ``(deleted)`` mappings.

    Returns ``(path -> holding pids, unreadable_pid_count)``. A PID whose maps
    file cannot be read counts as unreadable (it is almost always a root-owned
    process); a PID that has vanished mid-scan is ignored entirely, since
    processes exiting during a walk is normal, not a coverage gap.
    """
    found: dict[str, list[int]] = {}
    unreadable = 0
    for entry in sorted(proc_root.iterdir()):
        if not entry.name.isdigit():
            continue
        maps = entry / "maps"
        try:
            text = maps.read_text(errors="replace")
        except OSError:
            if maps.exists():
                unreadable += 1
            continue
        pid = int(entry.name)
        for line in text.splitlines():
            path = _mapping_path(line)
            if path is not None and pid not in found.setdefault(path, []):
                found[path].append(pid)
    return found, unreadable


# Tier constants for remediation strategies.
TIER_RESTART_UNIT = "restart-unit"
TIER_RELOGIN = "re-login"
TIER_REBOOT = "reboot"

# Higher = more disruptive. A report states the highest tier present.
_TIER_RANK = {TIER_RESTART_UNIT: 0, TIER_RELOGIN: 1, TIER_REBOOT: 2}

_USER_MANAGER_RE = re.compile(r"/user@(?P<uid>\d+)\.service/")


def tier_rank(tier: str | None) -> int:
    """Rank a tier for "worst wins" reduction. An unknown/absent tier ranks
    below every real tier so it never wins the reduction."""
    return _TIER_RANK.get(tier or "", -1)


def is_kernel_entry(entry: "StaleEntry") -> bool:
    """True when ``entry`` is the ``_kernel_reboot_entry()`` sentinel.

    ``TIER_REBOOT`` has two sources — the running kernel's module dir being
    gone, and pid 1 (systemd itself) mapping a deleted file — and consumers
    must not conflate them: only the former is actually about the kernel.
    The kernel entry is identifiable by its synthetic ``pid=0``/``comm=
    "kernel"`` pairing, which no real process can have. Both consumers use
    this discriminator rather than branching on tier.
    """
    return entry.pid == 0 and entry.comm == "kernel"


def _classify_cgroup(cgroup_text: str, pid: int) -> tuple[str | None, str | None, bool]:
    """Classify a process into a remediation tier from its cgroup path.

    Returns ``(tier, unit_name, is_user_unit)``.

    The discriminator is the **leaf unit's suffix**, not which slice it sits in:
    a ``.service`` is individually restartable (``systemctl restart``, or
    ``systemctl --user restart`` under a user manager), while a ``.scope`` holds
    externally-spawned processes systemd supervises but did not launch and so has
    no restart verb — the only remedy is ending the session. Both kinds coexist
    under ``user@<uid>.service``, so classifying by slice would over-escalate
    every restartable user service into a full logout.
    """
    if pid == 1:
        return TIER_REBOOT, None, False

    # The unified (v2) hierarchy line is "0::/path"; take the last one present.
    path = ""
    for line in cgroup_text.splitlines():
        _, _, tail = line.partition("::")
        if tail:
            path = tail.strip()
    if not path:
        return None, None, False

    leaf = path.rsplit("/", 1)[-1]
    is_user = bool(_USER_MANAGER_RE.search(path + "/"))
    if leaf.endswith(".service"):
        return TIER_RESTART_UNIT, leaf, is_user
    if leaf.endswith(".scope"):
        return TIER_RELOGIN, None, is_user
    return None, None, False


@dataclass(frozen=True)
class StaleEntry:
    """One running process holding a superseded file."""
    pid: int
    comm: str
    tier: str | None
    package: str | None
    path: str
    unit: str | None = None
    is_user_unit: bool = False


@dataclass(frozen=True)
class StaleReport:
    entries: list[StaleEntry]
    highest_tier: str | None
    partial: bool

    def __bool__(self) -> bool:
        return bool(self.entries)


def _kernel_reboot_entry(*, modules_root: Path = Path("/usr/lib/modules")) -> StaleEntry | None:
    """Reboot-tier entry when the running kernel's package is no longer installed.

    Deliberately evidence-based rather than version-comparing: pacman's
    ``pkgver`` never carries the kernel's *localversion* suffix, but
    ``uname -r`` does (e.g. installed ``7.1.2.arch3-1`` vs. running-release
    ``7.1.2.arch3.1.sysforge`` after normalization) — no string transform
    reconciles the two, and ``linux``, ``linux-lts``, ``linux-sysforge`` and
    ``linux-custom`` coexist on this workstation so there's no single package
    name to compare against anyway.

    Instead this checks the one fact that actually answers "is the running
    kernel still installed": whether ``/usr/lib/modules/<uname -r>`` still
    exists. If a later package upgrade removed it, the running kernel's
    package has been replaced out from under the running system — a reboot
    signal on its own, independent of what (if anything) currently owns that
    path.
    """
    release = os.uname().release
    moddir = modules_root / release
    if not moddir.exists():
        return StaleEntry(pid=0, comm="kernel", tier=TIER_REBOOT,
                          package=None, path=str(moddir))
    return None


def scan_stale_processes(*, proc_root: Path = Path("/proc")) -> StaleReport:
    """Report running processes still using files a package upgrade replaced.

    Read-only and never escalates: ``/proc`` entries owned by other users are
    counted toward ``partial`` rather than triggering a privilege prompt. On a
    live desktop this still covers the session processes that matter; the
    invisible remainder is the root-owned ``/system.slice`` set.
    """
    found, unreadable = _scan_deleted_mappings(proc_root)
    owners = pacman.owners_of_paths(sorted(found)) if found else {}

    entries: list[StaleEntry] = []
    for path, pids in sorted(found.items()):
        for pid in pids:
            pid_dir = proc_root / str(pid)
            try:
                cgroup_text = (pid_dir / "cgroup").read_text()
            except OSError:
                cgroup_text = ""
            try:
                comm = (pid_dir / "comm").read_text().strip()
            except OSError:
                comm = ""
            tier, unit, is_user = _classify_cgroup(cgroup_text, pid)
            entries.append(StaleEntry(
                pid=pid, comm=comm, tier=tier, package=owners.get(path),
                path=path, unit=unit, is_user_unit=is_user))

    kernel_entry = _kernel_reboot_entry()
    if kernel_entry is not None:
        entries.append(kernel_entry)

    highest = max((e.tier for e in entries), key=tier_rank, default=None)
    if tier_rank(highest) < 0:
        highest = None
    return StaleReport(entries=entries, highest_tier=highest,
                       partial=unreadable > 0)
