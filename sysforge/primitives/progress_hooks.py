# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
progress_hooks.py — the small progress surface primitives actually need.

Seven primitives (``prompt``, ``pager``, ``editor``, ``makepkg_invoke``,
``pty_runner``, ``aur_resolve``, …) need a handful of things from the display
in ``sysforge.ui.progress``: hand it the terminal (``yield_terminal``), size a
child around it (``reserved_rows``), report liveness (``heartbeat``), repaint
it while forwarding a raw byte stream (``forwarding_output``/``refresh``), and
count items if anyone is counting (``tracker``/``require_no_tracker``). None
of them wants *the bar*. That is a protocol, not a dependency (3.2.0-F1b): the
leaf layer declares the shape and a no-op default, and ``ui.progress``
registers itself as the implementation when it is imported.

The payoff is that a primitive keeps working with no UI layer present at all —
in a test, a subprocess helper, or any future non-terminal front end — instead
of transitively importing the renderer, its signal handlers and its atexit hook
to ask how many rows are reserved.

Usage from a primitive::

    from sysforge.primitives import progress_hooks

    with progress_hooks.hooks().yield_terminal("child"):
        ...

Always go through :func:`hooks` at call time — never bind the object once at
import — so a later ``register`` (and any test patching of the underlying
``ui.progress`` functions) is seen.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class ProgressHooks(Protocol):
    """The progress surface the leaf layer is allowed to know about."""

    def reserved_rows(self) -> int:
        """Rows the display has reserved at the bottom (0 when nothing is)."""
        ...

    def heartbeat(self, detail: str) -> None:
        """Report liveness detail from a long, quiet operation."""
        ...

    def tracker(
        self, total: int, prefix: str, expected: list[int | None] | None = None,
    ) -> contextlib.AbstractContextManager[Any]:
        """Context manager yielding a ``tick(label)`` callable.

        ``expected`` optionally gives each item's expected seconds, in tick
        order (``None`` for an item with no history), for the ETA (3.3.0-F1).
        """
        ...

    def yield_terminal(
        self, kind: str = "prompt",
    ) -> contextlib.AbstractContextManager[None]:
        """Context manager: hand the terminal to a ``"prompt"`` or a ``"child"``."""
        ...

    def refresh(self) -> None:
        """Repaint now if what the display shows has changed."""
        ...

    def forwarding_output(self) -> contextlib.AbstractContextManager[None]:
        """Context manager: a raw byte stream is being forwarded to the terminal."""
        ...

    def require_no_tracker(self, owner: str) -> None:
        """Refuse to start *owner*, which opens a tracker, inside one."""
        ...

    def paused_seconds(self) -> float:
        """Monotonic total of seconds the display has spent paused (prompts).

        A caller timing work subtracts the delta across its span, so a wait on
        the user is not recorded as work (3.3.0-F1).
        """
        ...


class _NoOpTick:
    """A tick that counts nothing, matching ``ui.progress.Tick``'s shape."""

    def __call__(self, label: str = "") -> None:
        pass

    def note(self, text: str) -> None:
        pass

    def resume(self) -> None:
        pass


class _NoOpHooks:
    """The default: correct behaviour with no display attached."""

    def reserved_rows(self) -> int:
        return 0

    def heartbeat(self, detail: str) -> None:
        pass

    @contextlib.contextmanager
    def tracker(self, total: int, prefix: str,
                expected: list[int | None] | None = None) -> Iterator[_NoOpTick]:
        yield _NoOpTick()

    @contextlib.contextmanager
    def yield_terminal(self, kind: str = "prompt") -> Iterator[None]:
        yield

    def refresh(self) -> None:
        pass

    @contextlib.contextmanager
    def forwarding_output(self) -> Iterator[None]:
        yield

    def require_no_tracker(self, owner: str) -> None:
        pass

    def paused_seconds(self) -> float:
        return 0.0


_NO_OP: Any = _NoOpHooks()
_impl: Any = _NO_OP


def register(impl: Any) -> None:
    """Install the live implementation (``ui.progress`` calls this itself).

    The module object satisfies the protocol structurally, so registration is a
    single call at the bottom of ``ui/progress.py`` and every lookup still goes
    through the module — patching ``ui.progress.yield_terminal`` in a test keeps
    working exactly as before.
    """
    global _impl
    _impl = impl


def reset() -> None:
    """Drop back to the no-op default (test teardown)."""
    global _impl
    _impl = _NO_OP


def hooks() -> ProgressHooks:
    """Return the registered implementation, or the no-op default."""
    return _impl
