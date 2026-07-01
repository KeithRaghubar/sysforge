# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
system_probe.py — pacman / system-integrity diagnostics (doctor ``pacman`` axis).

Read-only checks against the *local* pacman database and the filesystem — never
``-Sy`` or any sync, so a ``doctor`` run cannot mutate the package set:

  - ``pacman -Dk``  — local-db dependency/consistency check (missing deps).
  - stale ``db.lck`` — an interrupted pacman transaction left the lock behind.
  - ``*.pacnew`` / ``*.pacsave`` under ``/etc`` — unmerged config drift.
  - ``pacman -Qtdq`` — true orphans (unrequired dependency packages).

Returns ``diagnostics.Finding`` objects directly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from sysforge.primitives import diagnostics as diag

_PACMAN_DB_LOCK = Path("/var/lib/pacman/db.lck")
_ETC = Path("/etc")
# Cap the .pacnew/.pacsave sample so a long-neglected /etc doesn't flood output.
_PACNEW_SAMPLE = 12


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return None


def _check_db_consistency() -> list[diag.Finding]:
    """`pacman -Dk` — surface unsatisfied dependencies in the local db."""
    proc = _run(["pacman", "-Dk"])
    if proc is None:
        return []
    # -Dk prints per-package OK lines to stdout and problems to stderr; a
    # non-zero exit means at least one inconsistency. Collect the substantive
    # problem lines (not the "checking <pkg>" / "all OK" chatter).
    if proc.returncode == 0:
        return []
    findings: list[diag.Finding] = []
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip a leading "error: " / "warning: " marker for the message.
        body = line.split(":", 1)[1].strip() if line.startswith(("error:", "warning:")) else line
        findings.append(diag.Finding(
            "pacman", diag.SEV_ERROR, "pacman_db_inconsistent", body,
            remediation="resolve the missing dependency (install it) — the local "
                        "package database is inconsistent",
        ))
    if not findings:
        # Non-zero exit but nothing parseable — report generically.
        findings.append(diag.Finding(
            "pacman", diag.SEV_WARN, "pacman_db_check_failed",
            "`pacman -Dk` reported a database inconsistency",
            remediation="run `pacman -Dk` manually to see the details",
        ))
    return findings


def _check_stale_lock() -> list[diag.Finding]:
    """A lingering db.lck means a pacman transaction was interrupted."""
    if not _PACMAN_DB_LOCK.exists():
        return []
    return [diag.Finding(
        "pacman", diag.SEV_WARN, "pacman_db_locked",
        f"pacman database lock present at {_PACMAN_DB_LOCK} — a previous "
        "transaction may have been interrupted (or pacman is running now)",
        remediation=f"if no pacman is running, remove it: sudo rm {_PACMAN_DB_LOCK}",
    )]


def _check_pacfiles() -> list[diag.Finding]:
    """Unmerged ``*.pacnew`` / ``*.pacsave`` config files under /etc.

    Split by what ``pacdiff`` can actually act on: a pacfile whose *base* file
    (the path with the suffix stripped) still exists is mergeable — pacdiff can
    3-way merge it. A pacfile whose base is gone (typically an orphaned
    ``.pacsave`` left by a removed package) has nothing to merge against, so
    pacdiff no-ops on it; advising pacdiff there dead-ends (B10). Those need a
    manual review/remove instead.
    """
    found: list[Path] = []
    for pattern in ("*.pacnew", "*.pacsave"):
        try:
            found += list(_ETC.rglob(pattern))
        except (OSError, PermissionError):
            continue
    if not found:
        return []

    mergeable = sorted(str(p) for p in found if p.with_suffix("").exists())
    orphaned = sorted(str(p) for p in found if not p.with_suffix("").exists())

    out: list[diag.Finding] = []
    if mergeable:
        out.append(_pacfile_finding(
            mergeable, "pacnew_unmerged",
            "unmerged pacman config file(s) under /etc",
            "review and merge each with `pacdiff` (from pacman-contrib), "
            "then remove the .pacnew/.pacsave"))
    if orphaned:
        out.append(_pacfile_finding(
            orphaned, "pacsave_orphaned",
            "orphaned pacman config backup(s) under /etc (base file gone; "
            "pacdiff cannot merge these)",
            "these are leftovers from removed packages — review and delete "
            "each once you've salvaged any settings you need"))
    return out


def _pacfile_finding(files: list[str], check_id: str, summary: str,
                     remediation: str) -> diag.Finding:
    sample = "\n    ".join(files[:_PACNEW_SAMPLE])
    more = "" if len(files) <= _PACNEW_SAMPLE else f"\n    (+{len(files) - _PACNEW_SAMPLE} more)"
    return diag.Finding(
        "pacman", diag.SEV_WARN, check_id,
        f"{len(files)} {summary}:\n    {sample}{more}",
        remediation=remediation,
    )


def _check_orphans() -> list[diag.Finding]:
    """`pacman -Qtdq` — true orphans (unrequired dependency packages)."""
    proc = _run(["pacman", "-Qtdq"])
    if proc is None or proc.returncode != 0:
        # Non-zero / empty = no orphans (pacman exits 1 when the query is empty).
        return []
    orphans = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not orphans:
        return []
    sample = ", ".join(orphans[:12])
    more = "" if len(orphans) <= 12 else f" (+{len(orphans) - 12} more)"
    return [diag.Finding(
        "pacman", diag.SEV_INFO, "orphan_packages",
        f"{len(orphans)} orphan package(s) (installed as deps, now unrequired): "
        f"{sample}{more}",
        remediation="remove if unwanted: sudo pacman -Rns $(pacman -Qtdq)",
    )]


def collect_system_findings() -> list[diag.Finding]:
    """Run all pacman/system-integrity checks; return findings (read-only)."""
    findings: list[diag.Finding] = []
    findings += _check_db_consistency()
    findings += _check_stale_lock()
    findings += _check_pacfiles()
    findings += _check_orphans()
    return findings
