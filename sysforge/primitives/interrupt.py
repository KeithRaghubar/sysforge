# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
interrupt.py — clean-exit SIGINT handling for install-bearing stages.

Install-bearing stages (toolchain, kernel, packages) mutate the live
system over many minutes via ``sudo pacman -U``. A bare Ctrl-C in the
middle of one of these stages risks landing the system in a mismatched
state (e.g. a partially-installed ``llvm-libs`` mismatched with the
prior ``llvm``/``clang``/``lld`` set).

This primitive installs a SIGINT handler that **does not** raise
``KeyboardInterrupt`` immediately. Instead, the first Ctrl-C flips an
internal flag, and code inside the scope stops at its next safe boundary
by calling :func:`checkpoint` (no scope object needed), which raises
:class:`CleanExitRequested` and leaves the
:class:`~sysforge.primitives.stage_sentinel.StageSentinel` intact. A
double-Ctrl-C falls through to the default Python SIGINT behaviour
(immediate ``KeyboardInterrupt``) — the operator explicitly chose the
unsafe path at that point.

Where the scope's body has no safe midpoint (an install, then its
verification) there is no checkpoint, and the request is acted on when the
body completes: :meth:`InterruptScope.redeliver` sends the SIGINT again once
the previous handler is back, so the run stops exactly as a Ctrl-C outside
any scope would (3.5.0-B44). Children in the terminal's foreground process
group (makepkg, pacman) still receive the first Ctrl-C themselves.

Usage::

    with InterruptScope():
        for step in ...:
            checkpoint()    # raises CleanExitRequested if Ctrl-C was seen
            run_subprocess(...)

``CleanExitRequested`` is a subclass of ``BaseException`` (not
``Exception``) so it propagates through ``except Exception:`` blocks
without being silently swallowed.
"""
from __future__ import annotations

import contextlib
import os
import signal
import threading

# Innermost-last stack of entered scopes, so checkpoint() reaches the active
# one without the scope being threaded through every call.
_ACTIVE: list["InterruptScope"] = []


def checkpoint() -> None:
    """Raise :class:`CleanExitRequested` when the active scope saw a Ctrl-C.

    A no-op outside any :class:`InterruptScope`. Call only where stopping
    leaves the system consistent, e.g. between two packages.
    """
    if _ACTIVE:
        _ACTIVE[-1].check()


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
        _ACTIVE.append(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        with contextlib.suppress(ValueError):
            _ACTIVE.remove(self)
        if self._prev_handler is not None:
            signal.signal(signal.SIGINT, self._prev_handler)

    def redeliver(self) -> None:
        """Send the deferred SIGINT again, once the scope has exited.

        For a body that completed with a request pending: the work it
        protected is done, so the Ctrl-C now takes effect under the previous
        handler (normally ``KeyboardInterrupt``). A no-op when none was seen.
        """
        if self.requested:
            signal.raise_signal(signal.SIGINT)

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
            with contextlib.suppress(Exception):
                self._on_signal()
        else:
            # The first Ctrl-C used to be silent (3.5.0-B44). A raw write:
            # a signal handler must not take the logging locks.
            with contextlib.suppress(OSError):
                os.write(2, b"\n[SYSFORGE] Ctrl-C: stopping at the next safe point. "
                            b"Press Ctrl-C again to stop now, which may leave the "
                            b"system inconsistent.\n")
