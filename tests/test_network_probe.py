"""
test_network_probe.py — network / connectivity probe (doctor ``network`` axis).

``ip`` / ``systemctl`` and the /etc/resolv.conf state are stubbed; no real
network or filesystem access. The probe is read-only and best-effort: an absent
tool or a nonzero exit must degrade to no findings (never a false "no network").
It must also never make a live network call (no DNS lookups, no pings).
"""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import network_probe


def _proc(stdout="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=rc)


# --- default route ----------------------------------------------------------

def test_no_default_route_warns(monkeypatch):
    monkeypatch.setattr(network_probe, "_run", lambda cmd: _proc(stdout="", rc=0))
    findings = network_probe._check_default_route()
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "no_default_route"
    assert f.severity == diag.SEV_WARN
    assert f.category == "network"


def test_default_route_present_is_clean(monkeypatch):
    out = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"
    monkeypatch.setattr(network_probe, "_run", lambda cmd: _proc(stdout=out))
    assert network_probe._check_default_route() == []


def test_default_route_no_ip_tool_degrades(monkeypatch):
    monkeypatch.setattr(network_probe, "_run", lambda cmd: None)
    assert network_probe._check_default_route() == []


def test_default_route_nonzero_exit_degrades(monkeypatch):
    monkeypatch.setattr(network_probe, "_run", lambda cmd: _proc(stdout="", rc=1))
    assert network_probe._check_default_route() == []


# --- connection-manager ownership conflict ----------------------------------

def test_two_enabled_managers_conflict(monkeypatch):
    enabled = {"NetworkManager.service", "systemd-networkd.service"}
    monkeypatch.setattr(network_probe, "_unit_enabled", lambda u: u in enabled)
    findings = network_probe._check_manager_conflict()
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "network_manager_conflict"
    assert f.severity == diag.SEV_WARN
    # Both conflicting managers are named in the message.
    assert "NetworkManager" in f.message
    assert "systemd-networkd" in f.message


def test_single_enabled_manager_is_clean(monkeypatch):
    enabled = {"NetworkManager.service"}
    monkeypatch.setattr(network_probe, "_unit_enabled", lambda u: u in enabled)
    assert network_probe._check_manager_conflict() == []


def test_no_enabled_manager_is_clean(monkeypatch):
    # Nothing enabled (or none of the units exist) → no conflict, no false alarm.
    monkeypatch.setattr(network_probe, "_unit_enabled", lambda u: False)
    assert network_probe._check_manager_conflict() == []


# --- DNS provisioner conflict -----------------------------------------------

def test_resolved_active_but_static_resolv_conf_warns(monkeypatch):
    monkeypatch.setattr(network_probe, "_resolved_active", lambda: True)
    monkeypatch.setattr(network_probe, "_resolv_conf_state",
                        lambda: ("file", None))
    findings = network_probe._check_dns_conflict()
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "dns_resolved_unmanaged"
    assert f.severity == diag.SEV_WARN
    assert f.category == "network"


def test_resolved_active_with_stub_symlink_is_clean(monkeypatch):
    monkeypatch.setattr(network_probe, "_resolved_active", lambda: True)
    monkeypatch.setattr(
        network_probe, "_resolv_conf_state",
        lambda: ("symlink", "/run/systemd/resolve/stub-resolv.conf"))
    assert network_probe._check_dns_conflict() == []


def test_resolved_inactive_is_clean(monkeypatch):
    # resolved not running → /etc/resolv.conf being a static file is expected.
    monkeypatch.setattr(network_probe, "_resolved_active", lambda: False)
    monkeypatch.setattr(network_probe, "_resolv_conf_state",
                        lambda: ("file", None))
    assert network_probe._check_dns_conflict() == []


def test_resolved_active_resolv_conf_missing_degrades(monkeypatch):
    monkeypatch.setattr(network_probe, "_resolved_active", lambda: True)
    monkeypatch.setattr(network_probe, "_resolv_conf_state",
                        lambda: ("missing", None))
    assert network_probe._check_dns_conflict() == []


# --- combiner ---------------------------------------------------------------

def test_collect_network_findings_combines(monkeypatch):
    monkeypatch.setattr(network_probe, "_run", lambda cmd: _proc(stdout="", rc=0))
    enabled = {"NetworkManager.service", "dhcpcd.service"}
    monkeypatch.setattr(network_probe, "_unit_enabled", lambda u: u in enabled)
    monkeypatch.setattr(network_probe, "_resolved_active", lambda: True)
    monkeypatch.setattr(network_probe, "_resolv_conf_state",
                        lambda: ("file", None))
    findings = network_probe.collect_network_findings()
    ids = {f.check_id for f in findings}
    assert ids == {"no_default_route", "network_manager_conflict",
                   "dns_resolved_unmanaged"}
    assert all(f.category == "network" for f in findings)
