"""
test_sudo_session.py — unit tests for primitives/sudo_session.py.

sudo_session is the credential-*lifetime* seam (3.1.0-B5): the one home for
keeping sudo credentials warm across a long build, and for asking whether they
are usable right now before entering a mutation window. Escalation itself lives
in primitives/privilege.py and is not exercised here.

Every test patches os.geteuid, because the module short-circuits to a no-op when
already root and the suite must not depend on who runs it.
"""
import threading
from unittest.mock import MagicMock, patch

from sysforge.primitives import sudo_session
from sysforge.primitives.sudo_session import (
    SUDO_KEEPALIVE_INTERVAL,
    _keepalive_daemon,
    authenticate,
    keepalive,
)


def _run_result(returncode):
    result = MagicMock()
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------

def test_authenticate_runs_sudo_v_and_reports_success():
    with patch("os.geteuid", return_value=1000), \
         patch("subprocess.run", return_value=_run_result(0)) as mock_run:
        assert authenticate() is True

    mock_run.assert_called_once_with(["sudo", "-v"])


def test_authenticate_reports_failure_on_nonzero():
    """A timed-out passwd prompt exits non-zero — the caller must be able to see it."""
    with patch("os.geteuid", return_value=1000), \
         patch("subprocess.run", return_value=_run_result(1)):
        assert authenticate() is False


def test_authenticate_is_a_noop_when_already_root():
    with patch("os.geteuid", return_value=0), \
         patch("subprocess.run") as mock_run:
        assert authenticate() is True

    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _keepalive_daemon
# ---------------------------------------------------------------------------

def test_keepalive_daemon_calls_sudo_v():
    """Daemon refreshes at least once before stop_event fires."""
    stop_event = threading.Event()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _run_result(0)

    with patch("sysforge.primitives.sudo_session.SUDO_KEEPALIVE_INTERVAL", 0), \
         patch("subprocess.run", side_effect=fake_run):
        t = threading.Thread(
            target=_keepalive_daemon, args=(stop_event, "PGO"), daemon=True)
        t.start()
        t.join(timeout=1)
        stop_event.set()
        t.join(timeout=1)

    assert any(cmd == ["sudo", "-v"] for cmd in calls)


def test_keepalive_daemon_stops_immediately():
    """A pre-set stop_event means the daemon exits without calling sudo — the
    wait-first ordering is what stops it duplicating the caller's authenticate()."""
    stop_event = threading.Event()
    stop_event.set()

    with patch("subprocess.run") as mock_run:
        t = threading.Thread(
            target=_keepalive_daemon, args=(stop_event, "PGO"), daemon=True)
        t.start()
        t.join(timeout=2)

    mock_run.assert_not_called()


def test_keepalive_daemon_warns_and_continues_on_failure():
    """Best-effort by contract: a failed refresh warns, it does not raise."""
    stop_event = threading.Event()
    warnings = []

    with patch("sysforge.primitives.sudo_session.SUDO_KEEPALIVE_INTERVAL", 0), \
         patch("subprocess.run", return_value=_run_result(1)), \
         patch.object(sudo_session.log, "warn",
                      side_effect=lambda tag, msg: warnings.append((tag, msg))):
        t = threading.Thread(
            target=_keepalive_daemon, args=(stop_event, "KERNEL"), daemon=True)
        t.start()
        t.join(timeout=1)
        stop_event.set()
        t.join(timeout=1)

    assert warnings, "a failed refresh must warn"
    assert warnings[0][0] == "[KERNEL]"
    assert "keepalive failed" in warnings[0][1]


def test_keepalive_interval_under_sudoers_default():
    """Interval must be under 5 minutes (minimum reasonable sudoers timeout)."""
    assert SUDO_KEEPALIVE_INTERVAL < 5 * 60


# ---------------------------------------------------------------------------
# keepalive (context manager)
# ---------------------------------------------------------------------------

def test_keepalive_starts_and_joins_the_thread():
    started = threading.Event()

    def fake_run(cmd, **kwargs):
        started.set()
        return _run_result(0)

    with patch("os.geteuid", return_value=1000), \
         patch("sysforge.primitives.sudo_session.SUDO_KEEPALIVE_INTERVAL", 0), \
         patch("subprocess.run", side_effect=fake_run):
        with keepalive(tag="KERNEL"):
            assert started.wait(timeout=2), "daemon did not start"
            live = [t for t in threading.enumerate()
                    if t.name == "sysforge-sudo-keepalive"]
            assert live

    assert not [t for t in threading.enumerate()
                if t.name == "sysforge-sudo-keepalive" and t.is_alive()]


def test_keepalive_joins_the_thread_on_exception():
    """The whole point of the context manager: a stage cannot leak a daemon by
    forgetting its teardown, including on the failure path."""
    with patch("os.geteuid", return_value=1000), \
         patch("subprocess.run", return_value=_run_result(0)):
        try:
            with keepalive(tag="KERNEL"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    assert not [t for t in threading.enumerate()
                if t.name == "sysforge-sudo-keepalive" and t.is_alive()]


def test_keepalive_disabled_is_a_noop():
    """enabled=False (dry run) keeps one code path at the call site."""
    with patch("os.geteuid", return_value=1000), \
         patch("subprocess.run") as mock_run:
        with keepalive(tag="PGO", enabled=False):
            assert not [t for t in threading.enumerate()
                        if t.name == "sysforge-sudo-keepalive"]

    mock_run.assert_not_called()


def test_keepalive_is_a_noop_when_already_root():
    with patch("os.geteuid", return_value=0), \
         patch("subprocess.run") as mock_run:
        with keepalive(tag="PGO"):
            assert not [t for t in threading.enumerate()
                        if t.name == "sysforge-sudo-keepalive"]

    mock_run.assert_not_called()
