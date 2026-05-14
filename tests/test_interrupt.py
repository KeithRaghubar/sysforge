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

from sysforge.primitives.interrupt import CleanExitRequested, InterruptScope


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
