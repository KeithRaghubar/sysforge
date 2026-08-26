# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Tests for describe_editor_chain / resolve_editor agreement."""
import subprocess
from unittest.mock import patch

from sysforge.primitives.editor import describe_editor_chain, resolve_editor


def _no_config():
    return patch("sysforge.primitives.editor.load_sysforge_toml", return_value={})


def test_chain_has_five_rungs_in_precedence_order(monkeypatch):
    monkeypatch.delenv("SYSFORGE_EDITOR", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    with _no_config():
        rungs, _ = describe_editor_chain()
    assert [r.source for r in rungs] == [
        "SYSFORGE_EDITOR", "sysforge.toml", "$EDITOR", "$VISUAL", "detected",
    ]
    assert [r.index for r in rungs] == [1, 2, 3, 4, 5]


def test_winner_is_highest_set_and_usable_rung(monkeypatch):
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.delenv("SYSFORGE_EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"
    ):
        rungs, winner = describe_editor_chain()
    assert rungs[winner].source == "$EDITOR"
    assert rungs[winner].value == "nano"


def test_chain_winner_matches_resolve_editor(monkeypatch):
    """The renderer must never disagree with what actually launches."""
    monkeypatch.setenv("SYSFORGE_EDITOR", "nvim")
    monkeypatch.setenv("EDITOR", "vim")
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"
    ):
        rungs, winner = describe_editor_chain()
        value, source = resolve_editor()
    assert (rungs[winner].value, rungs[winner].source) == (value, source)


def test_set_but_not_on_path_is_unusable_and_skipped(monkeypatch):
    monkeypatch.setenv("EDITOR", "ghost-editor")
    monkeypatch.setenv("VISUAL", "nano")
    monkeypatch.delenv("SYSFORGE_EDITOR", raising=False)
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which",
        side_effect=lambda c: None if c == "ghost-editor" else f"/usr/bin/{c}",
    ):
        rungs, winner = describe_editor_chain()
    editor_rung = next(r for r in rungs if r.source == "$EDITOR")
    assert editor_rung.value == "ghost-editor"
    assert editor_rung.usable is False
    assert "not on PATH" in editor_rung.detail
    assert rungs[winner].source == "$VISUAL"


def test_no_winner_when_nothing_resolves(monkeypatch):
    for var in ("SYSFORGE_EDITOR", "EDITOR", "VISUAL"):
        monkeypatch.delenv(var, raising=False)
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which", return_value=None
    ):
        rungs, winner = describe_editor_chain()
        assert winner == -1
        assert resolve_editor() == ("", "none")
        assert rungs[4].value == ""


def test_rung_indices_and_winner_are_derived_not_hardcoded(monkeypatch):
    """Characterization guard for `2.6.1-B13`.

    The detected/last-resort rung's ``index`` and the winner it claims were
    literals matching the length of the env-rung list above them. They agree
    today, so this cannot fail before the fix — it exists to fail *after* a
    future rung is added or removed, which is the defect's real trigger.
    Since ``resolve_editor`` is a reader over this function, a stale winner
    index is a wrong editor, not merely a wrong label.
    """
    for var in ("SYSFORGE_EDITOR", "EDITOR", "VISUAL"):
        monkeypatch.delenv(var, raising=False)
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which", side_effect=lambda c: f"/usr/bin/{c}"
    ):
        rungs, winner = describe_editor_chain()

    # Every rung's displayed index is its own 1-based position.
    assert [r.index for r in rungs] == list(range(1, len(rungs) + 1))
    # The last-resort rung is last, and when it wins, winner points at it.
    assert rungs[-1].source == "detected"
    assert rungs[winner].index == winner + 1


def test_detected_rung_lists_all_candidates_in_detail(monkeypatch):
    for var in ("SYSFORGE_EDITOR", "EDITOR", "VISUAL"):
        monkeypatch.delenv(var, raising=False)
    with _no_config(), patch(
        "sysforge.primitives.editor.shutil.which",
        side_effect=lambda c: f"/usr/bin/{c}" if c in ("nano", "vi") else None,
    ):
        rungs, winner = describe_editor_chain()
    assert rungs[winner].source == "detected"
    assert rungs[winner].value == "nano"       # first found wins, order vim,nano,vi
    assert "nano, vi" in rungs[4].detail


def test_run_tty_argv_releases_progress_region_around_child(monkeypatch):
    """A TUI editor must run inside ``progress.suspended()``.

    ``update``'s build loop holds a ``"building"`` tracker, so the recovery
    menu's ``[e]`` opens the editor while the bar still owns the bottom row
    via its DECSTBM region. The editor sizes itself to the full terminal and
    paints its status line onto the reserved row, leaving the bottom line
    corrupted on exit — the same failure the pager hit in B5, and this is the
    one home every editor launch goes through (3.1.0-B10).
    """
    import contextlib

    from sysforge.primitives import editor as editor_mod
    from sysforge.ui import progress

    events: list[str] = []

    @contextlib.contextmanager
    def _spy_suspended():
        events.append("suspend-enter")
        try:
            yield
        finally:
            events.append("suspend-exit")

    def _fake_run(argv, **kwargs):
        events.append("child")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(progress, "suspended", _spy_suspended)
    monkeypatch.setattr(editor_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(editor_mod.os, "open", lambda *a: (_ for _ in ()).throw(OSError))

    assert editor_mod.run_tty_argv(["nvim", "PKGBUILD"]) == 0
    assert events == ["suspend-enter", "child", "suspend-exit"]
