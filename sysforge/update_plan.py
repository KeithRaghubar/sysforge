# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update_plan.py — what one ``sysforge update`` run is about to change (3.1.0-F4).

Pure model and renderer behind the confirmation gate (``update._phase_confirm``)
and the text ``--dry-run`` answer, so both show the same list. The header line
states the protections the run will build under (review, sandbox, freeze, AUR)
alongside what it will change.

Public API:
    PlanRow, Plan
    posture_line(review, sandbox, net) -> str
    profiles_override_sandbox(config) -> bool
    build_plan(*, to_build, pending_pacman_upgrade, sysupgrade_pending,
               promoted, review_marks, sandbox_marks, posture) -> Plan
    render_plan(plan) -> list[str]
"""
from __future__ import annotations

from dataclasses import dataclass

from sysforge.primitives import render


@dataclass(frozen=True)
class PlanRow:
    verb: str          # "rebuild" | "upgrade" | "sysupgrade"
    name: str
    detail: str
    marks: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    posture: str
    rows: tuple[PlanRow, ...]

    def is_empty(self) -> bool:
        return not self.rows


def _sandbox_short(policy) -> str:
    if not policy.enabled:
        return "off"
    bits = [name for name, on in (("clean", policy.clean), ("update", policy.update),
                                  ("local-repo", policy.local_repo)) if on]
    return f"on ({', '.join(bits)})" if bits else "on"


def posture_line(review, sandbox, net) -> str:
    if net.frozen:
        freeze = "on" + (f" (thawed: {', '.join(sorted(net.thawed))})" if net.thawed else "")
    else:
        freeze = "off"
    return " | ".join([
        f"review: {review.describe()}",
        f"sandbox: {_sandbox_short(sandbox)}",
        f"freeze: {freeze}",
        f"AUR: {'on' if net.aur_enabled else 'off'}",
    ])


def profiles_override_sandbox(config: dict) -> bool:
    """Whether any profile, or [defaults], sets ``sandbox_builds``. When none
    does, no package can differ from the global policy, and the plan skips
    resolving profiles altogether."""
    if "sandbox_builds" in (config.get("defaults") or {}):
        return True
    return any(isinstance(p, dict) and "sandbox_builds" in p
               for p in (config.get("profiles") or {}).values())


def _pair(r) -> str:
    if r.installed_ver and r.pkgbuild_ver:
        return render.version_pair(r.installed_ver, r.pkgbuild_ver, equal_marker=False)
    return r.pkgbuild_ver or ""


def build_plan(*, to_build, pending_pacman_upgrade, sysupgrade_pending, promoted,
               review_marks, sandbox_marks, posture) -> Plan:
    rows: list[PlanRow] = []
    for r in to_build:
        detail = promoted.get(r.pkgbase) or _pair(r)
        marks = tuple(m for m in ("review" if r.pkgbase in review_marks else None,
                                  sandbox_marks.get(r.pkgbase)) if m)
        rows.append(PlanRow("rebuild", r.pkgbase, detail, marks))
    for r in pending_pacman_upgrade:
        rows.append(PlanRow("upgrade", r.pkgbase, _pair(r)))
    if sysupgrade_pending:
        # No count: before -Sy the local sync databases are stale.
        rows.append(PlanRow("sysupgrade", "pacman -Syu",
                            "repo packages; pacman lists them as it runs"))
    return Plan(posture=posture, rows=tuple(rows))


def render_plan(plan: Plan) -> list[str]:
    lines = [f"{render.tag_header('UPDATE')}Plan: {plan.posture}"]
    if plan.is_empty():
        return lines + ["    nothing to change"]
    width = max(len(r.name) for r in plan.rows)
    for r in plan.rows:
        marks = f"  ({'; '.join(r.marks)})" if r.marks else ""
        lines.append(f"    {r.verb:<10} {r.name:<{width}}  {r.detail}{marks}")
    return lines
