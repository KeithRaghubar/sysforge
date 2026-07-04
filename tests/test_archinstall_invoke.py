import os
import stat
import pytest
from sysforge.primitives import archinstall_invoke as ai

def _cfg():
    return {"!root-password": "s3cret", "users": [{"!password": "hunter2"}], "version": "3.0.15"}

def test_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="live"):
        ai.run_archinstall(_cfg(), dry_run=False)

def test_redact_scrubs_passwords():
    red = ai._redact(_cfg())
    assert red["!root-password"] == "***"
    assert red["users"][0]["!password"] == "***"
    # original untouched
    assert _cfg()["!root-password"] == "s3cret"

def test_dry_run_does_not_invoke(monkeypatch, capsys):
    monkeypatch.setattr(ai.shutil, "which", lambda _: "/usr/bin/archinstall")
    called = []
    monkeypatch.setattr(ai, "run_or_raise", lambda *a, **k: called.append(a))
    ai.run_archinstall(_cfg(), dry_run=True)
    assert called == []
    out = capsys.readouterr().out
    assert "s3cret" not in out and "hunter2" not in out
    assert "***" in out

def test_real_run_writes_0600_and_invokes(monkeypatch):
    monkeypatch.setattr(ai.shutil, "which", lambda _: "/usr/bin/archinstall")
    monkeypatch.setattr(ai, "_warn_on_version_drift", lambda: None)
    seen = {}
    def fake_run(cmd, **kw):
        # config file exists and is 0600 at invocation time
        path = cmd[cmd.index("--config") + 1]
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        seen["cmd"] = cmd
        class R:
            pass
        return R()
    monkeypatch.setattr(ai, "run_or_raise", fake_run)
    ai.run_archinstall(_cfg(), dry_run=False)
    assert seen["mode"] == 0o600
    assert "--silent" in seen["cmd"]
