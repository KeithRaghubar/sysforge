# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Shared leaf for the update pipeline's cross-phase constants and predicates.

These symbols are needed by more than one update phase module (the version
check in ``update_version`` and the source-sync orchestration in ``update``),
so they live here — a pure leaf that imports only from the primitives layer —
to avoid a facade-import-order cycle. ``update.py`` re-exports them so existing
``from sysforge.update import _is_vcs`` call sites keep working.
"""

from sysforge.primitives.source_sync import (
    STATUS_FAILED, STATUS_FROZEN, STATUS_PURGE_REFUSED, STATUS_RATE_LIMITED,
)


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# Sync statuses that block the package from proceeding to build, and the
# user-facing action each maps to in the update summary. Statuses absent
# from this map (UP_TO_DATE, FETCHED, CLONED, DIVERGED, SKIPPED_OFFLINE,
# SKIPPED_NO_TRACKING) are non-blocking — the build proceeds against the
# local PKGBUILD.
_SYNC_STATUS_TO_ACTION = {
    STATUS_FAILED: "PULL_FAILED",
    STATUS_RATE_LIMITED: "RATE_LIMITED",
    STATUS_PURGE_REFUSED: "PURGE_REFUSED",
    STATUS_FROZEN: "FROZEN",
}
_SYNC_BLOCKING_STATUSES = frozenset(_SYNC_STATUS_TO_ACTION)
