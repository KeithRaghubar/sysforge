# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update_split.py — split-pkgbase membership for ``sysforge update`` (3.2.0-B31).

``update`` tracks installed *pkgnames* but builds and reports by *pkgbase*.
When one member of a split ``-git`` pkgbase is replaced by its stock repo
package (``pacman -S adwaita-icon-theme`` displaces ``adwaita-icon-theme-git``)
the sibling (``adwaita-cursors-git``) stays installed and tracked — pacman
has no reason to touch it, since its ``provides`` still satisfies the stock
package's dependency. The pkgbase therefore stays in the update, reported
under the name of the package the user just removed.

This pass runs after the version check and annotates each source-built
result with the members actually driving it (``via``), any member displaced
by an installed stock repo package (``replaced_members``), and the stock
package that would finish the switch for each remaining member
(``stock_for_remaining``). Pure detection — it never changes what gets built
or installed; ``update_summary`` renders it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_state import BUILD_MODE_SOURCE
from sysforge.primitives.pkgbuild_meta import member_relations, parse_pkgbuild
from sysforge.update_common import _VCS_SUFFIXES
from sysforge.update_result import _UpdateResult

_log = log.get_logger("UPDATE")


@dataclass
class SplitMembers:
    via: list[str] = field(default_factory=list)
    replaced: dict[str, str] = field(default_factory=dict)
    stock_for_remaining: dict[str, str] = field(default_factory=dict)


def _in_sync_repo(name: str) -> bool:
    from sysforge.primitives.pacman import get_pacman_sync_version
    return get_pacman_sync_version(name) is not None


def _stock_candidates(parsed, member: str, declared: set[str]) -> list[str]:
    """Stock names ``member`` stands in for: its provides/conflicts plus the
    VCS-suffix-stripped name, minus the pkgbase's own members."""
    names = set(member_relations(parsed, member))
    for suffix in _VCS_SUFFIXES:
        if member.endswith(suffix):
            names.add(member[: -len(suffix)])
    return sorted(names - declared)


def detect_split_members(
    pkgbase: str,
    *,
    tracked: list[str],
    pkgbuild_path: Path,
    installed: dict[str, str],
    in_repo: Callable[[str], bool] = _in_sync_repo,
) -> SplitMembers:
    """Classify ``pkgbase``'s split members against the live install set.

    A declared member that is not installed counts as *replaced* only when
    one of its stock candidates is installed **and** carried by a sync repo —
    a member the user simply never installed (an optional ``-docs`` split) has
    no installed stock counterpart and is not flagged.
    """
    info = SplitMembers()
    parsed = parse_pkgbuild(pkgbuild_path)
    declared = parsed.get("globals", {}).get("pkgname") or []
    if isinstance(declared, str):
        declared = [declared]
    if len(declared) < 2:
        return info
    declared_set = set(declared)
    live = sorted(pn for pn in tracked if pn in installed)

    if pkgbase in declared_set and pkgbase not in installed and live:
        info.via = live

    for member in declared:
        if member in installed:
            continue
        for stock in _stock_candidates(parsed, member, declared_set):
            if stock in installed and in_repo(stock):
                info.replaced[member] = stock
                break
    if not info.replaced:
        return info

    for member in live:
        for stock in _stock_candidates(parsed, member, declared_set):
            if in_repo(stock):
                info.stock_for_remaining[member] = stock
                break
    return info


def annotate_split_members(
    results: list[_UpdateResult],
    pkgbase_entry: dict[str, dict],
    installed: dict[str, str],
    *,
    in_repo: Callable[[str], bool] = _in_sync_repo,
) -> None:
    """Fill ``via`` / ``replaced_members`` / ``stock_for_remaining`` in place.

    Only source-built pkgbases with a readable PKGBUILD are considered —
    pacman-class entries are never rebuilt, so their membership can't mislead.
    Best-effort: a PKGBUILD that fails to parse leaves its result untouched.
    """
    for r in results:
        entry = pkgbase_entry.get(r.pkgbase) or {}
        if entry.get("build_mode") != BUILD_MODE_SOURCE:
            continue
        pkgbuild_dir = entry.get("pkgbuild_dir")
        if not pkgbuild_dir:
            continue
        pkgbuild_path = Path(pkgbuild_dir) / "PKGBUILD"
        if not pkgbuild_path.is_file():
            continue
        try:
            info = detect_split_members(
                r.pkgbase, tracked=list(r.pkgnames), pkgbuild_path=pkgbuild_path,
                installed=installed, in_repo=in_repo,
            )
        except Exception as e:  # noqa: BLE001 — advisory pass; never fail the update
            _log.debug(f"{r.pkgbase}: split-member check skipped: {e}")
            continue
        r.via = info.via
        r.replaced_members = info.replaced
        r.stock_for_remaining = info.stock_for_remaining
        for member, stock in info.replaced.items():
            _log.warn(
                f"{r.pkgbase}: {member} was replaced by stock {stock}; "
                f"still tracked: {', '.join(r.pkgnames)}"
            )
