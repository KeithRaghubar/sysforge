"""
test_progress.py — tests for sysforge.ui.progress.

Verifies:
  - mode detection: stderr-is-tty / not-tty / TERM=dumb / CI / NO_COLOR / dry-run
  - plain mode emits [PROGRESS] via log.ui() and writes no ANSI to stderr
  - TTY mode writes DECSTBM scroll-region escapes to stderr
  - tracker() increments correctly and releases on exit
  - clear() is idempotent and survives being called with no region reserved
"""
import io
import sys

import pytest

from sysforge import log
from sysforge.ui import progress


def _reset_progress_state():
    """Force progress to re-detect mode on next use."""
    progress._mode = None
    progress._reserved = False
    progress._last_status = None
    progress._phase = None


@pytest.fixture(autouse=True)
def _clean_progress_between_tests(monkeypatch):
    _reset_progress_state()
    monkeypatch.setattr(log, "_DRY_RUN", False)
    for var in ("CI", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    yield
    progress.shutdown()
    _reset_progress_state()


def _fake_tty_stderr(monkeypatch) -> io.StringIO:
    """Install a stderr that claims isatty() == True and captures writes."""
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


def _fake_plain_stderr(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    buf.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


# --- Mode detection ---------------------------------------------------------

def test_mode_plain_when_stderr_not_tty(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    assert progress._mode == "plain"


def test_mode_tty_when_dry_run_on_a_tty(monkeypatch):
    """3.1.0-B7: a dry run renders progress exactly as the real run it previews.
    The log-to-stdout redirect it used to be paired with cannot interleave with
    progress, which writes to stderr."""
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setattr(log, "_DRY_RUN", True)
    progress.init()
    assert progress._mode == "tty"


def test_mode_plain_when_dry_run_off_tty(monkeypatch):
    """The genuine non-interactive rungs still apply to a dry run — a scripted
    dry run redirected to a file gets plain output from the isatty rung."""
    _fake_plain_stderr(monkeypatch)
    monkeypatch.setattr(log, "_DRY_RUN", True)
    progress.init()
    assert progress._mode == "plain"


def test_mode_plain_when_term_dumb(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("TERM", "dumb")
    progress.init()
    assert progress._mode == "plain"


def test_mode_plain_when_ci_set(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("CI", "1")
    progress.init()
    assert progress._mode == "plain"


def test_mode_plain_when_no_color_set(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    progress.init()
    assert progress._mode == "plain"


def test_mode_tty_when_all_conditions_met(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    assert progress._mode == "tty"


# --- Plain mode rendering ---------------------------------------------------

def test_plain_mode_emits_no_ansi(monkeypatch):
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.render(3, 10, "building htop")
    assert "\x1b" not in buf.getvalue()


def test_plain_mode_routes_through_log_ui(monkeypatch):
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.render(3, 10, "building htop")
    out = buf.getvalue()
    assert "[PROGRESS]" in out
    assert "[3/10] building htop" in out


# --- TTY mode rendering -----------------------------------------------------

def test_tty_mode_emits_scroll_region(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 2, "x")
    written = buf.getvalue()
    # DECSTBM set region: ESC[1;Nr
    assert "\x1b[1;" in written and "r" in written
    # Some text painted on the bottom row
    assert "1/2" in written
    assert "x" in written


def test_tty_mode_release_on_clear(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 1, "x")
    buf.truncate(0)
    buf.seek(0)
    progress.clear()
    written = buf.getvalue()
    # ESC[r resets scroll region
    assert "\x1b[r" in written
    assert progress._reserved is False


def test_tty_mode_shutdown_idempotent(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 1, "x")
    progress.shutdown()
    progress.shutdown()  # must not raise


# --- tracker() context manager ----------------------------------------------

def test_tracker_increments_counter(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = []
    # Capture via monkeypatching render
    def _capture(i, n, label):
        seen.append((i, n, label))
    monkeypatch.setattr(progress, "render", _capture)
    with progress.tracker(3, "building") as tick:
        tick("a")
        tick("b")
        tick("c")
    assert seen == [
        (0, 3, "building · starting..."),
        (1, 3, "building · a"),
        (2, 3, "building · b"),
        (3, 3, "building · c"),
    ]


def test_tracker_note_and_resume(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = []
    monkeypatch.setattr(progress, "render", lambda i, n, label: seen.append((i, n, label)))
    with progress.tracker(3, "building") as tick:
        tick("htop")
        # Overlay a transient sub-step at the current count (no increment) ...
        tick.note("installing 2 intra-batch dep(s) for htop")
        # ... then hand the line back to the last tick state.
        tick.resume()
    assert seen == [
        (0, 3, "building · starting..."),
        (1, 3, "building · htop"),
        (1, 3, "installing 2 intra-batch dep(s) for htop"),
        (1, 3, "building · htop"),
    ]


def test_tracker_resume_before_first_tick_is_noop(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = []
    monkeypatch.setattr(progress, "render", lambda i, n, label: seen.append((i, n, label)))
    with progress.tracker(2, "building") as tick:
        tick.resume()  # no tick yet — must not repaint
    # Only the entry placeholder; resume() emitted nothing.
    assert seen == [(0, 2, "building · starting...")]


def test_tracker_releases_region_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(2, "p") as tick:
        tick("one")
        tick("two")
    # After exit, region should be reset.
    assert "\x1b[r" in buf.getvalue()
    assert progress._reserved is False


def test_tracker_releases_on_exception(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with pytest.raises(RuntimeError), progress.tracker(3, "p") as tick:
        tick("a")
        raise RuntimeError("boom")
    assert progress._reserved is False


# --- clear() safety ---------------------------------------------------------

def test_clear_without_reservation_is_safe(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.clear()  # no prior render — must not raise or write garbage


def test_render_reestablishes_after_clear(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 2, "x")
    progress.clear()
    buf.truncate(0)
    buf.seek(0)
    progress.render(2, 2, "y")
    written = buf.getvalue()
    assert "\x1b[1;" in written  # region re-established
    assert "2/2" in written


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
    assert progress._phase is None
    assert progress._reserved is False
    assert "\x1b[r" in buf.getvalue()


def test_tracker_restores_enclosing_phase_on_exit(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("dep prep")
    buf.truncate(0)
    buf.seek(0)
    with progress.tracker(1, "building") as tick:
        tick("a")
    # Tracker exit repaints the phase instead of releasing the region.
    assert progress._reserved is True
    assert "\x1b[r" not in buf.getvalue()
    written = buf.getvalue()
    assert written.rindex("dep prep") > written.rindex("building")


def test_tracker_still_releases_without_phase(monkeypatch):
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.tracker(1, "building") as tick:
        tick("a")
    assert progress._reserved is False
    assert "\x1b[r" in buf.getvalue()


def test_suspend_for_prompt_keeps_region(monkeypatch):
    # suspend_for_prompt() blanks the bar line in place but must NOT reset the
    # scroll region (no ESC[r) and must keep _reserved True — the prompt then
    # prints in the live content flow, and the next render repaints the bar.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")   # reserve + paint
    buf.truncate(0)
    buf.seek(0)
    progress.suspend_for_prompt()
    written = buf.getvalue()
    assert progress._RESET_REGION not in written   # region NOT released
    assert progress._reserved is True
    assert progress._CLEAR_LINE in written          # bar line blanked
    # Balanced cursor save/restore — the content cursor is left untouched.
    assert written.count(progress._SAVE) == written.count(progress._RESTORE)


def test_suspend_for_prompt_safe_without_region(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.suspend_for_prompt()  # no region reserved — must be a no-op


def test_suspended_releases_then_restores_region(monkeypatch):
    # suspended() fully releases the region for the body (so a TTY-inheriting
    # subprocess gets a clean terminal) and re-establishes + repaints on exit.
    buf = _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.phase("building")
    assert progress._reserved is True
    buf.truncate(0)
    buf.seek(0)
    with progress.suspended():
        # Inside the body the region is released (full clear).
        assert progress._reserved is False
        assert progress._RESET_REGION in buf.getvalue()
    # On exit the region is re-established and the bar repainted.
    assert progress._reserved is True
    assert "building" in buf.getvalue()


def test_suspended_noop_without_region(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    with progress.suspended():  # nothing reserved — clean no-op
        assert progress._reserved is False
    assert progress._reserved is False


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
    # Each step is present, in order, before the region is set.
    bottom_jump = f"\x1b[{progress._rows};1H"
    i_save = written.index(progress._SAVE)
    i_jump = written.index(bottom_jump, i_save)
    i_index = written.index(progress._INDEX, i_jump)
    i_region = written.index(f"\x1b[1;{progress._rows - 1}r", i_index)
    i_restore = written.index(progress._RESTORE, i_region)
    i_up = written.index("\x1b[1A", i_restore)
    assert i_save < i_jump < i_index < i_region < i_restore < i_up
    # Cursor handling stays balanced.
    assert written.count(progress._SAVE) == written.count(progress._RESTORE)


def test_phase_plain_mode_dedupes_repeats(monkeypatch):
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = []
    monkeypatch.setattr(log, "ui", lambda tag, msg: seen.append(msg))
    progress.phase("version check")
    progress.phase("version check")
    progress.phase("drift check")
    assert seen == ["[PROGRESS] version check", "[PROGRESS] drift check"]




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
    progress.render(1, 2, "building")
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
    progress.render(1, 2, "compiling mesa")
    paint = [e for e in _trace_lines(trace) if e.startswith("paint")][0]
    assert "row=" in paint and "compiling mesa" in paint


def test_region_trace_is_off_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSFORGE_PROGRESS_TRACE", raising=False)
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 2, "x")
    assert list(tmp_path.iterdir()) == []


def test_region_trace_failure_never_breaks_the_run(tmp_path, monkeypatch):
    """A diagnostic must not be able to take down the thing it observes."""
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(tmp_path / "no" / "dir" / "t.log"))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 2, "x")  # must not raise


# --- Heartbeat repaint (3.2.0-B13) ------------------------------------------

def test_heartbeat_repaints_without_advancing_the_counter(tmp_path, monkeypatch):
    """3.2.0-B13. A whole package build sits inside one ``tick()``, so the bar
    was painted once and left untouched for the build's entire duration — the
    trace showed a 379s gap between paints against a 4s runner-up. The
    heartbeat repaints the same count with live detail."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 1, "building · mesa-sysforge")
    progress.heartbeat("[220/900] Compiling rusticl")
    paints = [e for e in _trace_lines(trace) if e.startswith("paint")]
    assert "[1/1]" in paints[-1], paints[-1]
    assert "building · mesa-sysforge" in paints[-1]
    # Truncated to the terminal width, so match the head of the detail.
    assert "[220/900] Compiling" in paints[-1]


def test_heartbeat_does_not_stack_detail_across_calls(tmp_path, monkeypatch):
    """It fires every 30s for the length of a build; appending to the previous
    painted text would grow the line without bound."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 1, "building · mesa")
    progress.heartbeat("first")
    progress.heartbeat("second")
    last = [e for e in _trace_lines(trace) if e.startswith("paint")][-1]
    assert "second" in last and "first" not in last


def test_heartbeat_before_any_status_is_a_noop(monkeypatch):
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.heartbeat("x")  # must not raise


def test_heartbeat_is_silent_in_plain_mode(monkeypatch):
    """Plain mode writes through log.ui(), so a 30s repaint would append a
    line to the log forever; the per-package log already gets the heartbeat."""
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.render(1, 1, "building · mesa")
    before = buf.getvalue()
    progress.heartbeat("[220/900] Compiling")
    assert buf.getvalue() == before


def test_heartbeat_survives_a_later_render(tmp_path, monkeypatch):
    """The next real tick must replace the heartbeat detail, not inherit it."""
    trace = tmp_path / "t.log"
    monkeypatch.setenv("SYSFORGE_PROGRESS_TRACE", str(trace))
    _fake_tty_stderr(monkeypatch)
    progress.init()
    progress.render(1, 2, "building · mesa")
    progress.heartbeat("compiling")
    progress.render(2, 2, "building · volk")
    last = [e for e in _trace_lines(trace) if e.startswith("paint")][-1]
    assert "volk" in last and "compiling" not in last
