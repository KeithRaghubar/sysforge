"""
interrupt.py — clean-exit SIGINT handling for install-bearing stages.

Install-bearing stages (toolchain, kernel, packages) mutate the live
system over many minutes via ``sudo pacman -U``. A bare Ctrl-C in the
middle of one of these stages risks landing the system in a mismatched
state (e.g. a partially-installed ``llvm-libs`` mismatched with the
prior ``llvm``/``clang``/``lld`` set).

This primitive installs a SIGINT handler that **does not** raise
``KeyboardInterrupt`` immediately. Instead, the first Ctrl-C flips an
internal flag; the stage code is expected to check it at safe boundaries
(after each ``makepkg`` run, between PGO passes, after each ``pacman -U``)
via :meth:`InterruptScope.check` and exit cleanly with the
:class:`~sysforge.primitives.stage_sentinel.StageSentinel` intact. A
double-Ctrl-C falls through to the default Python SIGINT behaviour
(immediate ``KeyboardInterrupt``) — the operator explicitly chose the
unsafe path at that point.

Usage::

    with InterruptScope() as scope:
        for step in ...:
            run_subprocess(...)
            scope.check()   # raises CleanExitRequested if Ctrl-C was seen

``CleanExitRequested`` is a subclass of ``BaseException`` (not
``Exception``) so it propagates through ``except Exception:`` blocks
without being silently swallowed.
"""
from __future__ import annotations

import signal
import threading


class CleanExitRequested(BaseException):
    """Raised by :meth:`InterruptScope.check` when SIGINT was received.

    Subclasses ``BaseException`` so it propagates through typical
    ``except Exception:`` handlers — only top-level cleanup code should
    catch it. Carries no message; callers raise their own with context.
    """


class InterruptScope:
    """Context manager that defers SIGINT to a checkable flag.

    On ``__enter__``: installs a SIGINT handler that sets an internal
    flag and logs a one-line notice. The first Ctrl-C is captured; the
    second falls through to default handling and terminates immediately.

    On ``__exit__``: restores the previous SIGINT handler. If the flag
    was set and the body did not raise, the caller is responsible for
    having checked it — ``__exit__`` does not raise on its own.

    Thread-safety: the flag is protected by a lock; ``check()`` may be
    called from any thread. The signal handler runs on the main thread
    only, per Python's signal module contract.
    """

    def __init__(self, *, on_signal=None) -> None:
        self._flag = threading.Event()
        self._sigint_count = 0
        self._lock = threading.Lock()
        self._prev_handler = None
        self._on_signal = on_signal

    # ------------------------------------------------------------------
    # context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "InterruptScope":
        self._prev_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handler)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._prev_handler is not None:
            signal.signal(signal.SIGINT, self._prev_handler)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def requested(self) -> bool:
        """True iff at least one SIGINT has been observed."""
        return self._flag.is_set()

    def check(self) -> None:
        """Raise :class:`CleanExitRequested` if a SIGINT was observed.

        Safe to call from any thread. Call at every sub-step boundary
        in the stage: after each subprocess, between PGO passes, after
        each ``pacman -U``.
        """
        if self._flag.is_set():
            raise CleanExitRequested()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _handler(self, signum, frame) -> None:
        with self._lock:
            self._sigint_count += 1
            count = self._sigint_count
        if count >= 2:
            # Second Ctrl-C: restore default handler and re-raise so the
            # process terminates immediately. The user explicitly chose
            # the unsafe path.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise KeyboardInterrupt()
        self._flag.set()
        if self._on_signal is not None:
            try:
                self._on_signal()
            except Exception:
                pass
