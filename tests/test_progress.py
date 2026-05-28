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


def test_mode_plain_when_dry_run(monkeypatch):
    _fake_tty_stderr(monkeypatch)
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
    def _capture(i, n, label, phase=None):
        seen.append((i, n, label))
    monkeypatch.setattr(progress, "render", _capture)
    with progress.tracker(3, "building") as tick:
        tick("a")
        tick("b")
        tick("c")
    assert seen == [
        (0, 3, "building starting..."),
        (1, 3, "building a"),
        (2, 3, "building b"),
        (3, 3, "building c"),
    ]


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
    with pytest.raises(RuntimeError):
        with progress.tracker(3, "p") as tick:
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


# --- phase-aware nested format ---------------------------------------------

def test_render_plain_with_phase(monkeypatch):
    """phase=(idx, total) prepends a [idx/total] block in plain mode."""
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.render(2, 5, "build llvm", phase=(1, 4))
    out = buf.getvalue()
    assert "[PROGRESS]" in out
    assert "[1/4][2/5] build llvm" in out


def test_render_plain_no_phase_unchanged(monkeypatch):
    """phase=None preserves the historical `[i/n] label` format exactly."""
    buf = _fake_plain_stderr(monkeypatch)
    progress.init()
    progress.render(3, 10, "building htop")
    out = buf.getvalue()
    # Critical: no stray bracket pair before [3/10].
    assert "[PROGRESS] [3/10] building htop" in out


def test_tracker_forwards_phase(monkeypatch):
    """tracker() with phase forwards it into every render() call (start + ticks)."""
    _fake_plain_stderr(monkeypatch)
    progress.init()
    seen = []

    def _capture(i, n, label, phase=None):
        seen.append((i, n, label, phase))

    monkeypatch.setattr(progress, "render", _capture)
    with progress.tracker(2, "PGO 2/4", phase=(2, 4)) as tick:
        tick("clang")
        tick("lld")
    assert seen == [
        (0, 2, "PGO 2/4 starting...", (2, 4)),
        (1, 2, "PGO 2/4 clang", (2, 4)),
        (2, 2, "PGO 2/4 lld", (2, 4)),
    ]
