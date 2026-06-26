"""
test_audio_probe.py — audio / sound-stack probe (doctor ``audio`` axis).

systemctl and pactl are stubbed; no real PipeWire/session-bus access. The probe
is read-only and best-effort: an absent tool or an unreachable session bus must
degrade to no findings (never a false "audio device vanished").
"""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import audio_probe
from sysforge.primitives import diagnostics as diag


def _proc(stdout="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=rc)


def _dispatch(mapping):
    """Return a fake _run that dispatches on cmd[0]."""
    def run(cmd):
        return mapping.get(cmd[0])
    return run


# --- failed audio user units ------------------------------------------------

def test_failed_audio_units_reported_as_errors(monkeypatch):
    out = (
        "wireplumber.service loaded failed failed Multimedia Service\n"
        "pipewire-pulse.service loaded failed failed PipeWire PulseAudio\n"
    )
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"systemctl": _proc(stdout=out)}))
    findings = audio_probe._check_failed_audio_units()
    ids = {f.check_id for f in findings}
    assert ids == {"audio_unit_failed:wireplumber.service",
                   "audio_unit_failed:pipewire-pulse.service"}
    assert all(f.severity == diag.SEV_ERROR for f in findings)
    assert all(f.category == "audio" for f in findings)


def test_failed_units_ignores_non_audio_units(monkeypatch):
    out = "fancontrol.service loaded failed failed fan control daemon\n"
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"systemctl": _proc(stdout=out)}))
    assert audio_probe._check_failed_audio_units() == []


def test_failed_units_no_user_bus_degrades(monkeypatch):
    # `systemctl --user` with no session bus exits nonzero → no findings.
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"systemctl": _proc(stdout="", rc=1)}))
    assert audio_probe._check_failed_audio_units() == []


def test_failed_units_no_systemctl(monkeypatch):
    monkeypatch.setattr(audio_probe, "_run", _dispatch({}))
    assert audio_probe._check_failed_audio_units() == []


# --- vanished output sink ---------------------------------------------------

def test_no_usable_sink_warns(monkeypatch):
    # Only the dummy device and a monitor source remain → device vanished.
    sinks = "50\tauto_null\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"pactl": _proc(stdout=sinks)}))
    findings = audio_probe._check_audio_sinks()
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "no_audio_sink"
    assert f.severity == diag.SEV_WARN
    assert f.category == "audio"


def test_real_sink_present_is_clean(monkeypatch):
    sinks = (
        "50\tauto_null\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
        "51\talsa_output.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts32le 2ch\tRUNNING\n"
    )
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"pactl": _proc(stdout=sinks)}))
    assert audio_probe._check_audio_sinks() == []


def test_no_sink_no_pactl_degrades(monkeypatch):
    monkeypatch.setattr(audio_probe, "_run", _dispatch({}))
    assert audio_probe._check_audio_sinks() == []


def test_no_sink_server_unreachable_degrades(monkeypatch):
    # pactl present but can't reach the server (run under sudo, no runtime dir).
    monkeypatch.setattr(audio_probe, "_run",
                        _dispatch({"pactl": _proc(stdout="", rc=1)}))
    assert audio_probe._check_audio_sinks() == []


# --- combiner ---------------------------------------------------------------

def test_collect_audio_findings_combines(monkeypatch):
    monkeypatch.setattr(audio_probe, "_run", _dispatch({
        "systemctl": _proc(stdout="pipewire.service loaded failed failed x\n"),
        "pactl": _proc(stdout="50\tauto_null\tPipeWire\ts16le\tSUSPENDED\n"),
    }))
    findings = audio_probe.collect_audio_findings()
    ids = {f.check_id for f in findings}
    assert "audio_unit_failed:pipewire.service" in ids
    assert "no_audio_sink" in ids
