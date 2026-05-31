"""
runtime_probe.py — services / runtime-health diagnostics (doctor ``services`` axis).

Read-only checks of the live system's service and driver runtime state:

  - ``systemctl --failed`` — units that failed to start.
  - missing firmware for bound drivers — best-effort parse of the current boot's
    kernel ring buffer (``journalctl -k -b``) for "Direct firmware load … failed".

DKMS health is checked in the ``boot`` axis (``kernel_safety.check_dkms_for_kernel``
for the running kernel), not here, to avoid double-reporting.

Returns ``diagnostics.Finding`` objects directly. Every external command is
guarded so an absent tool or permission error yields no findings rather than a
crash (``run_axes`` also isolates exceptions as a backstop).
"""
from __future__ import annotations

import re
import subprocess

from sysforge.primitives import diagnostics as diag

# "Direct firmware load for <name> failed with error -2" /
# "firmware: failed to load <name> (-2)"
_RE_FW_FAILED = re.compile(
    r"(?:Direct firmware load for|firmware:\s*failed to load)\s+(\S+)",
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return None


def _check_failed_units() -> list[diag.Finding]:
    """`systemctl --failed` — each failed unit becomes a finding."""
    proc = _run(["systemctl", "--failed", "--no-legend", "--plain"])
    if proc is None:
        return []
    findings: list[diag.Finding] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if not unit.endswith((".service", ".socket", ".mount", ".timer",
                              ".target", ".path", ".device", ".scope", ".slice")):
            continue
        findings.append(diag.Finding(
            "services", diag.SEV_ERROR, f"failed_unit:{unit}",
            f"systemd unit '{unit}' is in failed state",
            remediation=f"inspect: systemctl status {unit}; journalctl -u {unit}",
        ))
    return findings


def _check_missing_firmware() -> list[diag.Finding]:
    """Best-effort: kernel firmware-load failures from the current boot's log."""
    proc = _run(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"])
    if proc is None or proc.returncode != 0 or not proc.stdout:
        return []
    missing: list[str] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        m = _RE_FW_FAILED.search(line)
        if not m:
            continue
        fw = m.group(1).rstrip(":,").strip("'\"")
        if fw and fw not in seen:
            seen.add(fw)
            missing.append(fw)
    if not missing:
        return []
    sample = ", ".join(missing[:10])
    more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
    return [diag.Finding(
        "services", diag.SEV_WARN, "missing_firmware",
        f"{len(missing)} firmware file(s) a driver requested but could not load "
        f"this boot: {sample}{more}",
        remediation="install the matching firmware package (e.g. `linux-firmware` "
                    "or a vendor package); harmless if the device is unused",
    )]


def collect_runtime_findings() -> list[diag.Finding]:
    """Run all services/runtime checks; return findings (read-only)."""
    findings: list[diag.Finding] = []
    findings += _check_failed_units()
    findings += _check_missing_firmware()
    return findings
