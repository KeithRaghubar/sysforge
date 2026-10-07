"""tests for sysforge.primitives.privilege — the escalation seam (2.3.0-F10)."""
from unittest.mock import patch

from sysforge.primitives import privilege


def test_privileged_argv_prefixes_sudo_when_not_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        assert privilege.privileged_argv(["pacman", "-Syu"]) == ["sudo", "pacman", "-Syu"]


def test_privileged_argv_bare_when_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=0):
        assert privilege.privileged_argv(["pacman", "-Syu"]) == ["pacman", "-Syu"]


def test_privileged_argv_noninteractive_inserts_dash_n_when_not_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        assert privilege.privileged_argv(["rm", "-f", "/x"], noninteractive=True) == [
            "sudo", "-n", "rm", "-f", "/x",
        ]


def test_privileged_argv_noninteractive_moot_when_root():
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=0):
        assert privilege.privileged_argv(["rm", "-f", "/x"], noninteractive=True) == [
            "rm", "-f", "/x",
        ]


def test_privileged_argv_does_not_mutate_input():
    argv = ["pacman", "-Syu"]
    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000):
        privilege.privileged_argv(argv)
    assert argv == ["pacman", "-Syu"]


def test_run_privileged_escalates_then_delegates_to_run_or_raise():
    seen = {}

    def fake_run_or_raise(cmd, *, tag, **kw):
        seen["cmd"] = cmd
        seen["tag"] = tag
        return "OK"

    with patch("sysforge.primitives.privilege.os.geteuid", return_value=1000), \
         patch("sysforge.primitives.privilege.run_or_raise", fake_run_or_raise):
        result = privilege.run_privileged(["pacman", "-U", "x.pkg"], tag="TEST")

    assert result == "OK"
    assert seen["cmd"] == ["sudo", "pacman", "-U", "x.pkg"]
    assert seen["tag"] == "TEST"


# ---------------------------------------------------------------------------
# ensure_credentials (3.3.0-F3) — prompt with the progress bar yielded
# ---------------------------------------------------------------------------

def _yield_recorder(monkeypatch):
    import contextlib
    from sysforge.primitives import progress_hooks
    events = []

    class _Hooks:
        @contextlib.contextmanager
        def yield_terminal(self, kind="prompt"):
            events.append(("yield", kind))
            yield
            events.append(("unyield", kind))

    monkeypatch.setattr(progress_hooks, "hooks", lambda: _Hooks())
    return events


def test_ensure_credentials_uncached_prompts_inside_yield(monkeypatch):
    events = _yield_recorder(monkeypatch)
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(privilege, "_credentials_cached", lambda: False)

    def fake_run(argv, **kw):
        events.append(("run", tuple(argv)))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(privilege.subprocess, "run", fake_run)
    assert privilege.ensure_credentials() is True
    assert events == [("yield", "prompt"), ("run", ("sudo", "-v")), ("unyield", "prompt")]


def test_ensure_credentials_cached_yields_nothing(monkeypatch):
    events = _yield_recorder(monkeypatch)
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(privilege, "_credentials_cached", lambda: True)
    monkeypatch.setattr(privilege.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert privilege.ensure_credentials() is True
    assert events == []


def test_ensure_credentials_root_never_probes(monkeypatch):
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 0)
    monkeypatch.setattr(privilege, "_credentials_cached",
                        lambda: (_ for _ in ()).throw(AssertionError("probed")))
    assert privilege.ensure_credentials() is True


def test_credentials_probe_is_sudo_n_true_without_stdin(monkeypatch):
    monkeypatch.undo()  # drop conftest's _isolate_sudo_probe stub for this test
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(privilege.subprocess, "run", fake_run)
    assert privilege._credentials_cached() is False
    assert seen["argv"] == ["sudo", "-n", "true"]
    assert seen["stdin"] is privilege.subprocess.DEVNULL


def test_ensure_credentials_pauses_an_open_tracker(monkeypatch):
    """End to end through ui.progress: the clock is paused while sudo prompts."""
    from sysforge.ui import progress
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(privilege, "_credentials_cached", lambda: False)
    paused = []

    def fake_run(argv, **kw):
        paused.append(progress._bar.pause_depth)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(privilege.subprocess, "run", fake_run)
    monkeypatch.setattr(progress._bar, "mode", "tty")
    monkeypatch.setattr(progress, "_write", lambda *a, **k: None)
    with progress.tracker(3, "building") as tick:
        tick("a")
        privilege.ensure_credentials()
    assert paused == [1]
