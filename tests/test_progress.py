"""
test_progress.py — tests for sysforge.ui.progress.

Verifies:
  - mode detection: stderr-is-tty / not-tty / TERM=dumb / CI / NO_COLOR / dry-run
  - plain mode emits [PROGRESS] via log.ui() and writes no ANSI to stderr
  - TTY mode writes DECSTBM scroll-region escapes to stderr, one write per frame
  - tracker() increments correctly and releases on exit
  - yield_terminal() hands the terminal over and pauses the tracker clock
  - the scheduled repaint (ticker / refresh) and the one-tracker rule (3.3.0-F2)

The autouse fixture in ``tests/conftest.py`` gives every test a fresh ``_bar``,
strict nesting and no ticker thread; scheduled paints are driven by calling
``_ticker_step()`` / ``refresh()`` directly.
"""
import contextlib
import io
import shutil
import sys
import time

import pytest

from sysforge import log
from sysforge.primitives import progress_hooks
from sysforge.ui import progress


@pytest.fixture(autouse=True)
def _clean_progress_between_tests(monkeypatch):
    monkeypatch.setattr(log, "_DRY_RUN", False)
    for var in ("CI", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    yield
    progress.shutdown()


class _Stream(io.StringIO):
    """A stderr stand-in that records every write() call separately."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, s: str) -> int:
        self.writes.append(s)
        return super().write(s)

    def reset(self) -> None:
        self.truncate(0)
        self.seek(0)
        self.writes.clear()


def _fake_tty_stderr(monkeypatch) -> _Stream:
    """Install a stderr that claims isatty() == True and captures writes."""
    buf = _Stream(tty=True)
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def _fake_plain_stderr(monkeypatch) -> _Stream:
    buf = _Stream(tty=False)
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def _fake_clock(monkeypatch):
    """Install a controllable monotonic clock; returns an advance() callable."""
    state = {"t": 1000.0}
    monkeypatch.setattr(progress, "_monotonic", lambda: state["t"])

    def advance(seconds):
        state["t"] += seconds
    return advance


def _plain_lines(monkeypatch):
    """Capture the text progress emits in plain mode."""
    lines = []
    monkeypatch.setattr(log, "ui", lambda tag, msg: lines.append(msg))
    return lines


def _composed() -> str | None:
    return progress._compose(progress._bar, progress._monotonic())


# --- Mode detection ---------------------------------------------------------

def test_mode_plain_when_stderr_not_tty(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    assert progress._bar.mode == "plain"


def test_mode_tty_when_dry_run_on_a_tty(monkeypatch):
    """3.1.0-B7: a dry run renders progress exactly as the real run it previews.
    The log-to-stdout redirect it used to be paired with cannot interleave with
    progress, which writes to stderr."""
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setattr(log, "_DRY_RUN", True)
    progress.init()
    assert progress._bar.mode == "tty"


def test_mode_plain_when_dry_run_off_tty(monkeypatch):
    """The genuine non-interactive rungs still apply to a dry run — a scripted
    dry run redirected to a file gets plain output from the isatty rung."""
    _fake_plain_stderr(monkeypatch)
    monkeypatch.setattr(log, "_DRY_RUN", True)
    progress.init()
    assert progress._bar.mode == "plain"


def test_mode_plain_when_term_dumb(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    progress.init()
    assert progress._bar.mode == "plain"


def test_mode_plain_when_ci_set(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("CI", "1")
    progress.init()
    assert progress._bar.mode == "plain"


def test_mode_plain_when_no_color_set(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    progress.init()
    assert progress._bar.mode == "plain"


def test_mode_tty_when_all_conditions_met(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert progress._bar.mode == "tty"


# --- Plain mode rendering ---------------------------------------------------

def test_plain_mode_emits_no_ansi(monkeypatch):
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    with progress.tracker(10, "building") as tick:
        tick("htop")
    assert "\x1b" not in buf.getvalue()


def test_plain_mode_routes_through_log_ui(monkeypatch):
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    with progress.tracker(10, "building") as tick:
        tick("htop")
    out = buf.getvalue()
    assert "[PROGRESS]" in out
    assert "[1/10] building · htop" in out


# --- TTY mode rendering -----------------------------------------------------

def test_tty_mode_emits_scroll_region(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "p") as tick:
        tick("x")
        written = buf.getvalue()
    # DECSTBM set region: ESC[1;Nr
    assert "\x1b[1;" in written and "r" in written
    # Some text painted on the bottom row
    assert "1/2" in written
    assert "x" in written


def test_tty_mode_release_on_clear(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    buf.reset()
    progress.clear()
    written = buf.getvalue()
    # ESC[r resets scroll region
    assert "\x1b[r" in written
    assert progress._bar.reserved is False


def test_tty_mode_shutdown_idempotent(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    progress.shutdown()
    progress.shutdown()  # must not raise


# --- tracker() context manager ----------------------------------------------

def test_tracker_increments_counter(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    lines = _plain_lines(monkeypatch)
    with progress.tracker(3, "building") as tick:
        tick("a")
        tick("b")
        tick("c")
    assert lines == [
        "[PROGRESS] [0/3] building · starting...",
        "[PROGRESS] [1/3] building · a",
        "[PROGRESS] [2/3] building · b",
        "[PROGRESS] [3/3] building · c",
    ]


def test_tracker_note_and_resume(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    lines = _plain_lines(monkeypatch)
    with progress.tracker(3, "building") as tick:
        tick("htop")
        # Overlay a transient sub-step at the current count (no increment) ...
        tick.note("installing 2 intra-batch dep(s) for htop")
        # ... then hand the line back to the last tick state.
        tick.resume()
    assert lines == [
        "[PROGRESS] [0/3] building · starting...",
        "[PROGRESS] [1/3] building · htop",
        "[PROGRESS] [1/3] installing 2 intra-batch dep(s) for htop",
        "[PROGRESS] [1/3] building · htop",
    ]


def test_tracker_resume_before_first_tick_is_noop(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    lines = _plain_lines(monkeypatch)
    with progress.tracker(2, "building") as tick:
        tick.resume()  # no tick yet — must not repaint
    # Only the entry placeholder; resume() emitted nothing.
    assert lines == ["[PROGRESS] [0/2] building · starting..."]


def test_tracker_releases_region_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "p") as tick:
        tick("one")
        tick("two")
    # After exit, region should be reset.
    assert "\x1b[r" in buf.getvalue()
    assert progress._bar.reserved is False


def test_tracker_releases_on_exception(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with pytest.raises(RuntimeError), progress.tracker(3, "p") as tick:
        tick("a")
        raise RuntimeError("boom")
    assert progress._bar.reserved is False


def test_stale_tick_after_exit_is_a_noop(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "p") as tick:
        tick("a")
    buf.reset()
    tick("late")
    tick.note("late note")
    tick.resume()
    assert buf.writes == []
    assert progress._bar.tracker is None


def test_tracker_line_wins_over_a_mid_tracker_phase(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "building") as tick:
        tick("a")
        progress.phase("installing built packages")
        assert _composed() == "[SYSFORGE][PROGRESS] [1/2] building · a"
    # The slot was updated all along and shows once the tracker closes.
    assert _composed() == "[SYSFORGE][PROGRESS] installing built packages"


# --- clear() safety ---------------------------------------------------------

def test_clear_without_reservation_is_safe(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.clear()  # no prior paint — must not raise or write garbage


def test_paint_reestablishes_after_clear(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    progress.clear()
    buf.reset()
    progress.phase("y")
    written = buf.getvalue()
    assert "\x1b[1;" in written  # region re-established
    assert "[SYSFORGE][PROGRESS] y" in written


def test_clear_leaves_state_untouched(monkeypatch):
    """clear() is teardown of the terminal, not of what the bar says."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    progress.clear()
    assert progress._bar.phase == "x"
    assert progress._bar.drawn is None


# --- phase() ------------------------------------------------------------------

def test_phase_paints_uncounted_status_tty(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("loading state")
    written = buf.getvalue()
    assert "\x1b[1;" in written  # region established
    assert "[SYSFORGE][PROGRESS] loading state" in written
    assert "[0/" not in written  # no counter


def test_phase_none_clears_and_releases(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("loading state")
    progress.phase(None)
    assert progress._bar.phase is None
    assert progress._bar.reserved is False
    assert "\x1b[r" in buf.getvalue()


def test_tracker_restores_enclosing_phase_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("dep prep")
    buf.reset()
    with progress.tracker(1, "building") as tick:
        tick("a")
    # Tracker exit repaints the phase instead of releasing the region.
    assert progress._bar.reserved is True
    assert "\x1b[r" not in buf.getvalue()
    written = buf.getvalue()
    assert written.rindex("dep prep") > written.rindex("building")


def test_tracker_still_releases_without_phase(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(1, "building") as tick:
        tick("a")
    assert progress._bar.reserved is False
    assert "\x1b[r" in buf.getvalue()


def test_phase_plain_mode_dedupes_repeats(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = _plain_lines(monkeypatch)
    progress.phase("version check")
    progress.phase("version check")
    progress.phase("drift check")
    assert seen == ["[PROGRESS] version check", "[PROGRESS] drift check"]


def test_plain_tracker_exit_does_not_relog_the_phase(monkeypatch):
    """The phase line was already logged before the tracker; exit is silent,
    and the same phase set again afterwards is still a repeat."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = _plain_lines(monkeypatch)
    progress.phase("dep prep")
    with progress.tracker(1, "building") as tick:
        tick("a")
    progress.phase("dep prep")
    assert seen == [
        "[PROGRESS] dep prep",
        "[PROGRESS] [0/1] building · starting...",
        "[PROGRESS] [1/1] building · a",
    ]


# --- yield_terminal() ---------------------------------------------------------

def test_prompt_yield_keeps_region(monkeypatch):
    # A prompt yield blanks the bar line in place but must NOT reset the
    # scroll region (no ESC[r) and must keep the region reserved — the prompt
    # then prints in the live content flow.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")   # reserve + paint
    buf.reset()
    with progress.yield_terminal("prompt"):
        written = buf.getvalue()
        assert progress._RESET_REGION not in written   # region NOT released
        assert progress._bar.reserved is True
        assert progress._CLEAR_LINE in written          # bar line blanked
        # Balanced cursor save/restore — the content cursor is left untouched.
        assert written.count(progress._SAVE) == written.count(progress._RESTORE)


def test_prompt_yield_repaints_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    with progress.yield_terminal("prompt"):
        buf.reset()
    assert "[SYSFORGE][PROGRESS] building" in buf.getvalue()


def test_prompt_yield_safe_without_region(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.yield_terminal("prompt"):  # nothing reserved — a no-op
        pass
    assert buf.writes == []


def test_child_yield_releases_then_restores_region(monkeypatch):
    # A child yield fully releases the region for the body (so a TTY-inheriting
    # subprocess gets a clean terminal) and re-establishes + repaints on exit.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    assert progress._bar.reserved is True
    buf.reset()
    with progress.yield_terminal("child"):
        # Inside the body the region is released (full clear).
        assert progress._bar.reserved is False
        assert progress._RESET_REGION in buf.getvalue()
    # On exit the region is re-established and the bar repainted.
    assert progress._bar.reserved is True
    assert "building" in buf.getvalue()


def test_child_yield_noop_without_region(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.yield_terminal("child"):  # nothing reserved — clean no-op
        assert progress._bar.reserved is False
    assert progress._bar.reserved is False


def test_nothing_draws_while_yielded(monkeypatch):
    """State keeps updating while yielded; only the outermost exit paints."""
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("a")
    with progress.yield_terminal("prompt"):
        buf.reset()
        progress.phase("b")
        progress.refresh()
        progress._ticker_step()
        assert buf.writes == []
    assert "[SYSFORGE][PROGRESS] b" in buf.getvalue()


def test_nested_yield_strongest_kind_applies(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("a")
    with progress.yield_terminal("prompt"):
        assert progress.reserved_rows() == 1
        with progress.yield_terminal("child"):
            assert progress._bar.yielded == "child"
            assert progress.reserved_rows() == 0
        # Still yielded: only the outermost exit restores and repaints.
        assert progress._bar.yielded is not None
        assert progress._bar.reserved is False
    assert progress._bar.yielded is None
    assert progress._bar.reserved is True


def test_yield_terminal_is_a_noop_in_plain_mode(monkeypatch):
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.phase("a")
    before = buf.getvalue()
    with progress.yield_terminal("child"):
        assert progress._bar.yielded is None
    assert buf.getvalue() == before


def test_yield_terminal_rejects_unknown_kind(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with pytest.raises(ValueError), progress.yield_terminal("pager"):
        pass


def test_reserved_rows_one_when_region_active(monkeypatch):
    # A pty child is sized to terminal_height - reserved_rows() so it never
    # touches the bar row — must report 1 while the bar holds the bottom row.
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert progress.reserved_rows() == 0      # nothing reserved yet
    progress.phase("building")
    assert progress.reserved_rows() == 1      # bar holds the bottom row
    progress.clear()
    assert progress.reserved_rows() == 0      # released


def test_reserved_rows_zero_in_plain_mode(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    assert progress.reserved_rows() == 0      # no scroll region in plain mode


def test_tty_region_ops_preserve_cursor(monkeypatch):
    # Regression: in a fresh shell (content near the top, empty rows below),
    # establishing/releasing the DECSTBM region must not drag the logical
    # cursor to the screen bottom — otherwise the empty gap is stranded as
    # scrollback blank lines. The guarantee is structural: every region
    # mutation is bracketed by a save (ESC7) / restore (ESC8) pair.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("starting")   # establishes the region
    progress.phase(None)         # releases it
    written = buf.getvalue()
    # Region was actually set and reset (guards against the test going no-op).
    assert "\x1b[1;" in written and progress._RESET_REGION in written
    # Saves and restores are balanced — no cursor move escapes its bracket.
    assert written.count(progress._SAVE) == written.count(progress._RESTORE)
    # The teardown leaves the cursor restored (last op is a restore), not
    # parked at the absolute bottom row.
    assert written.rstrip().endswith(progress._RESTORE)


def test_establish_scroll_reserves_bar_row(monkeypatch):
    # Establishing the region must land the cursor INSIDE [1, N-1] regardless of
    # where the prompt started — otherwise a bottom-of-screen prompt leaves the
    # cursor below the region and output collapses onto the bottom row. The
    # "scroll up one to reserve" choreography: save → jump to bottom row → index
    # (ESC D, scroll up one) → set region → restore → cursor-up one.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("starting")   # establishes the region
    written = buf.getvalue()
    rows = progress._bar.rows
    # Each step is present, in order, before the region is set.
    bottom_jump = f"\x1b[{rows};1H"
    i_save = written.index(progress._SAVE)
    i_jump = written.index(bottom_jump, i_save)
    i_index = written.index(progress._INDEX, i_jump)
    i_region = written.index(f"\x1b[1;{rows - 1}r", i_index)
    i_restore = written.index(progress._RESTORE, i_region)
    i_up = written.index("\x1b[1A", i_restore)
    assert i_save < i_jump < i_index < i_region < i_restore < i_up
    # Cursor handling stays balanced.
    assert written.count(progress._SAVE) == written.count(progress._RESTORE)


# --- Compose (3.3.0-F2) -------------------------------------------------------

def test_compose_every_shape():
    now = 1000.0
    bar = progress._Bar(mode="tty")
    assert progress._compose(bar, now) is None
    bar.detail = "orphan detail"
    assert progress._compose(bar, now) is None   # detail alone composes nothing
    bar.detail = None
    bar.phase = "loading"
    assert progress._compose(bar, now) == "[SYSFORGE][PROGRESS] loading"
    bar.detail = "cc foo.c"
    assert progress._compose(bar, now) == "[SYSFORGE][PROGRESS] loading · cc foo.c"
    bar.detail = None
    bar.tracker = progress._Tracker(total=3, prefix="building", started=now)
    assert progress._compose(bar, now) == "[SYSFORGE][PROGRESS] [0/3] building · starting..."
    bar.tracker.index, bar.tracker.label = 2, "mesa"
    assert progress._compose(bar, now) == "[SYSFORGE][PROGRESS] [2/3] building · mesa"
    bar.tracker.note = "installing deps"
    assert progress._compose(bar, now) == "[SYSFORGE][PROGRESS] [2/3] installing deps"
    bar.mode = "plain"
    assert progress._compose(bar, now) == "[PROGRESS] [2/3] installing deps"


def test_compose_ignores_an_empty_tracker():
    """tracker(0, ...) never painted a 0/0 line; the phase shows instead."""
    bar = progress._Bar(mode="tty", phase="resolving")
    bar.tracker = progress._Tracker(total=0, prefix="AUR dep", started=0.0)
    assert progress._compose(bar, 0.0) == "[SYSFORGE][PROGRESS] resolving"


# --- Paint (3.3.0-F2) ---------------------------------------------------------

def test_each_frame_is_exactly_one_write(monkeypatch):
    """Four writes per frame could be interleaved by another thread's log
    line; region establishment rides in the same single write."""
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    assert len(buf.writes) == 1
    assert "\x1b[1;" in buf.writes[0] and "[SYSFORGE][PROGRESS] x" in buf.writes[0]
    progress.phase("y")
    assert len(buf.writes) == 2


def test_unchanged_frame_writes_nothing(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    buf.reset()
    progress.phase("x")
    progress.refresh()
    progress._ticker_step()
    assert buf.writes == []


def test_sigwinch_only_flags_and_next_frame_applies(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    buf.reset()
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(80, 24): __import__("os").terminal_size((100, 40)))
    progress._on_sigwinch()
    assert buf.writes == []                  # no I/O from the signal handler
    assert progress._bar.resize_pending is True
    progress._ticker_step()
    assert len(buf.writes) == 1
    assert "\x1b[1;39r" in buf.writes[0]     # region re-established at new size
    assert "[SYSFORGE][PROGRESS] x" in buf.writes[0]
    assert progress._bar.resize_pending is False


def _term_size(monkeypatch, cols, rows):
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=(80, 24): __import__("os").terminal_size((cols, rows)))


def test_resize_release_erases_below_cursor_not_the_stale_row(monkeypatch):
    """A taller window moves the old bar off its old row: the emulator pulls
    scrollback in and shifts everything (cursor included) down. Clearing the
    old absolute row would erase a content line and leave the stale bar for
    establish's index to scroll into the output. The bar is always below the
    content cursor, so erase from there instead."""
    _term_size(monkeypatch, 80, 24)
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    buf.reset()
    _term_size(monkeypatch, 80, 40)
    progress._on_sigwinch()
    progress._ticker_step()
    frame = buf.writes[0]
    assert "\x1b[24;1H" not in frame               # never address the stale row
    erase = frame.index("\x1b[r\x1b8\x1b[J")       # reset region, back to content, erase below
    assert erase < frame.index("\x1b[1;39r")       # ...before the new region goes up


def test_shutdown_with_resize_pending_erases_below_cursor(monkeypatch):
    _term_size(monkeypatch, 80, 24)
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    buf.reset()
    _term_size(monkeypatch, 80, 40)
    progress._on_sigwinch()
    progress.shutdown()
    assert "\x1b[24;1H" not in buf.getvalue()
    assert "\x1b8\x1b[J" in buf.getvalue()


def test_resize_during_child_yield_applies_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    with progress.yield_terminal("child"):
        monkeypatch.setattr(shutil, "get_terminal_size",
                            lambda fallback=(80, 24): __import__("os").terminal_size((100, 40)))
        progress._on_sigwinch()
        buf.reset()
    assert "\x1b[1;39r" in buf.getvalue()
    assert progress._bar.resize_pending is False


# --- Region trace (3.2.0-B11) -----------------------------------------------

def _trace_lines(path):
    return [ln.split(None, 1)[1] for ln in path.read_text().splitlines() if ln.strip()]


def test_region_trace_records_establish_and_release(tmp_path, monkeypatch):
    """The bar's disappearance has two candidate explanations that look
    identical on screen: the region was released, or it was never repainted.
    The raw pty dump (3.2.0-B9) ruled out the container scrolling over the
    row, so the remaining question is on sysforge's own side, and only a
    record of the transitions can separate the two."""
    trace = tmp_path / "trace.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    progress.shutdown()
    events = _trace_lines(trace)
    assert any(e.startswith("establish") for e in events), events
    assert any(e.startswith("paint") for e in events), events
    assert any(e.startswith("release") for e in events), events


def test_region_trace_paint_records_the_row_and_text(tmp_path, monkeypatch):
    trace = tmp_path / "trace.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("compiling mesa")
    paint = [e for e in _trace_lines(trace) if e.startswith("paint")][0]
    assert "row=" in paint and "compiling mesa" in paint


def test_region_trace_is_off_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSFORGE_PROGRESS_TRACE", raising=False)
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")
    assert list(tmp_path.iterdir()) == []


def test_region_trace_failure_never_breaks_the_run(tmp_path, monkeypatch):
    """A diagnostic must not be able to take down the thing it observes."""
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(tmp_path / "no" / "dir" / "t.log"))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("x")  # must not raise


# --- Heartbeat (3.2.0-B13) ----------------------------------------------------

def test_heartbeat_repaints_without_advancing_the_counter(tmp_path, monkeypatch):
    """3.2.0-B13. A whole package build sits inside one ``tick()``, so the bar
    was painted once and left untouched for the build's entire duration — the
    trace showed a 379s gap between paints against a 4s runner-up. The
    heartbeat sets live detail; the scheduled repaint shows it."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(1, "building") as tick:
        tick("mesa-sysforge")
        progress.heartbeat("[220/900] Compiling rusticl")
        progress.refresh()
    paints = [e for e in _trace_lines(trace) if e.startswith("paint")]
    assert "[1/1]" in paints[-1], paints[-1]
    assert "building · mesa-sysforge" in paints[-1]
    # A ninja status reads as its position (3.5.0-F6).
    assert paints[-1].endswith("· ninja 24%'"), paints[-1]


def test_heartbeat_itself_does_not_paint(monkeypatch):
    """3.3.0-F2: heartbeat only sets detail — the scheduled owner paints."""
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    buf.reset()
    progress.heartbeat("cc foo.c")
    assert buf.writes == []
    assert progress._bar.detail == "cc foo.c"


def test_heartbeat_does_not_stack_detail_across_calls(tmp_path, monkeypatch):
    """It fires every 30s for the length of a build; appending to the previous
    painted text would grow the line without bound."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(1, "building") as tick:
        tick("mesa")
        progress.heartbeat("first")
        progress.refresh()
        progress.heartbeat("second")
        progress.refresh()
    last = [e for e in _trace_lines(trace) if e.startswith("paint")][-1]
    assert "second" in last and "first" not in last


def test_heartbeat_before_any_status_is_a_noop(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.heartbeat("x")  # must not raise
    progress.refresh()
    progress._ticker_step()
    assert buf.writes == []  # never establishes a region on its own


def test_heartbeat_is_silent_in_plain_mode(monkeypatch):
    """Plain mode writes through log.ui(), so a 30s repaint would append a
    line to the log forever; the per-package log already gets the heartbeat."""
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    with progress.tracker(1, "building") as tick:
        tick("mesa")
        before = buf.getvalue()
        progress.heartbeat("[220/900] Compiling")
        progress.refresh()
        progress._ticker_step()
        assert buf.getvalue() == before


def test_heartbeat_survives_a_later_tick(tmp_path, monkeypatch):
    """The next real tick must replace the heartbeat detail, not inherit it."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "building") as tick:
        tick("mesa")
        progress.heartbeat("compiling")
        progress.refresh()
        tick("volk")
    paints = [e for e in _trace_lines(trace) if e.startswith("paint") and "volk" in e]
    assert paints and "compiling" not in paints[-1]


# --- elapsed / ETA suffix (3.2.0-F14) ---------------------------------------

def test_no_time_suffix_for_a_fast_batch(monkeypatch):
    """Under the 5s floor there is nothing worth saying, so the line is
    byte-identical to the pre-F14 shape."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(3, "building") as tick:
        tick("a")
        advance(1)
        tick("b")
    assert lines[-1] == "[PROGRESS] [2/3] building · b"


def test_elapsed_shown_without_eta_after_one_completion(monkeypatch):
    """One completed item is a single sample — show the clock, never a rate."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(4, "source sync") as tick:
        tick("a")
        advance(30)
        tick("b")
    assert lines[-1] == "[PROGRESS] [2/4] source sync · b · 30s"


def test_eta_shown_after_two_completions(monkeypatch):
    """Two items in 20s = 10s/item; two items remain (c in flight, plus d)."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(4, "source sync") as tick:
        tick("a")
        advance(10)
        tick("b")
        advance(10)
        tick("c")
    assert lines[-1] == "[PROGRESS] [3/4] source sync · c · 20s · ~20s left"


def test_single_item_batch_never_shows_eta(monkeypatch):
    """total=1 can never reach two completions; the clock still runs."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(1, "building") as tick:
        advance(90)
        tick("mesa")
    assert lines[-1] == "[PROGRESS] [1/1] building · mesa · 1m30s"


def test_eta_omitted_once_the_estimate_is_overrun(monkeypatch):
    """An overrun estimate is stale, not negative — drop it rather than
    print '~0s left' for the rest of a long item."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(3, "building") as tick:
        tick("a")
        advance(10)
        tick("b")
        advance(10)
        tick("c")          # 2 done in 20s → 10s/item, 1 item left → ~10s
        advance(600)       # c blows straight through it
        tick.resume()
    assert lines[-1] == "[PROGRESS] [3/3] building · c · 10m20s"


def test_scheduled_repaint_advances_the_clock_mid_item(monkeypatch):
    """A whole build sits inside one tick; the bar must not freeze."""
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "building") as tick:
        advance(30)
        tick("mesa")
        advance(120)
        buf.reset()
        progress.heartbeat("cc mesa.c")
        progress._ticker_step()
    painted = buf.getvalue()
    assert "2m30s" in painted
    assert "cc mesa.c" in painted


def test_suffix_cleared_after_tracker_exits(monkeypatch):
    """A line composed outside any tracker carries no stale clock."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(2, "building") as tick:
        advance(60)
        tick("a")
    assert progress._bar.tracker is None
    progress.phase("unrelated")
    assert lines[-1] == "[PROGRESS] unrelated"


# --- Pause (3.3.0-F2) -------------------------------------------------------

def test_prompt_mid_item_is_excluded_from_elapsed_and_rate(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(4, "sync") as tick:
        tick("a")
        advance(10)
        tick("b")
        advance(4)
        with progress.yield_terminal("prompt"):
            advance(100)     # the user takes their time answering
        advance(6)
        tick("c")
        # 20 active seconds for two items → 10s/item, two left (c + d).
        assert _composed() == "[SYSFORGE][PROGRESS] [3/4] sync · c · 20s · ~20s left"


def test_nested_yields_pause_once(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "b") as tick:
        tick("a")
        advance(10)
        with progress.yield_terminal("prompt"):
            advance(50)
            with progress.yield_terminal("child"):
                advance(50)
        t = progress._bar.tracker
        assert t is not None
        assert t.paused_total == 100
        assert t.paused_since is None
        assert _composed() == "[SYSFORGE][PROGRESS] [1/2] b · a · 10s"


def test_tracker_opened_while_yielded_starts_paused(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with contextlib.ExitStack() as outer:
        yield_stack = contextlib.ExitStack()
        yield_stack.enter_context(progress.yield_terminal("prompt"))
        tick = outer.enter_context(progress.tracker(2, "b"))
        tick("a")
        advance(100)                 # still yielded: none of this counts
        yield_stack.close()
        advance(10)
        assert _composed() == "[SYSFORGE][PROGRESS] [1/2] b · a · 10s"


def test_pause_without_yield_freezes_the_clock_but_still_paints(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "b") as tick:
        tick("a")
        advance(10)
        progress._pause()
        try:
            advance(100)
            buf.reset()
            tick("z")
            assert "[2/2] b · z · 10s" in buf.getvalue()
        finally:
            progress._unpause()


def test_plain_mode_never_pauses(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    lines = _plain_lines(monkeypatch)
    with progress.tracker(1, "b") as tick:
        with progress.yield_terminal("prompt"):
            advance(30)
        tick("a")
    assert lines[-1] == "[PROGRESS] [1/1] b · a · 30s"


# --- Scheduled repaint (3.3.0-F2) ---------------------------------------------

def test_ticker_step_paints_only_on_change(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "b") as tick:
        tick("a")
        buf.reset()
        progress._ticker_step()
        assert buf.writes == []        # nothing changed: no bytes
        advance(10)
        progress._ticker_step()
        assert len(buf.writes) == 1 and "10s" in buf.writes[0]


def test_ticker_step_skips_while_forwarding(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "b") as tick:
        tick("a")
        with progress.forwarding_output():
            advance(10)
            buf.reset()
            progress._ticker_step()
            assert buf.writes == []    # the forwarder owns repaint now
            progress.refresh()
            assert len(buf.writes) == 1 and "10s" in buf.writes[0]


def test_no_ticker_thread_in_plain_mode(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    monkeypatch.setattr(progress, "_ticker_enabled", True)
    progress.init()
    progress.phase("x")
    assert progress._ticker_thread is None


def test_ticker_thread_starts_survives_errors_and_stops(tmp_path, monkeypatch):
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setattr(progress, "_ticker_enabled", True)
    monkeypatch.setattr(progress, "_TICK_S", 0.01)
    calls = []

    def _flaky_step():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(progress, "_ticker_step", _flaky_step)
    progress.init()
    progress.phase("x")              # first TTY draw starts the ticker
    thread = progress._ticker_thread
    assert thread is not None and thread.is_alive()
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) >= 3           # kept going after the exception
    progress.shutdown()
    assert not thread.is_alive()
    assert progress._ticker_thread is None
    assert any(e.startswith("ticker-error") for e in _trace_lines(trace))


# --- One tracker at a time (3.3.0-F2) -----------------------------------------

def test_nested_tracker_raises_in_strict_mode(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "outer"), \
            pytest.raises(RuntimeError, match="tracker 'inner' opened inside 'outer'"), \
            progress.tracker(1, "inner"):
        pass


def test_nested_tracker_is_a_noop_in_lenient_mode(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    monkeypatch.setattr(progress, "_strict_nesting", False)
    progress.init()
    with progress.tracker(2, "outer") as outer_tick:
        outer_tick("a")
        with progress.tracker(5, "inner") as inner_tick:
            assert isinstance(inner_tick, progress_hooks._NoOpTick)
            inner_tick("ignored")
        t = progress._bar.tracker
        assert t is not None and t.prefix == "outer" and t.index == 1


def test_require_no_tracker(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.require_no_tracker("build_and_install")   # nothing open: fine
    with progress.tracker(1, "building"):
        with pytest.raises(RuntimeError, match="build_and_install"):
            progress.require_no_tracker("build_and_install")
        monkeypatch.setattr(progress, "_strict_nesting", False)
        progress.require_no_tracker("build_and_install")  # lenient: logs only


def test_a_recovery_menu_prompt_pauses_an_open_tracker(monkeypatch):
    """The build-failure recovery menu prompts from inside the ``building``
    tracker through primitives/prompt.py; the time the user spends deciding
    is not build time."""
    from sysforge.primitives import prompt

    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)

    def slow_answer(_msg=""):
        advance(300)
        return "s"

    monkeypatch.setattr("builtins.input", slow_answer)
    with progress.tracker(2, "building") as tick:
        tick("mesa")
        advance(10)
        assert prompt.prompt_choice("[r]etry/[s]kip? ", choices=("r", "s")) == "s"
        assert _composed() == "[SYSFORGE][PROGRESS] [1/2] building · mesa · 10s"


# --- Per-item expected durations (3.3.0-F1) ----------------------------------

def test_expected_eta_shows_from_the_first_item(monkeypatch):
    """With history there is no need to wait for two completions."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(3, "building", expected=[1200, 30, 30]) as tick:
        tick("mesa")
        advance(200)
        # mesa 1200-200 left + 30 + 30
        assert _composed() == "[SYSFORGE][PROGRESS] [1/3] building · mesa · 3m20s · ~17m40s left"


def test_expected_eta_stays_steady_across_a_large_completion(monkeypatch):
    """A 20-minute package finishing must not swing the figure for the small
    ones behind it: each counts at its own median, never a batch mean."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(4, "building", expected=[30, 1200, 30, 30]) as tick:
        tick("lib-a")
        advance(30)
        tick("mesa")
        advance(1200)
        tick("lib-b")
        # Old mean projection would read (1230/2)*2 = ~20m30s here.
        assert _composed() == "[SYSFORGE][PROGRESS] [3/4] building · lib-b · 20m30s · ~1m00s left"
        advance(10)
        assert _composed().endswith("· ~50s left")


def test_expected_item_overrun_counts_as_zero_not_negative(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(2, "building", expected=[30, 60]) as tick:
        tick("a")
        advance(500)       # a overruns its 30s median
        assert _composed().endswith("· ~1m00s left")   # only b remains
        tick("b")
        advance(90)        # last item overruns: estimate dropped
        assert "left" not in _composed()


def test_expected_unknown_item_uses_in_run_mean_once_available(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(4, "building", expected=[10, 10, None, 100]) as tick:
        tick("a")
        advance(10)
        tick("b")
        # one completion: no mean yet for the unknown item → withheld
        assert "left" not in (_composed() or "")
        advance(10)
        tick("c")
        # mean 10s; c in flight at 10 + d 100
        assert _composed().endswith("· ~1m50s left")


def test_no_history_batch_matches_the_mean_projection(monkeypatch):
    """expected all-None is today's behaviour, byte for byte."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.tracker(4, "sync", expected=[None] * 4) as tick:
        tick("a")
        advance(10)
        tick("b")
        advance(10)
        tick("c")
        assert _composed() == "[SYSFORGE][PROGRESS] [3/4] sync · c · 20s · ~20s left"


def test_expected_with_wrong_length_is_ignored(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(3, "b", expected=[1, 2]):
        assert progress._bar.tracker.expected is None


def test_paused_seconds_accumulates_with_or_without_a_tracker(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    start = progress.paused_seconds()
    with progress.yield_terminal("prompt"):
        advance(40)
        assert progress.paused_seconds() - start == 40   # in progress counts
    advance(5)
    with progress.tracker(1, "b"), progress.yield_terminal("prompt"):
        advance(15)
    assert progress.paused_seconds() - start == 55


# --- Stage steps (3.5.0-F6) --------------------------------------------------

_PLAN = ["preflight", "optimize", "audit"]


def test_steps_line_carries_step_item_ninja_and_stage_clock(monkeypatch):
    """The target frame: stage position, step, item, ninja percentage, then the
    stage's elapsed and ETA (the rest of this step plus every later one)."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.steps("toolchain", _PLAN,
                        expected={"preflight": 10, "optimize": 100, "audit": 20}):
        progress.step("preflight")
        advance(10)
        progress.step("optimize")
        with progress.tracker(1, "optimize") as tick:
            tick("llvm")
            progress.heartbeat("[7412/10023] Building CXX object lib/Foo.cpp.o")
            advance(50)
            assert _composed() == ("[SYSFORGE][PROGRESS] toolchain 2/3 · optimize · llvm"
                                   " · ninja 73% · 1m00s · ~1m10s left")


def test_steps_eta_withheld_while_a_remaining_step_has_no_history(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.steps("toolchain", _PLAN, expected={"preflight": 10, "optimize": 100}):
        progress.step("preflight")
        progress.step("optimize")
        advance(30)
        assert _composed() == "[SYSFORGE][PROGRESS] toolchain 2/3 · optimize · 30s"


def test_steps_eta_from_history_on_the_first_item(monkeypatch):
    """Unlike a tracker's in-run mean, recorded history gives an ETA at once."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    with progress.steps("toolchain", ["build"], expected={"build": 600}):
        progress.step("build")
        with progress.tracker(1, "build") as tick:
            tick("llvm")
            advance(6)
            assert _composed().endswith("· llvm · 6s · ~9m54s left")


def test_steps_show_a_counter_only_when_it_counts_more_than_one(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.steps("toolchain", ["audit"]):
        progress.step("audit")
        with progress.tracker(18, "audit") as tick:
            for name in ["a", "b", "lldb"]:
                tick(name)
            assert _composed() == "[SYSFORGE][PROGRESS] toolchain 1/1 · audit · [3/18] lldb"
            tick.note("BOLT")
            assert _composed().endswith("· audit · [3/18] BOLT")


def test_step_drops_planned_steps_the_run_skipped(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.steps("toolchain", ["preflight", "instrument", "train", "optimize", "audit"]):
        progress.step("preflight")
        progress.step("optimize")           # profdata reused: 1-3 never ran
        assert progress._bar.steps.names == ["preflight", "optimize", "audit"]
        assert _composed() == "[SYSFORGE][PROGRESS] toolchain 2/3 · optimize"


def test_step_inserts_an_unplanned_step_next(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.steps("toolchain", ["audit", "install", "verify", "bench"]):
        progress.step("verify")
        progress.step("consumers")          # Gate 2 found consumers to rebuild
        assert progress._bar.steps.names == ["verify", "consumers", "bench"]
        progress.step("consumers")          # repeating the current step is a no-op
        assert progress._bar.steps.index == 1


def test_plan_steps_replaces_only_what_follows_the_current_step(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.steps("toolchain", ["preflight", "instrument", "bootstrap", "train"]):
        progress.step("preflight")
        progress.plan_steps(["instrument", "train", "optimize", "audit"])
        assert progress._bar.steps.names == ["preflight", "instrument", "train",
                                             "optimize", "audit"]


def test_step_durations_reported_without_paused_time(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    done = []
    with progress.steps("toolchain", _PLAN, on_done=lambda n, s: done.append((n, s))):
        progress.step("preflight")
        advance(5)
        with progress.yield_terminal("prompt"):   # a confirm prompt is not work
            advance(300)
        advance(5)
        progress.step("optimize")
        advance(100)
    assert done == [("preflight", 10.0), ("optimize", 100.0)]


def test_a_failing_step_is_not_reported(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    advance = _fake_clock(monkeypatch)
    done = []
    with pytest.raises(RuntimeError), \
         progress.steps("toolchain", _PLAN, on_done=lambda n, s: done.append(n)):
        progress.step("preflight")
        advance(5)
        progress.step("optimize")
        raise RuntimeError("build failed")
    assert done == ["preflight"]
    assert progress._bar.steps is None


def test_a_failing_timing_callback_never_breaks_the_run(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()

    def boom(_name, _secs):
        raise OSError("read-only state dir")
    with progress.steps("toolchain", _PLAN, on_done=boom):
        progress.step("preflight")
        progress.step("optimize")


def test_steps_show_a_phase_but_not_the_stage_name_phase(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("toolchain")             # what the pipeline runner sets
    with progress.steps("toolchain", ["audit", "install"]):
        progress.step("audit")
        assert _composed() == "[SYSFORGE][PROGRESS] toolchain 1/2 · audit"
        progress.phase("ABI scan, 18 packages")
        assert _composed().endswith("· audit · ABI scan, 18 packages")
        progress.step("install")            # a step change clears the phase
        assert _composed().endswith("toolchain 2/2 · install")
    assert progress._bar.phase == "toolchain"   # restored for the runner


def test_steps_inside_steps_raise_under_tests(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.steps("toolchain", _PLAN), pytest.raises(RuntimeError), \
            progress.steps("kernel", ["build"]):
        pass


def test_step_without_open_steps_is_a_noop(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.step("optimize")
    progress.plan_steps(["audit"])
    assert _composed() is None


def test_plain_mode_logs_one_line_per_step(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    lines = _plain_lines(monkeypatch)
    progress.init()
    with progress.steps("toolchain", _PLAN):
        progress.step("preflight")
        progress.step("optimize")
    assert lines == ["[PROGRESS] toolchain 1/3 · preflight",
                     "[PROGRESS] toolchain 2/3 · optimize"]


# --- ninja heartbeat (3.5.0-F6) ----------------------------------------------

def _detail_after(*heartbeats):
    for hb in heartbeats:
        progress.heartbeat(hb)
    return progress._bar.detail


def test_ninja_status_reads_as_a_percentage(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert _detail_after("[2128/10023] Building SPIRVGenPreLegalizeGICombiner.inc...") \
        == "ninja 21%"


def test_ninja_sub_build_restart_is_named_beside_the_main_build(monkeypatch):
    """llvm's 23.1.3 build, as logged: the runtimes' own ninja (1900 steps)
    starts partway through the main one (10023), which then resumes."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert _detail_after(
        "[2128/10023] Building SPIRVGenPreLegalizeGICombiner.inc...",
        "[1895/1900] Building CXX object libcxx/src/CMakeFiles/cxx_static.dir/x.cpp.o",
    ) == "ninja 21% · libcxx 99%"
    assert _detail_after("[7939/10023] Building CXX object tools/lld/x.cpp.o") == "ninja 79%"
    # A sub-build whose path names nothing usable stays generic.
    assert _detail_after("[444/445] Generating nvptx64-nvidia-cuda/libclc.bc") \
        == "ninja 79% · sub-build 99%"


def test_a_new_item_forgets_the_last_items_ninja_builds(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "build") as tick:
        tick("llvm")
        _detail_after("[9000/10023] x")
        tick("spirv-llvm-translator")
        assert _detail_after("[10/400] y") == "ninja 2%"


def test_non_ninja_heartbeat_passes_through(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert _detail_after("no output for ~30s") == "no output for ~30s"
    assert _detail_after(" 77 159.3M  77 123.4M") == " 77 159.3M  77 123.4M"


def test_plain_mode_steps_never_log_heartbeat_detail(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    lines = _plain_lines(monkeypatch)
    progress.init()
    with progress.steps("toolchain", ["preflight", "optimize"]):
        progress.step("preflight")
        progress.heartbeat("[10/100] x")
        progress.plan_steps(["optimize", "audit"])   # repaints without an event
    assert all("ninja" not in ln for ln in lines), lines


def test_step_timing_callback_runs_without_the_bar_lock(monkeypatch):
    """on_done writes a file; _lock is never held across a blocking call."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    held = []

    def on_done(_name, _secs):
        # Another thread can take the lock only if this caller does not hold it.
        import threading
        res = []

        def probe():
            ok = progress._lock.acquire(timeout=0.5)
            res.append(ok)
            if ok:
                progress._lock.release()
        th = threading.Thread(target=probe)
        th.start()
        th.join()
        held.append(not res[0])
    with progress.steps("toolchain", ["a", "b"], on_done=on_done):
        progress.step("a")
        progress.step("b")
    assert held == [False, False]


def test_a_phase_change_forgets_ninja_totals(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    _detail_after("[9000/10023] x")
    progress.phase("repackaging")
    assert _detail_after("[10/400] y") == "ninja 2%"


def test_a_second_build_in_one_step_reads_its_own_percentage(monkeypatch):
    """3.5.0-B91: the --bolt-only repackage runs with no tick or phase change."""
    _fake_tty_stderr(monkeypatch)
    progress.init()
    _detail_after("[9000/10023] x")
    progress.build_started()
    assert _detail_after("[10/400] y") == "ninja 2%"


def test_progress_module_still_satisfies_the_hooks_protocol():
    from sysforge.primitives import progress_hooks
    assert isinstance(progress, progress_hooks.ProgressHooks)
