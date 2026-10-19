"""
test_interrupt.py — tests for the InterruptScope SIGINT-deferral primitive.

Covers the single-Ctrl-C flag flow, the check() boundary, and exit-on-block
behavior. Multi-Ctrl-C semantics (signal.SIG_DFL fallthrough) are exercised
by signal-level mechanics in the implementation and intentionally not
unit-tested — the harness can't easily simulate a real second SIGINT
without killing the test process.
"""
import os
import signal

import pytest

from sysforge.primitives import interrupt as _interrupt
from sysforge.primitives.interrupt import CleanExitRequested, InterruptScope
from sysforge.primitives.stage_sentinel import StageSentinel, sentinel_scope


def test_no_signal_check_is_noop():
    """check() with no signal received does nothing."""
    with InterruptScope() as scope:
        scope.check()
        assert scope.requested is False


def test_signal_sets_flag_but_does_not_raise_immediately():
    """A SIGINT inside the scope sets the flag without raising."""
    with InterruptScope() as scope:
        os.kill(os.getpid(), signal.SIGINT)
        # The signal handler runs synchronously on the main thread before
        # the next bytecode; flag should be set by the time we check.
        assert scope.requested is True


def test_check_raises_after_signal():
    """check() raises CleanExitRequested after a SIGINT was observed."""
    with InterruptScope() as scope:
        os.kill(os.getpid(), signal.SIGINT)
        with pytest.raises(CleanExitRequested):
            scope.check()


def test_scope_restores_prior_handler():
    """Exiting the context restores whatever SIGINT handler was installed
    before — important because sysforge runs many nested contexts."""
    sentinel = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with InterruptScope():
            assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, sentinel)


def test_on_signal_callback_fires():
    """The optional on_signal hook fires once when a signal is received."""
    calls = []

    def cb():
        calls.append(1)

    with InterruptScope(on_signal=cb) as scope:
        os.kill(os.getpid(), signal.SIGINT)
        assert scope.requested is True
    assert calls == [1]


def test_clean_exit_requested_is_base_exception():
    """CleanExitRequested must NOT be caught by `except Exception:` —
    it must propagate to top-level cleanup so the sentinel is preserved."""
    assert issubclass(CleanExitRequested, BaseException)
    assert not issubclass(CleanExitRequested, Exception)


# ---------------------------------------------------------------------------
# 3.5.0-B44: the first Ctrl-C is acted on (checkpoint / redeliver), not lost
# ---------------------------------------------------------------------------


def test_checkpoint_is_a_noop_outside_a_scope():
    _interrupt.checkpoint()


def test_checkpoint_raises_after_first_ctrl_c(capfd):
    with pytest.raises(CleanExitRequested), InterruptScope():
        _interrupt.checkpoint()
        signal.raise_signal(signal.SIGINT)
        _interrupt.checkpoint()
    assert "stopping at the next safe point" in capfd.readouterr().err
    _interrupt.checkpoint()   # scope gone: no-op again


def test_first_ctrl_c_in_sentinel_scope_stops_at_checkpoint_sentinel_kept(tmp_path):
    reached = []
    with pytest.raises(RuntimeError, match="interrupted by user"):
        with sentinel_scope(tmp_path, "update", retry_cmd="sysforge update"):
            signal.raise_signal(signal.SIGINT)
            reached.append("after signal")    # the handler swallowed it
            _interrupt.checkpoint()
            reached.append("after checkpoint")
    assert reached == ["after signal"]
    assert StageSentinel(tmp_path).get_active()["stage"] == "update"


def test_ctrl_c_with_no_checkpoint_stops_once_the_body_completes(tmp_path):
    """An install window with no safe midpoint finishes, clears its
    sentinel, then the Ctrl-C takes effect as it would outside the scope."""
    done = []
    with pytest.raises(KeyboardInterrupt):
        with sentinel_scope(tmp_path, "kernel"):
            signal.raise_signal(signal.SIGINT)
            done.append("install finished")
    assert done == ["install finished"]
    assert StageSentinel(tmp_path).get_active() is None
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_no_ctrl_c_no_redelivery(tmp_path):
    with sentinel_scope(tmp_path, "kernel"):
        pass
    assert StageSentinel(tmp_path).get_active() is None
