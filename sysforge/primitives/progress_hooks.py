# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
progress_hooks.py — the small progress surface primitives actually need.

Seven primitives (``prompt``, ``pager``, ``editor``, ``makepkg_invoke``,
``aur_resolve``, …) used the same five names out of ``sysforge.ui.progress``:
``suspend_for_prompt``, ``suspended``, ``reserved_rows``, ``heartbeat`` and
``tracker``. None of them wants *the bar* — they want "tell the display I am
about to take the terminal" and "count these items if anyone is counting".
That is a protocol, not a dependency (3.2.0-F1b): the leaf layer declares the
shape and a no-op default, and ``ui.progress`` registers itself as the
implementation when it is imported.

The payoff is that a primitive keeps working with no UI layer present at all —
in a test, a subprocess helper, or any future non-terminal front end — instead
of transitively importing the renderer, its signal handlers and its atexit hook
to ask how many rows are reserved.

Usage from a primitive::

    from sysforge.primitives import progress_hooks

    with progress_hooks.hooks().suspended():
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

    def suspend_for_prompt(self) -> None:
        """Release the terminal before reading interactive input."""

    def suspended(self):
        """Context manager: hand the terminal to a child, then take it back."""

    def reserved_rows(self) -> int:
        """Rows the display has reserved at the bottom (0 when nothing is)."""

    def heartbeat(self, detail: str) -> None:
        """Report liveness detail from a long, quiet operation."""

    def tracker(self, total: int, prefix: str):
        """Context manager yielding a ``tick(label)`` callable."""


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

    def suspend_for_prompt(self) -> None:
        pass

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        yield

    def reserved_rows(self) -> int:
        return 0

    def heartbeat(self, detail: str) -> None:
        pass

    @contextlib.contextmanager
    def tracker(self, total: int, prefix: str) -> Iterator[_NoOpTick]:
        yield _NoOpTick()


_NO_OP: Any = _NoOpHooks()
_impl: Any = _NO_OP


def register(impl: Any) -> None:
    """Install the live implementation (``ui.progress`` calls this itself).

    The module object satisfies the protocol structurally, so registration is a
    single call at the bottom of ``ui/progress.py`` and every lookup still goes
    through the module — patching ``ui.progress.suspended`` in a test keeps
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
