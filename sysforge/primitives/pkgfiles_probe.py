# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pkgfiles_probe.py — package-file integrity checks for `doctor --integrity`.

Read-only verification that package-owned files still match alpm's stored
mtree, via `pacman -Qkk`. The deliberate complement to the artifact inventory
(`primitives/artifacts.py`), which manages only *user-authored* content and
excludes package-owned files at discovery — the two populations are disjoint.

`pacman -Qkk` (pacman 7.x, `LC_ALL=C`) emits, per discrepancy:

    [backup file: | warning: ] <pkg>: <path> (<Reason>)

plus a per-package summary line (`<pkg>: N total files, M altered file`) that
carries no path and is ignored. Reason strings are a small closed vocabulary
(Modification time / Size / SHA256 checksum / File type / Permissions / UID /
GID mismatch, and a missing-file `No such file …` report). pacman prefixes
discrepancies on files in a package's `backup` array with `backup file: ` and
excludes them from its own altered count — those are expected admin edits, not
integrity drift, and we mirror that classification.

Run unprivileged (the intended mode), pacman cannot read root-only files and
emits access-error reasons instead (`failed to calculate SHA256 checksum`,
`Permission denied`). These are stripped before classification: a path whose
reasons are *only* access errors is access-limited, not drift — it is counted
and rolled into a single `integrity_partial_coverage` info advisory rather
than emitted as a per-path finding. A path with genuine signal alongside an
access error (e.g. `Size mismatch` from a successful stat) keeps its real
drift classification, with the access-error reason dropped from the message.

Never mutates, never syncs, never escalates privilege (`-Qkk` is unprivileged).
Advisory only: findings carry `pacman -S <pkg>` remediation text but no
`fix_cmd`.
"""
from __future__ import annotations

import os
import re
import subprocess

from sysforge.primitives import diagnostics as diag
from sysforge.primitives.diagnostics import Finding

# [prefix] <pkg>: <path starting with /> (<Reason>). The summary line has no
# `(...)` and its "path" does not start with `/`, so it never matches.
_LINE_RE = re.compile(
    r"^(?:(?P<backup>backup file: )|(?:warning: ))?"
    r"(?P<pkg>[^:]+): (?P<path>/[^(]*\S) \((?P<reason>[^)]+)\)\s*$"
)

_MTIME_REASON = "Modification time mismatch"

_ACCESS_ERROR_MARKERS = ("failed to calculate", "Permission denied")


def _is_access_error(reason: str) -> bool:
    return any(m in reason for m in _ACCESS_ERROR_MARKERS)


def _run(packages: list[str] | None) -> subprocess.CompletedProcess | None:
    cmd = ["pacman", "-Qkk"]
    if packages:
        cmd += list(packages)
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (FileNotFoundError, OSError):
        return None


def _classify(is_backup: bool, reasons: set[str]) -> tuple[str, str]:
    """(severity, check_id) for a path given its set of discrepancy reasons."""
    if is_backup:
        return diag.SEV_INFO, "integrity_backup_edited"
    if any("No such file" in r for r in reasons):
        return diag.SEV_ERROR, "integrity_missing"
    # Present, non-backup: a modification-time-only deviation is a benign
    # identical rewrite (size + hash still match).
    if reasons == {_MTIME_REASON}:
        return diag.SEV_INFO, "integrity_altered"
    return diag.SEV_WARN, "integrity_altered"


def collect_integrity_findings(
    packages: list[str] | None = None,
) -> list[Finding]:
    """Verify package-owned files via `pacman -Qkk` (optionally scoped).

    One Finding per drifted path, severity = worst discrepancy. Read-only.
    """
    proc = _run(packages)
    if proc is None:
        return [Finding(
            "integrity", diag.SEV_WARN, "integrity_unavailable",
            "could not run `pacman -Qkk` (pacman not found)",
        )]

    paths: dict[str, dict] = {}
    for stream in (proc.stdout or "", proc.stderr or ""):
        for line in stream.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            entry = paths.setdefault(m.group("path"), {
                "pkg": m.group("pkg"), "is_backup": False, "reasons": set(),
            })
            if m.group("backup"):
                entry["is_backup"] = True
            entry["reasons"].add(m.group("reason"))

    if not paths:
        # rc 0/1 with no discrepancy lines → genuinely clean (1 can mean a
        # scoped pkg not found; false-positive-averse, we stay silent). Any
        # other exit with nothing parsed → the tool misbehaved: degrade.
        if proc.returncode not in (0, 1):
            return [Finding(
                "integrity", diag.SEV_WARN, "integrity_unavailable",
                "`pacman -Qkk` produced no parseable output",
            )]
        return []

    findings: list[Finding] = []
    unreadable = 0
    for path in sorted(paths):
        entry = paths[path]
        real = {r for r in entry["reasons"] if not _is_access_error(r)}
        if not real:
            # Path had ONLY access-error reasons — access-limited, not drift.
            unreadable += 1
            continue
        sev, check_id = _classify(entry["is_backup"], real)
        reasons = ", ".join(sorted(real))
        verb = "missing" if check_id == "integrity_missing" else "altered"
        findings.append(Finding(
            "integrity", sev, check_id,
            f"{entry['pkg']}: {path} {verb} ({reasons})",
            remediation=f"restore from the package with `pacman -S {entry['pkg']}`",
        ))
    if unreadable:
        findings.append(Finding(
            "integrity", diag.SEV_INFO, "integrity_partial_coverage",
            f"{unreadable} package-owned file(s) were unreadable without root; "
            f"coverage is partial",
            remediation="re-run `sudo sysforge doctor --integrity` for full coverage",
        ))
    return findings
