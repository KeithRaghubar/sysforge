
from sysforge.primitives import gfxperf_probe as gp


def _ids(findings):
    return {f.check_id for f in findings}


def test_cpu_governor_powersave_warns(monkeypatch, tmp_path):
    gov = tmp_path / "scaling_governor"
    gov.write_text("powersave\n")
    monkeypatch.setattr(gp, "_GOVERNOR_PATH", gov)
    f = gp._check_cpu_governor()
    assert f is not None and f.severity == gp.SEV_WARN
    assert f.check_id == "cpu_governor"


def test_cpu_governor_performance_is_info(monkeypatch, tmp_path):
    gov = tmp_path / "scaling_governor"
    gov.write_text("schedutil\n")
    monkeypatch.setattr(gp, "_GOVERNOR_PATH", gov)
    f = gp._check_cpu_governor()
    assert f is not None and f.severity == gp.SEV_INFO
    assert "schedutil" in f.message


def test_cpu_governor_absent_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(gp, "_GOVERNOR_PATH", tmp_path / "missing")
    assert gp._check_cpu_governor() is None


def test_memory_pressure_low_available_info(monkeypatch):
    meminfo = "MemTotal: 1000 kB\nMemAvailable: 50 kB\nSwapTotal: 200 kB\nSwapFree: 100 kB\n"
    monkeypatch.setattr(gp, "_read_text", lambda p: meminfo)
    f = gp._check_memory_pressure()
    assert f is not None and f.severity == gp.SEV_INFO
    assert "instant" in f.message


def test_check_gfxperf_no_gpu_runs_only_agnostic(monkeypatch):
    monkeypatch.setattr(gp.pacman, "get_all_installed_packages", lambda: {})
    ids = _ids(gp.check_gfxperf({}, gpu_vendors=[]))
    assert ids == {"cpu_governor", "memory_pressure"}


def test_check_gfxperf_never_errors(monkeypatch):
    monkeypatch.setattr(gp.pacman, "get_all_installed_packages", lambda: {})
    for f in gp.check_gfxperf({}, gpu_vendors=["nvidia"]):
        assert f.severity in (gp.SEV_WARN, gp.SEV_INFO)


def test_vaapi_missing_warns():
    f = gp._check_vaapi_driver({})
    assert f.severity == gp.SEV_WARN and f.check_id == "vaapi_driver"
    # B4: remediation must name the real Arch package, not the phantom upstream one.
    assert "libva-nvidia-driver" in f.remediation
    assert "Install 'nvidia-vaapi-driver'" not in f.remediation


def test_vaapi_present_info():
    # B4: gate on the real Arch package name (libva-nvidia-driver), which is
    # what is actually installed on a correct system.
    f = gp._check_vaapi_driver({"libva-nvidia-driver": "0.0.17-1"})
    assert f.severity == gp.SEV_INFO


def test_vaapi_upstream_name_not_matched():
    """B4: nvidia-vaapi-driver is the upstream project name, not an Arch package.
    Matching on it never fires on a real system (permanent false negative on the
    OK path); the gate must key on libva-nvidia-driver only."""
    f = gp._check_vaapi_driver({"nvidia-vaapi-driver": "0.0.13-1"})
    assert f.severity == gp.SEV_WARN


def test_libva_env_always_info(monkeypatch):
    monkeypatch.delenv("LIBVA_DRIVER_NAME", raising=False)
    monkeypatch.delenv("NVD_BACKEND", raising=False)
    f = gp._check_libva_env()
    assert f.severity == gp.SEV_INFO
    assert "unset" in f.message and "session" in f.message


def test_nvidia_gated_checks_present(monkeypatch):
    monkeypatch.setattr(gp.pacman, "get_all_installed_packages", lambda: {})
    ids = _ids(gp.check_gfxperf({}, gpu_vendors=["nvidia"]))
    assert {"vaapi_driver", "libva_env"} <= ids


import subprocess as _sp


def _cp(stdout="", rc=0):
    return _sp.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def test_persistence_disabled_info(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp("Persistence Mode : Disabled\n"))
    f = gp._check_nvidia_persistence()
    assert f.severity == gp.SEV_INFO and "disabled" in f.message.lower()


def test_persistence_enabled_info(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp("Persistence Mode : Enabled\n"))
    f = gp._check_nvidia_persistence()
    assert f.severity == gp.SEV_INFO and f.remediation == ""


def test_persistence_no_smi_returns_none(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: None)
    assert gp._check_nvidia_persistence() is None


def test_powerd_inactive_info(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp("inactive\n", rc=3))
    f = gp._check_nvidia_powerd()
    assert f.severity == gp.SEV_INFO and "inactive" in f.message


def test_powerd_active_info(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp("active\n", rc=0))
    f = gp._check_nvidia_powerd()
    assert f.severity == gp.SEV_INFO and f.remediation == ""


def test_powerd_unknown_unit_none(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp("unknown\n", rc=4))
    assert gp._check_nvidia_powerd() is None


def test_gl_frame_pacing_always_info(monkeypatch):
    monkeypatch.delenv("__GL_MaxFramesAllowed", raising=False)
    monkeypatch.delenv("__GL_SYNC_TO_VBLANK", raising=False)
    f = gp._check_gl_frame_pacing()
    assert f.severity == gp.SEV_INFO and "session" in f.message


def test_gpu_thermal_near_slowdown_warns(monkeypatch):
    out = "GPU Current Temp : 88 C\nGPU Slowdown Temp : 90 C\n"
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp(out))
    f = gp._check_gpu_thermal()
    assert f.severity == gp.SEV_WARN and "instant" in f.message


def test_gpu_thermal_cool_info(monkeypatch):
    out = "GPU Current Temp : 45 C\nGPU Slowdown Temp : 90 C\n"
    monkeypatch.setattr(gp, "_run", lambda cmd: _cp(out))
    f = gp._check_gpu_thermal()
    assert f.severity == gp.SEV_INFO


def test_gpu_thermal_no_smi_none(monkeypatch):
    monkeypatch.setattr(gp, "_run", lambda cmd: None)
    assert gp._check_gpu_thermal() is None


def test_full_nvidia_id_set(monkeypatch):
    monkeypatch.setattr(gp.pacman, "get_all_installed_packages", lambda: {})
    monkeypatch.setattr(gp, "_run", lambda cmd: None)  # skip smi/systemctl checks
    ids = _ids(gp.check_gfxperf({}, gpu_vendors=["nvidia"]))
    assert {"vaapi_driver", "libva_env", "gl_frame_pacing"} <= ids
