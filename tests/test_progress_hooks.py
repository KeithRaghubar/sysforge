# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_progress_hooks.py — the leaf-layer progress protocol (3.2.0-F1b).

Two properties matter: a primitive works with no display registered at all
(the no-op default is a complete implementation, not a stub that raises), and
importing ``ui.progress`` wires the real renderer in without any primitive
learning about it.
"""
import pytest

from sysforge.primitives import progress_hooks


@pytest.fixture
def no_display():
    """Run with the no-op default, restoring whatever was registered."""
    prior = progress_hooks.hooks()
    progress_hooks.reset()
    yield
    progress_hooks.register(prior)


def test_noop_default_satisfies_the_whole_surface(no_display):
    h = progress_hooks.hooks()
    assert h.reserved_rows() == 0
    assert h.heartbeat("still going") is None
    with h.tracker(3, "pkg") as tick:
        tick("one")
        tick.note("sub-step")
        tick.resume()


def test_noop_default_covers_the_redesigned_surface(no_display):
    """3.3.0-F2: terminal handover, scheduled repaint and the nesting guard
    are all part of the protocol, so each needs a working no-op."""
    h = progress_hooks.hooks()
    with h.yield_terminal("prompt"):
        pass
    with h.yield_terminal("child"):
        pass
    with h.forwarding_output():
        pass
    assert h.refresh() is None
    assert h.require_no_tracker("build_and_install") is None


def test_the_superseded_handover_names_are_gone():
    """3.3.0-F2: yield_terminal is the one terminal-handover API."""
    from sysforge.ui import progress

    for name in ("suspend_for_prompt", "suspended"):
        assert not hasattr(progress_hooks.ProgressHooks, name)
        assert not hasattr(progress_hooks._NoOpHooks, name)
        assert not hasattr(progress, name)


def test_noop_default_is_protocol_conformant(no_display):
    assert isinstance(progress_hooks.hooks(), progress_hooks.ProgressHooks)


def test_ui_progress_registers_itself():
    """Importing the renderer installs it — no primitive imports ui/."""
    from sysforge.ui import progress

    progress_hooks.register(progress)  # idempotent; import already did it
    assert progress_hooks.hooks() is progress
    assert isinstance(progress, progress_hooks.ProgressHooks)


def test_register_is_seen_by_later_lookups(no_display):
    """Primitives call hooks() per use, so a late register still takes effect."""
    calls = []

    class Recorder:
        def refresh(self):
            calls.append("refresh")

    progress_hooks.register(Recorder())
    progress_hooks.hooks().refresh()
    assert calls == ["refresh"]


def test_prompt_suspends_through_the_hook(no_display, monkeypatch):
    """prompt_text yields the terminal via the protocol, not via a ui/ import."""
    import contextlib

    from sysforge.primitives import prompt

    calls = []

    class Recorder:
        @contextlib.contextmanager
        def yield_terminal(self, kind="prompt"):
            calls.append(f"yield:{kind}")
            yield

    progress_hooks.register(Recorder())
    monkeypatch.setattr("builtins.input", lambda _p: "value")
    assert prompt.prompt_text("pick", default="d") == "value"
    assert calls == ["yield:prompt"]
