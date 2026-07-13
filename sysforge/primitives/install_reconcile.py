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
  - ``self-install`` — appended by :func:`record_self_install`, called from the
    single ``pacman -U`` chokepoint (``pacman.batch_install_pkgs``): the
    pkgnames sysforge itself installs from its own built artifacts.

``external = buildstate_targets − self_install_targets`` is therefore the set of
packages installed by something *other* than sysforge — i.e. a user's
``pacman -S``. Those are the demote candidates.

Event format (shared with ``tools/pacman-hook-helper.sh``): each transaction is
a timestamp line followed by zero or more pkgname lines, then a blank line.
This is the single home for that format; do not parse the sentinels elsewhere.
"""
from __future__ import annotations

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


def _parse_events(text: str) -> list[set[str]]:
    """Parse appended sentinel blocks into a list of pkgname sets.

    Each block is a timestamp line then pkgname lines then a blank separator.
    Blank lines and the leading timestamp of each block are discarded; what
    remains per block is the set of target pkgnames. A line that looks like a
    timestamp (``...Z``) starts a new block.
    """
    events: list[set[str]] = []
    current: set[str] = set()
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if started:
                events.append(current)
                current = set()
                started = False
            continue
        if _looks_like_timestamp(line):
            # Timestamp opens a new block; flush any in-progress one first.
            if started:
                events.append(current)
                current = set()
            started = True
            continue
        current.add(line)
    if started:
        events.append(current)
    return events


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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    targets: set[str] = set()
    for block in _parse_events(text):
        targets |= block
    return targets


def resolve_installed_name(bs, name: str) -> str:
    """Resolve a user-supplied package name to its actually-installed name.

    A build that earned the ``-sysforge`` rename (conflict/coexist modes) is
    installed under the renamed name while recording the stock base in
    ``origin_pkgbase``. A user naming the stock base (``mesa``) should still
    reach the installed ``mesa-sysforge``. Resolution order:

      * exact tracked key            -> returned unchanged
      * some entry's origin_pkgbase  -> that entry's key (lowest key, so the
                                        result is deterministic across dict order)
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
    return name


def external_install_targets(sentinel_dir=None) -> set[str]:
    """Packages installed externally (``pacman -S``) since the last reconcile.

    ``buildstate`` targets minus sysforge's own ``self-install`` targets. Empty
    when neither sentinel exists.
    """
    d = _dir(sentinel_dir)
    return _read_targets(d / _BUILDSTATE_NAME) - _read_targets(d / _SELF_INSTALL_NAME)


def record_self_install(pkgnames, sentinel_dir=None) -> None:
    """Append ``pkgnames`` to the self-install sentinel (best-effort, never raises).

    Called from the ``pacman -U`` chokepoint so a later ``update`` reconcile can
    tell sysforge's own artifact installs from an external ``pacman -S``. The
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
            try:
                os.fchmod(fd, 0o664)
            except OSError:
                pass  # not the owner — the append still succeeds via group-write
            os.write(fd, block.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        _log.info(f"could not record self-install marker (non-fatal): {e}")


def clear_reconcile_sentinels(sentinel_dir=None) -> None:
    """Unlink the buildstate + self-install sentinels (best-effort)."""
    d = _dir(sentinel_dir)
    for path in (d / _BUILDSTATE_NAME, d / _SELF_INSTALL_NAME):
        try:
            path.unlink()
        except OSError:
            pass
