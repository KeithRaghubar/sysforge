# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
network_probe.py — network / connectivity diagnostics (doctor ``network`` axis).

Read-only checks of the live network configuration. **No active network calls** —
this never performs a DNS lookup or a ping (those would hang or flap with real
connectivity). It inspects local routing/manager/DNS *configuration* only:

  - no default route — ``ip route show default`` empty (the machine has no
    gateway; almost always a real connectivity problem).
  - **connection-manager ownership conflict** — more than one mutually-exclusive
    connection manager is *enabled* (NetworkManager + systemd-networkd +
    dhcpcd + …). Two managers fighting over the same interface is the network
    analogue of the audio axis's failed-unit class, and is **not** caught by the
    ``services`` axis (which only reports units already in the ``failed`` state —
    a conflict often presents as flapping, not a hard failure).
  - **DNS provisioner conflict** — ``systemd-resolved`` is active but
    ``/etc/resolv.conf`` is a static file rather than the resolved stub symlink,
    so resolved's configuration is being silently overridden.

Best-effort and false-positive-averse. An absent tool or a nonzero exit degrades
to **no findings** rather than a phantom "no network". Deliberately does *not*
flag "no connection manager enabled at all" — a host may use a manager we don't
enumerate, and a missed finding is recoverable while a false alarm is not.

Returns :class:`diagnostics.Finding` objects directly (category ``"network"``).
Mutates nothing — never enables/restarts a unit or rewrites resolv.conf.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from sysforge.primitives import diagnostics as diag

# Mutually-exclusive full connection managers. A correctly-configured host runs
# exactly one. (iwd is intentionally absent: it is commonly a NetworkManager
# backend rather than a standalone manager, so its presence is not a conflict.)
_CONNECTION_MANAGERS: tuple[str, ...] = (
    "NetworkManager.service",
    "systemd-networkd.service",
    "dhcpcd.service",
    "connman.service",
    "netctl.service",
)

_RESOLV_CONF = Path("/etc/resolv.conf")


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except (FileNotFoundError, OSError):
        return None


def _unit_enabled(unit: str) -> bool:
    """True iff ``systemctl is-enabled <unit>`` reports an enabled state.

    An absent unit exits nonzero (and prints ``not-found``/nothing) → False.
    """
    proc = _run(["systemctl", "is-enabled", unit])
    if proc is None:
        return False
    # is-enabled prints e.g. "enabled" / "enabled-runtime"; "static",
    # "disabled", "masked" and not-found are all not-enabled.
    return proc.stdout.strip().startswith("enabled")


def _resolved_active() -> bool:
    """True iff systemd-resolved is the active resolver."""
    proc = _run(["systemctl", "is-active", "systemd-resolved.service"])
    if proc is None:
        return False
    return proc.stdout.strip() == "active"


def _resolv_conf_state() -> tuple[str, str | None]:
    """Classify ``/etc/resolv.conf`` as ``("symlink", target)`` / ``("file",
    None)`` / ``("missing", None)``. Read-only stat; never opens the file."""
    try:
        if _RESOLV_CONF.is_symlink():
            try:
                return ("symlink", str(_RESOLV_CONF.readlink()))
            except OSError:
                return ("symlink", None)
        if _RESOLV_CONF.exists():
            return ("file", None)
    except OSError:
        return ("missing", None)
    return ("missing", None)


def _check_default_route() -> list[diag.Finding]:
    """`ip route show default` — warn when the host has no gateway.

    ip absent or a nonzero exit → no findings (don't guess about connectivity).
    """
    proc = _run(["ip", "route", "show", "default"])
    if proc is None or proc.returncode != 0:
        return []
    if "default" in proc.stdout:
        return []
    return [diag.Finding(
        "network", diag.SEV_WARN, "no_default_route",
        "no default route — the system has no gateway and cannot reach off-link "
        "hosts",
        remediation=("check link/DHCP: `ip route`, `ip link`; bring the "
                     "interface up or fix your connection manager"),
    )]


def _check_manager_conflict() -> list[diag.Finding]:
    """Warn when more than one mutually-exclusive connection manager is enabled.

    Read-only (`systemctl is-enabled`). Zero or one enabled → clean.
    """
    enabled = [m for m in _CONNECTION_MANAGERS if _unit_enabled(m)]
    if len(enabled) < 2:
        return []
    names = ", ".join(m.removesuffix(".service") for m in enabled)
    return [diag.Finding(
        "network", diag.SEV_WARN, "network_manager_conflict",
        f"multiple connection managers are enabled ({names}); they will fight "
        "over the same interfaces and routing",
        remediation=("keep exactly one; disable the others, e.g. "
                     "`systemctl disable --now <unit>`"),
    )]


def _check_dns_conflict() -> list[diag.Finding]:
    """Warn when systemd-resolved is active but /etc/resolv.conf is a static
    file overriding it. resolved inactive, a managed symlink, or a missing
    resolv.conf → clean/degrade."""
    if not _resolved_active():
        return []
    kind, _target = _resolv_conf_state()
    if kind != "file":
        # symlink (managed, or dangling — out of scope) or missing → no finding.
        return []
    return [diag.Finding(
        "network", diag.SEV_WARN, "dns_resolved_unmanaged",
        "systemd-resolved is active but /etc/resolv.conf is a static file, not "
        "the resolved stub symlink — resolved's DNS configuration is being "
        "overridden",
        remediation=("link resolv.conf to resolved's stub: `ln -sf "
                     "/run/systemd/resolve/stub-resolv.conf /etc/resolv.conf`"),
    )]


def collect_network_findings() -> list[diag.Finding]:
    """Run all network checks; return findings (read-only, never mutates)."""
    findings: list[diag.Finding] = []
    findings += _check_default_route()
    findings += _check_manager_conflict()
    findings += _check_dns_conflict()
    return findings
