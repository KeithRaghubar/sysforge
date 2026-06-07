"""
update_result.py — the per-package result type for ``sysforge update``.

A pure data type (no logic, no logging) shared between the version-check phase
that produces it, the summary phase that renders it, and the orchestrator that
collects it. Lives in its own leaf module so producer and consumer both import
it downward, with no import cycle back through ``update.py``.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _UpdateResult:
    pkgbase: str
    pkgnames: list
    # Actions: UP_TO_DATE, NEEDS_REBUILD, NEEDS_PACMAN_UPGRADE, DEVEL,
    # DEVEL_EVAL_FAILED, DOWNGRADE, PULL_FAILED, RATE_LIMITED, PURGE_REFUSED,
    # SKIPPED_NO_CHECKUPDATES.
    action: str
    installed_ver: str | None
    pkgbuild_ver: str | None
    pkgbuild_path: Path | None
    has_build_record: bool = True
    source: str | None = None
