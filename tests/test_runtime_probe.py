"""
test_runtime_probe.py — services / runtime-health probe (doctor ``services`` axis).

systemctl and journalctl are stubbed; no real systemd access.
"""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import runtime_probe


def _proc(stdout="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=rc)


def _dispatch(mapping):
    """Return a fake _run that dispatches on cmd[0]."""
    def run(cmd):
        return mapping.get(cmd[0])
    return run


# --- failed units -----------------------------------------------------------

def test_failed_units_reported_as_errors(monkeypatch):
    out = (
        "fancontrol.service loaded failed failed fan control daemon\n"
        "sensord.service    loaded failed failed sensor logging\n"
    )
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"systemctl": _proc(stdout=out)}))
    findings = runtime_probe._check_failed_units()
    ids = {f.check_id for f in findings}
    assert ids == {"failed_unit:fancontrol.service", "failed_unit:sensord.service"}
    assert all(f.severity == diag.SEV_ERROR for f in findings)


def test_failed_units_ignores_non_unit_lines(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"systemctl": _proc(stdout="0 loaded units listed.\n")}))
    assert runtime_probe._check_failed_units() == []


def test_failed_units_no_systemctl(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run", _dispatch({}))
    assert runtime_probe._check_failed_units() == []


# --- missing firmware -------------------------------------------------------

def test_missing_firmware_parsed_and_deduped(monkeypatch):
    log = (
        "Direct firmware load for nvidia/ad102.bin failed with error -2\n"
        "some unrelated line\n"
        "Direct firmware load for nvidia/ad102.bin failed with error -2\n"
        "firmware: failed to load amdgpu/foo.bin (-2)\n"
    )
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout=log, rc=0)}))
    findings = runtime_probe._check_missing_firmware()
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "missing_firmware"
    assert f.severity == diag.SEV_WARN
    assert "2 firmware file(s)" in f.message
    assert "nvidia/ad102.bin" in f.message
    assert "amdgpu/foo.bin" in f.message


def test_missing_firmware_none_when_clean(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout="all good\n", rc=0)}))
    assert runtime_probe._check_missing_firmware() == []


def test_missing_firmware_journal_unreadable(monkeypatch):
    # journalctl present but exits non-zero (no permission) → degrade silently.
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout="", rc=1)}))
    assert runtime_probe._check_missing_firmware() == []


def test_collect_runtime_findings_combines(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run", _dispatch({
        "systemctl": _proc(stdout="foo.service loaded failed failed x\n"),
        "journalctl": _proc(stdout="Direct firmware load for q.bin failed\n", rc=0),
    }))
    findings = runtime_probe.collect_runtime_findings()
    ids = {f.check_id for f in findings}
    assert "failed_unit:foo.service" in ids
    assert "missing_firmware" in ids


# --- boot errors (F19) ------------------------------------------------------

def test_boot_errors_flagged(monkeypatch):
    log = ("Failed to start Foo Service.\n"
           "systemd-coredump[123]: Process 9 dumped core.\n"
           "ordinary info line\n"
           "EXT4-fs error (device sda1): bad\n")
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout=log, rc=0)}))
    out = runtime_probe._check_boot_errors()
    assert len(out) == 1 and out[0].check_id == "boot_errors"


def test_boot_errors_clean_when_no_matches(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout="all quiet\n", rc=0)}))
    assert runtime_probe._check_boot_errors() == []


def test_boot_errors_journal_unreadable(monkeypatch):
    monkeypatch.setattr(runtime_probe, "_run",
                        _dispatch({"journalctl": _proc(stdout="", rc=1)}))
    assert runtime_probe._check_boot_errors() == []
