import subprocess

import pytest

from sysforge.primitives.build_prep import pkgctl_switch_version


def test_pkgctl_switch_version_invokes_pkgctl(tmp_path, monkeypatch):
    calls = {}
    def fake_run(cmd, **kw):
        calls["cmd"], calls["cwd"] = cmd, kw.get("cwd")
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr("sysforge.primitives.build_prep.subprocess.run", fake_run)
    pkgctl_switch_version(tmp_path, "7.0.14.arch1-1")
    assert calls["cmd"] == ["pkgctl", "repo", "switch", "7.0.14.arch1-1"]
    assert calls["cwd"] == str(tmp_path)


def test_pkgctl_switch_version_failure_raises(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "error: no such version"
        return R()
    monkeypatch.setattr("sysforge.primitives.build_prep.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="no such version"):
        pkgctl_switch_version(tmp_path, "9.9.9-1")


def test_pkgctl_switch_version_missing_pkgctl_raises(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("pkgctl")
    monkeypatch.setattr("sysforge.primitives.build_prep.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="pkgctl not found on PATH"):
        pkgctl_switch_version(tmp_path, "7.0.14.arch1-1")


def test_pkgctl_switch_version_timeout_raises(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout"))
    monkeypatch.setattr("sysforge.primitives.build_prep.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="timed out after 60s"):
        pkgctl_switch_version(tmp_path, "7.0.14.arch1-1")
