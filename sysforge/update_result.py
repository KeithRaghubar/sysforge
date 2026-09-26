# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update_result.py — the per-package result type for ``sysforge update``.

A pure data type (no logic, no logging) shared between the version-check phase
that produces it, the summary phase that renders it, and the orchestrator that
collects it. Lives in its own leaf module so producer and consumer both import
it downward, with no import cycle back through ``update.py``.
"""
from dataclasses import dataclass, field
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
    # 3.0.0-B9: set when the rebuild was promoted by drift detection rather
    # than a version bump. Consumed by ``build_core``'s loop as a per-target
    # makepkg ``-f`` — a drift rebuild runs at an unchanged pkgver, so the
    # matching artifact is still in PKGDEST and makepkg would skip the build.
    force_rebuild: bool = False
    # 3.2.0-B31: split-pkgbase membership, filled by
    # ``update_split.annotate_split_members`` after the version check.
    # ``via`` lists the tracked members driving the pkgbase when the pkgbase's
    # namesake member is not installed (so the summary never labels a rebuild
    # with a package that is gone); ``replaced_members`` maps each member
    # displaced by an installed stock repo package to that package, and
    # ``stock_for_remaining`` maps each still-tracked member to the stock repo
    # package that would finish the switch.
    via: list = field(default_factory=list)
    replaced_members: dict = field(default_factory=dict)
    stock_for_remaining: dict = field(default_factory=dict)
