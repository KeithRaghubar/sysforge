"""
test_doctor_roster.py — every doctor axis reports which checks it ran and
which it skipped (3.4.0-F5).

Before this, only ``graphics``/``gfxperf`` carried a roster; every other axis
returned a bare list, so ``-vvv`` printed "roster: not reported by this axis"
and a probe that bailed out looked identical to one that passed.
"""
import inspect
from types import SimpleNamespace

import pytest

from sysforge import doctor
from sysforge.primitives import diagnostics as diag


def _args(**kw):
    return SimpleNamespace(**{"packages": None, "derived_targets": None,
                              "state_dir": None, "distro": False, **kw})


# ---------------------------------------------------------------------------
# Structural: no axis is left on the bare-list contract
# ---------------------------------------------------------------------------

_PRODUCERS = {
    "distro": doctor._collect_distro_findings,
    "toolchain": doctor._collect_toolchain_findings,
    "rust": doctor._collect_rust_findings,
    "cache": doctor._collect_cache_findings,
    "hardware": doctor._collect_hardware_findings,
    "graphics": doctor._collect_graphics_findings,
    "gfxperf": doctor._collect_gfxperf_findings,
    "pacman": doctor._collect_pacman_findings,
    "state": doctor._collect_state_findings,
    "boot": doctor._collect_boot_findings,
    "restart": doctor._collect_restart_findings,
    "storage": doctor._collect_storage_findings,
    "services": doctor._collect_services_findings,
    "audio": doctor._collect_audio_findings,
    "network": doctor._collect_network_findings,
    "integrity": doctor._collect_integrity_findings,
    "abi": doctor._abi_axis_result,
}


def test_every_registered_axis_has_a_roster_producer():
    assert set(_PRODUCERS) == set(doctor._SYSTEM_AXIS_ORDER)


@pytest.mark.parametrize("name", sorted(_PRODUCERS))
def test_producer_returns_an_axis_result(name):
    ann = inspect.signature(_PRODUCERS[name]).return_annotation
    assert ann in (diag.AxisResult, "diag.AxisResult"), name


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def test_run_checks_files_lists_singles_none_and_skips():
    f = diag.Finding("x", diag.SEV_WARN, "c", "m")
    res = diag.run_checks([
        ("many", lambda: [f, f]),
        ("one", lambda: f),
        ("clean", lambda: None),
        ("empty", lambda: []),
        ("nope", lambda: diag.Skip("tool missing")),
    ])
    assert res.findings == [f, f, f]
    assert res.roster.ran == ["many", "one", "clean", "empty"]
    assert res.roster.skipped == [("nope", "tool missing")]


# ---------------------------------------------------------------------------
# Behaviour: a bail-out is a skip, not a pass
# ---------------------------------------------------------------------------

def test_services_journal_unreadable_is_skipped(monkeypatch):
    from sysforge.primitives import runtime_probe
    monkeypatch.setattr(runtime_probe, "_run", lambda cmd: SimpleNamespace(
        returncode=0 if cmd[0] == "systemctl" else 1, stdout="", stderr=""))
    res = doctor._collect_services_findings()
    assert res.roster.ran == ["failed_units"]
    assert {c for c, _ in res.roster.skipped} == {"missing_firmware", "boot_errors"}


def test_hardware_without_running_config_skips_the_audit(monkeypatch):
    from sysforge.primitives import dep_analysis, device_probe
    monkeypatch.setattr(device_probe, "enumerate_devices", lambda: [])
    monkeypatch.setattr(device_probe, "check_unsupported_devices", lambda devices: [])
    monkeypatch.setattr(dep_analysis, "_parse_kernel_config", lambda: {})
    res = doctor._collect_hardware_findings()
    assert res.roster.ran == ["device_drivers"]
    assert res.roster.skipped[0][0] == "kernel_boot_config"


def test_rust_pins_do_not_apply_without_targets(monkeypatch):
    from sysforge.primitives import rust_probe
    monkeypatch.setattr(rust_probe, "collect_active_findings", lambda: [])
    res = doctor._collect_rust_findings({}, _args())
    assert res.roster.ran == ["active_toolchain"]
    assert res.roster.skipped == [("toolchain_pins", "no package targets named")]


def test_state_unreadable_skips_every_dependent_check(monkeypatch):
    from sysforge.primitives import paths

    def _boom(_):
        raise OSError("denied")
    monkeypatch.setattr(paths, "resolve_state_dir", _boom)
    res = doctor._collect_state_findings(_args())
    assert [f.check_id for f in res.findings] == ["state:unreadable"]
    assert {c for c, _ in res.roster.skipped} == {
        "build_failures", "stage_sentinel", "pgo_record_only", "state_drift"}


def test_abi_with_nothing_to_scan_is_skipped():
    walk = SimpleNamespace(run=lambda: [], scanned=0)
    res = doctor._abi_axis_result(walk)
    assert res.roster.skipped[0][0] == "depends_and_linkage"


def test_render_no_longer_says_not_reported(monkeypatch, capsys):
    """The -vvv line every unmigrated axis used to print is gone."""
    from sysforge import log
    from sysforge.primitives import network_probe
    monkeypatch.setattr(network_probe, "_run", lambda cmd: None)
    res = diag.run_axis(diag.Axis("network", "network", doctor._collect_network_findings,
                                  clean_msg="ok"))
    saved = log._VERBOSITY
    log.set_verbosity(3)
    try:
        diag.render_axis(doctor._log, "network", res.findings, clean_msg="ok",
                         quiet=False, roster=res.roster)
    finally:
        log.set_verbosity(saved)
    out = "".join(capsys.readouterr())
    assert "not reported by this axis" not in out
    assert "default_route" in out
