# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kconfig_history.py — archive of recent resolved kernel ``.config`` files (2.6.1-F25).

The kernel stage's change summary answers "what did I actually change about
this kernel" by diffing the config it just built against the config it built
last time. Nothing else in sysforge keeps a resolved ``.config`` after the
build tree is cleaned, so this module owns that history.

Layout: ``<state_dir>/kconfig-history/<pkgname>-<release>.config.gz``, newest
``KEEP`` per pkgname, pruned on every write. A resolved config gzips to a few
tens of KiB, so the whole archive stays in the low hundreds of KiB — small
enough to keep unconditionally, which is why there is no enable switch.

Every function is best-effort: this is advisory reporting, and a full disk or
an unreadable archive must never affect a kernel build. Failures return
``None`` / an empty result rather than raising.

Public API:
    archive_path(state_dir, pkgname, release) -> Path
    archive(state_dir, pkgname, release, config_path, keep=KEEP) -> Path | None
    previous(state_dir, pkgname, *, exclude_release=None) -> tuple[str, dict] | None
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

from sysforge.primitives.kernel_safety import parse_kconfig_text

# Newest N archives retained per pkgname. Five covers "compare against the
# handful of kernels I have actually run" without unbounded growth.
KEEP = 5

_DIRNAME = "kconfig-history"
_SUFFIX = ".config.gz"

# Release strings come from kbuild's kernel.release and pkgnames from
# kernel.toml; both reach the filesystem here, so both are constrained to a
# path-safe charset rather than trusted.
_SAFE = re.compile(r"[^A-Za-z0-9._+-]")


def _sanitize(part: str) -> str:
    return _SAFE.sub("_", str(part))


def history_dir(state_dir) -> Path:
    """The archive directory for ``state_dir`` (not created)."""
    return Path(state_dir) / _DIRNAME


def archive_path(state_dir, pkgname: str, release: str) -> Path:
    """Where the config for one (pkgname, release) pair lives."""
    return history_dir(state_dir) / f"{_sanitize(pkgname)}-{_sanitize(release)}{_SUFFIX}"


def _archives_for(state_dir, pkgname: str) -> list[Path]:
    """Existing archives for ``pkgname``, newest mtime first."""
    directory = history_dir(state_dir)
    if not directory.is_dir():
        return []
    prefix = f"{_sanitize(pkgname)}-"
    found = [
        p for p in directory.glob(f"*{_SUFFIX}")
        if p.name.startswith(prefix)
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def archive(state_dir, pkgname: str, release: str, config_path, keep: int = KEEP):
    """Copy a resolved ``.config`` into the archive, then prune to ``keep``.

    Returns the written path, or ``None`` if the source was unreadable or the
    write failed. Overwrites an existing entry for the same release — a rebuild
    at the same kernel release supersedes its predecessor rather than
    accumulating beside it.
    """
    try:
        text = Path(config_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    dest = archive_path(state_dir, pkgname, release)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return None

    # Prune after the write so a failed write never costs the operator an
    # older archive it would have replaced.
    for stale in _archives_for(state_dir, pkgname)[keep:]:
        try:
            stale.unlink()
        except OSError:  # noqa: PERF203 — one bad unlink must not stop the rest
            continue
    return dest


def previous(state_dir, pkgname: str, *, exclude_release: str | None = None):
    """Return ``(release, parsed_config)`` for the newest prior archive.

    ``exclude_release`` skips the entry for the build being reported on, which
    the caller has usually just written. ``None`` when there is no earlier
    config to compare against — a first build has nothing to diff, and saying
    so is the honest outcome.
    """
    skip = (
        archive_path(state_dir, pkgname, exclude_release).name
        if exclude_release else None
    )
    prefix_len = len(f"{_sanitize(pkgname)}-")
    for path in _archives_for(state_dir, pkgname):
        if path.name == skip:
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except (OSError, gzip.BadGzipFile, EOFError):
            continue
        release = path.name[prefix_len:-len(_SUFFIX)]
        return release, parse_kconfig_text(text)
    return None
