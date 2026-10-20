# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
install_reconcile.py — pacman-hook sentinel I/O for external-install demotion.

When a package sysforge previously built from source (``build_mode =
"source_built"``) is reinstalled from the repo via ``pacman -S``, the next
``sysforge update`` would otherwise rebuild it from source and undo the switch.
This module supplies the facts that let ``update`` auto-demote such a package
back to a plain ``pacman`` marker.

Two sentinels under ``/var/lib/sysforge/sentinels/`` cooperate:

  - ``buildstate`` — appended by the shipped ``sysforge-buildstate.hook``
    (PostTransaction, ``NeedsTargets``): every pacman transaction's target
    package list. This is *every* install/upgrade/remove, sysforge's own
    ``pacman -U`` builds included.
  - ``self-install`` — appended by :func:`record_self_install`, through
    ``pacman.record_installed_artifacts`` at every ``pacman -U`` of sysforge's
    own artifacts: the pkgnames sysforge itself installs.

A package is *external* when its latest ``buildstate`` event is later than its
latest ``self-install`` event (:func:`external_install_targets`) — i.e. a
user's ``pacman -S`` since sysforge last installed it. Those are the demote
candidates. Ordered rather than a set difference (3.5.0-B64): ``build foo``,
then ``pacman -S foo`` puts ``foo`` in both sentinels, and a set difference
kept it self-installed. Every consumer goes through :func:`consume_external_installs`,
which also clears the window.

Event format (shared with ``tools/pacman-hook-helper.sh``): each transaction is
a timestamp line followed by zero or more pkgname lines, then a blank line.
This is the single home for that format; do not parse the sentinels elsewhere.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log

_log = log.get_logger("RECONCILE")

# Fixed by the shipped libalpm hook + tmpfiles.d (see PKGBUILD). The buildstate
# sentinel is written by root (the hook); the self-install sentinel is written
# by the build user via the group-writable state dir. update.py owns the same
# directory as ``_SENTINEL_DIR`` and threads it into the dir-taking functions
# below so there is one effective path in production and one patch point in
# tests.
SENTINEL_DIR = Path("/var/lib/sysforge/sentinels")
_BUILDSTATE_NAME = "buildstate"
_SELF_INSTALL_NAME = "self-install"


def _dir(sentinel_dir) -> Path:
    return Path(sentinel_dir) if sentinel_dir is not None else SENTINEL_DIR


def _parse_stamped_events(text: str) -> list[tuple[str, set[str]]]:
    """Parse appended sentinel blocks into ``(timestamp, pkgnames)`` pairs.

    Each block is a timestamp line then pkgname lines then a blank separator.
    A line that looks like a timestamp (``...Z``) starts a new block. Names
    before any timestamp are kept under ``""``, which orders before every
    stamp.
    """
    events: list[tuple[str, set[str]]] = []
    stamp = ""
    current: set[str] = set()
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if started:
                events.append((stamp, current))
                current = set()
                started = False
            continue
        if _looks_like_timestamp(line):
            # Timestamp opens a new block; flush any in-progress one first.
            if started:
                events.append((stamp, current))
                current = set()
            stamp = line
            started = True
            continue
        current.add(line)
    if started:
        events.append((stamp, current))
    return events


def _parse_events(text: str) -> list[set[str]]:
    """Parse appended sentinel blocks into a list of pkgname sets."""
    return [names for _, names in _parse_stamped_events(text)]


def _looks_like_timestamp(line: str) -> bool:
    """True for an ISO-8601 UTC stamp like ``2026-06-21T20:07:00Z``."""
    if not line.endswith("Z") or "T" not in line:
        return False
    try:
        datetime.strptime(line, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def _read_targets(path: Path) -> set[str]:
    """Union of all target pkgnames recorded in ``path`` (empty if absent)."""
    return set(_last_seen(path))


def _last_seen(path: Path) -> dict[str, str]:
    """Each pkgname in ``path`` mapped to its latest event's timestamp.

    ISO-8601 UTC stamps at a fixed width compare correctly as strings.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    seen: dict[str, str] = {}
    for stamp, names in _parse_stamped_events(text):
        for name in names:
            if stamp >= seen.get(name, ""):
                seen[name] = stamp
    return seen


def resolve_installed_name(bs, name: str) -> str:
    """Resolve a user-supplied package name to its actually-installed name.

    A build that earned the ``-sysforge`` rename (conflict/coexist modes) is
    installed under the renamed name while recording the stock base in
    ``origin_pkgbase``. A user naming the stock base (``mesa``) should still
    reach the installed ``mesa-sysforge``. Resolution order:

      * exact tracked key            -> returned unchanged
      * some entry's origin_pkgbase  -> that entry's key (lowest key, so the
                                        result is deterministic across dict order)
      * ``<name>-sysforge`` tracked  -> that key: a renamed split *member*'s
                                        stock name (``vulkan-radeon``) records
                                        the pkgbase as origin, not itself
                                        (3.5.0-B62)
      * otherwise (untracked / repo) -> returned unchanged

    Single home for this reverse lookup -- both ``revert_cmd`` and
    ``uninstall_cmd`` call it; never reimplement.
    """
    entries = bs.all_packages()
    if name in entries:
        return name
    for key in sorted(entries):
        if entries[key].get("origin_pkgbase") == name:
            return key
    from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX
    renamed = f"{name}-{RENAME_SUFFIX}"
    if renamed in entries:
        return renamed
    return name


def external_install_targets(sentinel_dir=None) -> set[str]:
    """Packages installed externally (``pacman -S``) since the last reconcile.

    A ``buildstate`` target whose latest transaction is later than sysforge's
    own latest ``self-install`` of it, or which sysforge never installed in the
    window. A tie is sysforge's: the hook stamps its own ``pacman -U`` in the
    same second the record follows it. Empty when neither sentinel exists.
    """
    d = _dir(sentinel_dir)
    own = _last_seen(d / _SELF_INSTALL_NAME)
    return {name for name, stamp in _last_seen(d / _BUILDSTATE_NAME).items()
            if stamp > own.get(name, "")}


def consume_external_installs(bs, sentinel_dir=None, *, read_only: bool = False) -> list[str]:
    """Demote *bs*'s externally reinstalled entries, then clear the window.

    The one consumer of the sentinels, shared by ``update``, ``revert`` and
    ``uninstall`` (3.5.0-B64: the latter two used to skip the sync-repo
    filter and leave the window open). Only a package some sync repo carries
    could have been reinstalled from the repo via ``pacman -S``; a source-built
    ``-git`` package has none, so its ``buildstate`` event is sysforge's own
    ``pacman -U`` whose record went missing (e.g. a sentinel dir the hook
    created ``root:root`` before group provisioning), and demoting it would
    mass-demote every ``-git`` package. ``get_pacman_sync_version`` reads the
    local sync DBs (offline-safe), returning None for anything not in a repo.

    Returns the demoted pkgnames; the caller saves *bs*. ``read_only``
    (``--dry-run`` and the reports) demotes in memory and leaves the sentinels
    for the real run (3.5.0-B97); otherwise they are cleared even when the
    reconcile raises.
    """
    try:
        external = external_install_targets(sentinel_dir)
        if not external:
            return []
        from sysforge.primitives.pacman import get_pacman_sync_version
        external = {n for n in external if get_pacman_sync_version(n) is not None}
        return bs.reconcile_external_installs(external) if external else []
    finally:
        if not read_only:
            clear_reconcile_sentinels(sentinel_dir)


def record_self_install(pkgnames, sentinel_dir=None) -> None:
    """Append ``pkgnames`` to the self-install sentinel (best-effort, never raises).

    Called through ``pacman.record_installed_artifacts`` after each successful
    ``pacman -U`` of sysforge's own artifacts, so a later reconcile can tell
    them from an external ``pacman -S``. The
    state dir is group-writable (fs_provision); on any error this silently
    no-ops — a missing marker at worst costs one spurious demote, never a crash.
    """
    names = [n for n in (pkgnames or []) if n]
    if not names:
        return
    d = _dir(sentinel_dir)
    try:
        # Route through fs_provision so the sentinels dir lands (or is healed to)
        # root:sysforge 2775 — the same group-writable tree the root libalpm
        # hooks write into (2.2.0-B5). ``allow_sudo=False``: this runs from the
        # unprivileged ``pacman -U`` chokepoint and must never sudo-prompt. If
        # provisioning can't heal ownership (FsProvisionError), fall back to a
        # bare mkdir — the append below is still best-effort.
        from sysforge.primitives import fs_provision

        try:
            fs_provision.ensure_writable_dir(d, allow_sudo=False)
        except fs_provision.FsProvisionError:
            d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = ts + "\n" + "\n".join(names) + "\n\n"
        # Create/keep the sentinel group-writable so a later run under a
        # different uid in the ``sysforge`` group can append. The setgid state
        # dir (fs_provision, 2775) grants group *ownership* of new files, but
        # the group-*write* bit still comes from the umask — without this a file
        # first written by root (a ``sudo sysforge`` run) blocks a later append
        # by the build user with EACCES (2.1.0-B13). ``0o664`` is masked by the
        # umask on create, so fchmod explicitly once the fd is open (and we own
        # it, healing a pre-existing umask-644 file).
        fd = os.open(
            d / _SELF_INSTALL_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o664,
        )
        try:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o664)  # not the owner — the append still succeeds via group-write
            os.write(fd, block.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        _log.info(f"could not record self-install marker (non-fatal): {e}")


def clear_reconcile_sentinels(sentinel_dir=None) -> None:
    """Unlink the buildstate + self-install sentinels (best-effort)."""
    d = _dir(sentinel_dir)
    for path in (d / _BUILDSTATE_NAME, d / _SELF_INSTALL_NAME):
        with contextlib.suppress(OSError):
            path.unlink()
