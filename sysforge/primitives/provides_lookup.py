# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
provides_lookup.py — reverse-lookup which packages own a soname file

Thin wrapper around `pacman -F` for `sysforge doctor --suggest` so the
user can see which package would satisfy an unsatisfied soname or a
missing NEEDED runtime library.

Public API:
    files_db_present()                                       -> bool
    suggest_for_soname(entry, *, lib32=False, run_fn=None,
                       installed_names=None)                 -> list[str]
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.privilege import privileged_argv

_log = log.get_logger("PROV")

# Accepts both a raw depends entry (libfoo.so[=N[.M][-ARCH]]) and an
# already-resolved soname (libfoo.so.2). Same shape as dep_analysis._SONAME_RE
# but kept local so this module has no cross-dependency.
_SONAME_ENTRY_RE = re.compile(
    r"^(?P<base>\S+\.so)(?:=(?P<ver>[^-=\s]+))?(?:-(?P<arch>\d+))?$"
)

_FILES_DB_DIR = Path("/var/lib/pacman/sync")


def files_db_present() -> bool:
    """True if the pacman files db has been synced at least once."""
    if not _FILES_DB_DIR.is_dir():
        return False
    return any(_FILES_DB_DIR.glob("*.files"))


def sync_files_db() -> bool:
    """Sync the pacman files database (``sudo pacman -Fy``).

    Returns True on success. This is **install-bearing** (touches the system
    via sudo); read-only callers such as ``doctor`` must use
    :func:`files_db_present` to gate behaviour and must not call this. The
    intended caller is the reconfigure editor-install flow, which needs the
    files db to map an editor binary to its providing package.
    """
    if not shutil.which("pacman"):
        return False
    try:
        result = subprocess.run(privileged_argv(["pacman", "-Fy"]))
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _soname_query_path(entry: str, lib32: bool) -> str | None:
    """
    Translate a soname depends entry or resolved soname to a partial file
    path suitable for `pacman -Fq`. Returns None for non-soname inputs.
    """
    m = _SONAME_ENTRY_RE.match(entry)
    if m:
        base = m.group("base")
        ver = m.group("ver")
        soname = f"{base}.{ver}" if ver else base
    elif ".so" in entry:
        soname = entry
    else:
        return None
    libdir = "usr/lib32" if lib32 else "usr/lib"
    return f"{libdir}/{soname}"


def _bare_pkgname(candidate: str) -> str:
    """Strip the optional ``repo/`` prefix that `pacman -Fq` emits."""
    return candidate.split("/", 1)[1] if "/" in candidate else candidate


def suggest_for_soname(entry: str, *, lib32: bool = False,
                       run_fn=None,
                       installed_names: set[str] | None = None) -> list[str]:
    """
    Return deduped ``repo/pkgname`` candidates whose files db entries match
    the soname. Empty list if no match or the entry isn't a soname.
    Callers should gate on files_db_present() so stale-db messaging is
    surfaced once rather than per-issue.

    When ``installed_names`` is provided, candidates already present in the
    set (compared by bare pkgname, with the ``repo/`` prefix stripped) are
    dropped — useful for "install candidate" suggestions which shouldn't
    re-recommend packages the user already has installed.
    """
    if run_fn is None:
        run_fn = subprocess.run

    query = _soname_query_path(entry, lib32)
    if query is None:
        return []

    try:
        result = run_fn(
            ["pacman", "-Fq", query],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        _log.warn("pacman not found — cannot suggest candidates")
        return []

    # 0 = hits; 1 = no match (normal); other = error, skip.
    if result.returncode not in (0, 1):
        return []

    seen: set[str] = set()
    out: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("error:"):
            continue
        if line in seen:
            continue
        seen.add(line)
        if installed_names and _bare_pkgname(line) in installed_names:
            continue
        out.append(line)
    return out
