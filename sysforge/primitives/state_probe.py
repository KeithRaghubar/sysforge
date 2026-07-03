# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
state_probe.py — sysforge state-integrity diagnostics (doctor ``state`` axis).

Read-only inspection of sysforge's *own* persisted state:

  - recorded build failures (``build_state.toml`` ``[failures]`` table),
  - an active / interrupted stage sentinel (``stage_in_progress.toml``),
  - drift between the build-state mirror and the live pacman database.

This is the read-only counterpart to the recovery machinery: it never calls
``BuildState.save()`` and never calls the *recovering*
``stage_sentinel.check_and_recover_stale_sentinel`` — it only reports via
``StageSentinel.get_active()``. Returns ``diagnostics.Finding`` objects directly
(``diagnostics`` lives in the primitives layer, so no adapter is needed).

The last source-sync ``STATUS_*`` is intentionally *not* surfaced here: the
source-sync scheduler cache is per-process, so a standalone ``doctor`` run never
has sync results to report.
"""
from __future__ import annotations

from pathlib import Path

from sysforge.primitives import diagnostics as diag


def _short_error(text: str, limit: int = 160) -> str:
    """Collapse a stored failure ``error`` to a single readable line."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def collect_state_findings(state_dir: Path | str | None = None,
                           installed: dict[str, str] | None = None,
                           ) -> list[diag.Finding]:
    """Return state-integrity findings. All inputs are optional for testability.

    ``state_dir`` resolves through the usual ``SYSFORGE_STATE_DIR`` chain when
    ``None``; ``installed`` is fetched via ``pacman -Q`` when ``None``.
    """
    findings: list[diag.Finding] = []

    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives.build_state import BuildState

    try:
        resolved_dir, _ = resolve_state_dir(state_dir)
        bs = BuildState(resolved_dir)
    except Exception as exc:  # state dir unreadable / malformed
        return [diag.Finding(
            "state", diag.SEV_WARN, "state:unreadable",
            f"could not read sysforge state: {exc}",
            remediation="check SYSFORGE_STATE_DIR and build_state.toml integrity",
        )]

    # --- recorded build failures -------------------------------------------
    for pkgbase, rec in sorted(bs.all_failures().items()):
        msg = _short_error(rec.get("error", ""))
        when = rec.get("failed_at", "?")
        fix_cmd = rec.get("fix_cmd")
        signature = rec.get("signature")
        detail = f"{pkgbase} build failed ({when})"
        if signature:
            detail += f" [{signature}]"
        if msg:
            detail += f": {msg}"
        remediation = fix_cmd or (
            f"inspect with `sysforge log {pkgbase}`; clears on next successful build"
        )
        # Intentional-revert escape hatch: the warning is correct but
        # dead-ends a user who deliberately went back to the repo package.
        remediation += (
            f"; if you intentionally reverted to the repo package, "
            f"`sysforge state forget {pkgbase}` stops tracking it"
        )
        findings.append(diag.Finding(
            "state", diag.SEV_WARN, f"build_failure:{pkgbase}", detail,
            remediation=remediation, fix_cmd=fix_cmd,
            auto_remediable=False,
        ))

    # --- interrupted stage sentinel (read-only) ----------------------------
    try:
        from sysforge.primitives.stage_sentinel import StageSentinel
        record = StageSentinel(resolved_dir).get_active()
    except Exception:
        record = None
    if record:
        stage = record.get("stage", "?")
        started = record.get("started_at", "?")
        recovery_cmd = record.get("recovery_cmd")
        extra = ", ".join(
            f"{k}={v}" for k, v in sorted(record.items())
            if k not in ("stage", "started_at", "recovery_cmd")
        )
        msg = f"interrupted stage '{stage}' (started {started}) never completed"
        if extra:
            msg += f" — {extra}"
        remediation = recovery_cmd or (
            "verify the system, then clear the sentinel; the next install-bearing "
            "command will also offer recovery"
        )
        findings.append(diag.Finding(
            "state", diag.SEV_ERROR, "stale_sentinel", msg,
            remediation=remediation, fix_cmd=recovery_cmd,
        ))

    # --- build-state drift vs pacman (read-only) ---------------------------
    if installed is None:
        try:
            from sysforge.primitives import pacman
            installed = pacman.get_all_installed_packages()
        except Exception:
            installed = None
    if installed is not None:
        recorded = set(bs.all_packages().keys())
        zombies = recorded - set(installed.keys())
        if zombies:
            sample = ", ".join(sorted(zombies)[:8])
            more = "" if len(zombies) <= 8 else f" (+{len(zombies) - 8} more)"
            findings.append(diag.Finding(
                "state", diag.SEV_INFO, "state_drift:zombies",
                f"{len(zombies)} build_state entr"
                f"{'y' if len(zombies) == 1 else 'ies'} for packages no longer "
                f"installed: {sample}{more}",
                remediation="run `sysforge update` to reconcile the state mirror",
            ))

    return findings
