# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
ui/progress.py — bottom-anchored batch progress indicator.

Dual-mode renderer, picked once at init():
  TTY mode   — DECSTBM scroll region reserves the bottom row; other
               output (including subprocess output that inherits the
               TTY, e.g. makepkg / git / pacman) scrolls above it.
  Plain mode — non-TTY / TERM=dumb / CI / NO_COLOR. Emits
               '[PROGRESS] [i/n] label' through log.ui() so the same
               data still reaches the user and the log file without
               ANSI garbage in pipes or journald.

One owner per concern (3.3.0-F2):
  What the line says — one state object (``_bar``); ``_compose()`` derives
      the line from it, so nothing stores the rendered text twice.
  How the terminal is handed away — ``yield_terminal(kind)``, nothing else.
      Interactive input goes through ``primitives/prompt.py``, which yields
      for you; never call ``clear()`` before ``input()``.
  When the bar repaints — ``_draw()`` is the only writer, one write per
      frame. Events (tick, phase, yield exit) draw immediately; a ticker
      thread redraws ~1/s so the clock keeps moving, except while
      ``pty_runner`` forwards a raw byte stream, when it owns repaint
      (``forwarding_output()`` / ``refresh()``).
  Lifecycle — ``cli.main`` wraps the run in ``session()``; atexit is only
      the backstop for other importers.

Public API:
    init()                  once at CLI entry, after verbosity/colour/dry-run
    session()               context manager; releases the terminal on exit
    shutdown()              release the terminal; idempotent; atexit-registered
    phase(label)            uncounted status, a single slot; phase(None) clears
    tracker(total, prefix, expected=None)
                            context manager yielding a tick(label) callable.
                            While open its line wins over the phase and carries
                            ' · <elapsed>' (past _ELAPSED_FLOOR_S) and, after
                            two completions (or at once given per-item
                            ``expected`` seconds), ' · ~<eta> left'. The callable
                            also carries:
                              tick.note(text)  show '[i/n] <text>' instead of
                                               the label (no increment)
                              tick.resume()    drop the note
                            One tracker at a time: a tracker opened inside
                            another is a no-op (RuntimeError under tests).
    steps(prefix, names, expected=None, on_done=None)
                            context manager: a stage's own steps (3.5.0-F6). While
                            open the line reads '<prefix> i/n · <step> · <item>'
                            and its time pair is the stage's elapsed and ETA.
    step(name)              advance the open steps to *name* (planned steps it
                            passes are dropped; an unplanned one is inserted)
    plan_steps(names)       replace the steps planned after the current one
    heartbeat(detail)       live detail appended to the line; does not paint.
    build_started()         forget the previous build's ninja totals.
                            A ninja '[x/y] …' status shows as 'ninja 74%'
    yield_terminal(kind)    hand the terminal to a "prompt" (blank the row,
                            keep the region) or a "child" (release it); the
                            tracker clock pauses for the duration
    clear()                 teardown: release the region, keep the state
    reserved_rows()         0/1 — rows a pty child must not use
    refresh() / forwarding_output() / require_no_tracker(owner) / paused_seconds()
                            see ``primitives/progress_hooks.py``
"""
import atexit
import contextlib
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Generator
from typing import Callable, Optional, Protocol

from sysforge import log
from sysforge.primitives import progress_hooks as _progress_hooks

_log = log.get_logger("PROGRESS")


class Tick(Protocol):
    """Callable yielded by :func:`tracker`.

    Calling it (``tick(label)``) advances the counter and repaints. ``note``
    and ``resume`` overlay a transient sub-step onto the same ``[i/total]``
    counter without advancing it.
    """

    def __call__(self, label: str) -> None: ...
    def note(self, text: str) -> None: ...
    def resume(self) -> None: ...

_ESC = "\x1b"
_SAVE = _ESC + "7"
_RESTORE = _ESC + "8"
_INDEX = _ESC + "D"
_RESET_REGION = _ESC + "[r"
_CLEAR_LINE = _ESC + "[2K"
_ERASE_BELOW = _ESC + "[J"

_TTY_PREFIX = "[SYSFORGE][PROGRESS] "
_PLAIN_PREFIX = "[PROGRESS] "

# yield_terminal kinds, by strength: a child yield (region released) subsumes
# a prompt yield (row blanked, region kept).
_YIELD_STRENGTH = {"prompt": 1, "child": 2}


# ---------------------------------------------------------------------------
# State (3.3.0-F2)
# ---------------------------------------------------------------------------
# Everything the bar says, and everything known about the terminal, lives
# here. The rendered line is derived by _compose() at paint time and never
# stored except as `drawn` — the exact text last written, kept only so an
# unchanged frame writes nothing.

@dataclass
class _Tracker:
    total: int
    prefix: str
    started: float                     # active-clock time at entry
    index: int = 0                     # items ticked; item `index` is in flight
    label: str = ""                    # in-flight item's label
    note: Optional[str] = None         # tick.note() overlay; None → show label
    last_tick: float = 0.0             # active-clock time the in-flight item started
    expected: Optional[list[Optional[int]]] = None  # per-item seconds, tick order (3.3.0-F1)
    paused_total: float = 0.0          # wall seconds spent paused since entry
    paused_since: Optional[float] = None  # set while paused


@dataclass
class _Steps:
    """A stage's own steps (3.5.0-F6), outside and above the tracker."""
    prefix: str
    names: list[str]
    started: float                     # bar-active-clock time at entry
    index: int = -1                    # current step; -1 before the first
    step_started: float = 0.0          # bar-active-clock time the step began
    expected: dict[str, int] = field(default_factory=dict)  # step → seconds
    on_done: Optional[Callable[[str, float], None]] = None


@dataclass
class _Bar:
    mode: Optional[str] = None         # "tty" | "plain" | None (uninitialised)
    phase: Optional[str] = None        # single slot: phase(x) replaces, phase(None) clears
    tracker: Optional[_Tracker] = None  # at most one
    steps: Optional[_Steps] = None     # at most one; outermost layer of the line
    ninja: dict[int, int] = field(default_factory=dict)  # ninja total → done, this item
    detail: Optional[str] = None       # heartbeat detail; cleared by tick/note/resume/phase
    yielded: Optional[str] = None      # None | "prompt" | "child"
    yield_depth: int = 0
    pause_depth: int = 0               # yields + bare pauses; the clock stops while > 0
    paused_total: float = 0.0          # all time spent paused, tracker or not (3.3.0-F1)
    paused_since: Optional[float] = None
    forwarding: int = 0                # pty_runner forwarding a raw stream; ticker stands down
    reserved: bool = False
    resize_pending: bool = False
    rows: int = 0
    cols: int = 0
    drawn: Optional[str] = None        # exact text last written to the bar row


_bar = _Bar()

# Guards mutate-then-draw in every public entry point and the ticker. Never
# held across a blocking call or a yield_terminal body; never taken by the
# signal handler. Re-entrant so a helper that calls back in cannot deadlock.
_lock = threading.RLock()

# One tracker at a time. Production logs and ignores a nested tracker; the
# test suite flips this on (tests/conftest.py) so nesting fails loudly.
_strict_nesting = False

_sigwinch_installed: bool = False
_atexit_installed: bool = False

# Opt-in region-transition trace for diagnosing a vanishing bar (3.2.0-B11).
# Set SYSFORGE_PROGRESS_TRACE=<path> to append a timestamped record of every
# establish / release / paint / resize. The two explanations for a bar that is
# not on screen look identical to the user — the scroll region was released, or
# it simply stopped being repainted — and the pty raw dump (3.2.0-B9) already
# ruled out the third, a child scrolling over the row. Nothing here writes to
# the terminal, and every failure is swallowed: a diagnostic must never be able
# to break the thing it observes.
_TRACE_ENV = "SYSFORGE_PROGRESS_TRACE"


def _trace(event: str) -> None:
    path = os.environ.get(_TRACE_ENV)
    if not path:
        return
    try:
        with open(path, "a") as fh:  # noqa: PTH123 — best-effort diagnostic
            fh.write(f"{time.time():.3f} {event}\n")
    except Exception:  # noqa: S110 — a trace must never break the run
        pass

# ---------------------------------------------------------------------------
# Elapsed / ETA suffix (3.2.0-F14), on a pausable clock (3.3.0-F2)
# ---------------------------------------------------------------------------
# The counter alone gives position but not pace: at default verbosity a healthy
# 40-second source sync and a stalled one look identical. The estimate is built
# from *measured* per-item durations in this very run rather than from the
# configured per-operation timeouts — a timeout is a worst-case ceiling, so
# multiplying one by the item count advertises an hour for a run that takes
# forty seconds. Items already completed here were fetched on this machine,
# over this link, against this work, which is the best predictor available.
#
# Time spent with the terminal yielded (a prompt waiting on the user) is not
# work, so the tracker measures an *active* clock that stops while paused —
# otherwise one slow answer would inflate the rate for the rest of the batch.
#
# Indirection so tests can drive a deterministic clock.
_monotonic = time.monotonic

# Below this, there is nothing worth saying — and saying nothing keeps short
# batches rendering byte-identically to the pre-F14 line.
_ELAPSED_FLOOR_S = 5


def _fmt_span(seconds: float) -> str:
    """Compact duration for a live bar: 42s / 7m03s / 2h05m.

    Finer-grained than ``build_estimate._fmt_hms``'s minute resolution on
    purpose — that one summarises a batch before it starts, this one is watched
    while it moves, and a clock that only changes once a minute reads as stuck.
    """
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _active(t: _Tracker, now: float) -> float:
    """*now* on the tracker's active clock: wall time minus time paused."""
    paused = t.paused_total
    if t.paused_since is not None:
        paused += now - t.paused_since
    return now - paused


def _eta(t: _Tracker, active: float) -> Optional[float]:
    """Seconds left on *t*, or ``None`` when there is no honest estimate.

    Without per-item ``expected`` durations: the in-run mean of completed items
    times what remains (needs two completions — one is an anecdote). With
    them (3.3.0-F1): the sum of what is still to run, each item at its own
    expected duration — the in-flight one at ``max(0, expected - elapsed)`` —
    so a 20-minute package finishing does not swing the figure for the
    30-second ones behind it. An item with no history uses the in-run mean,
    and until there is one the estimate is withheld rather than guessed.
    """
    # Item `index` is still in flight, so only 1..index-1 have measured durations.
    completed = t.index - 1
    mean = (t.last_tick - t.started) / completed if completed >= 2 else None
    in_flight_for = active - t.last_tick
    if t.expected is None or all(e is None for e in t.expected):
        if mean is None or t.total <= completed:
            return None
        # Remaining work includes the in-flight item; subtract what it has
        # already burned so the figure decays between ticks.
        return mean * (t.total - completed) - in_flight_for
    current = max(t.index - 1, 0)          # 0-based in-flight item
    remaining = 0.0
    for i in range(current, t.total):
        exp = t.expected[i] if t.expected[i] is not None else mean
        if exp is None:
            return None
        if i == current and t.index >= 1:
            exp = max(0.0, exp - in_flight_for)
        remaining += exp
    return remaining


def _time_suffix(t: _Tracker, now: float) -> str:
    """' · <elapsed>[ · ~<eta> left]' for an open tracker, or ''."""
    active = _active(t, now)
    elapsed = active - t.started
    if elapsed < _ELAPSED_FLOOR_S:
        return ""
    out = f" · {_fmt_span(elapsed)}"
    eta = _eta(t, active)
    if eta is not None and eta > 0:
        out += f" · ~{_fmt_span(eta)} left"
    # eta <= 0 means the estimate is overrun, not that we are done: drop it
    # rather than pin '~0s left' to the bar for the rest of a long item.
    return out


def _bar_active(bar: _Bar, now: float) -> float:
    """*now* on the bar-wide active clock: wall time minus all time paused."""
    paused = bar.paused_total
    if bar.paused_since is not None:
        paused += now - bar.paused_since
    return now - paused


def _steps_eta(s: _Steps, active: float) -> Optional[float]:
    """Seconds left in the stage: the rest of the current step at its expected
    length, plus each later step's. ``None`` when any of them has no history:
    a partial sum would read as an estimate of the whole and understate it."""
    remaining = 0.0
    for i in range(max(s.index, 0), len(s.names)):
        exp = s.expected.get(s.names[i])
        if exp is None:
            return None
        if i == s.index:
            exp = max(0.0, exp - (active - s.step_started))
        remaining += exp
    return remaining


def _steps_suffix(s: _Steps, now: float) -> str:
    """' · <stage elapsed>[ · ~<stage eta> left]', or '' below the floor."""
    active = _bar_active(_bar, now)
    elapsed = active - s.started
    if elapsed < _ELAPSED_FLOOR_S:
        return ""
    out = f" · {_fmt_span(elapsed)}"
    eta = _steps_eta(s, active)
    if eta is not None and eta > 0:
        out += f" · ~{_fmt_span(eta)} left"
    return out


def _compose_steps(bar: _Bar, s: _Steps, now: float) -> str:
    """'<prefix> i/n · <step>[ · <item>][ · <detail>]<time>' (3.5.0-F6).

    The tracker's own prefix and clock give way to the step and the stage's
    clock; its counter shows only when it counts more than one item, since a
    pass of one PKGBUILD reading '[1/1]' for an hour says nothing.
    """
    parts = [f"{s.prefix} {s.index + 1}/{len(s.names)} · {s.names[s.index]}"]
    t = bar.tracker
    if t is not None and t.total > 0:
        item = t.note if t.note is not None else (t.label if t.index else None)
        if item:
            parts.append(f"[{t.index}/{t.total}] {item}" if t.total > 1 else item)
    elif bar.phase is not None and bar.phase != s.prefix:
        parts.append(bar.phase)
    if bar.detail and bar.mode != "plain":  # plain mode never shows the heartbeat
        parts.append(bar.detail)
    try:
        suffix = _steps_suffix(s, now)
    except Exception:  # a decoration must never break the bar
        suffix = ""
    return " · ".join(parts) + suffix


def _compose(bar: _Bar, now: float) -> Optional[str]:
    """The only line constructor: what the bar says right now, or None."""
    t = bar.tracker
    s = bar.steps
    if s is not None and 0 <= s.index < len(s.names):
        text = _compose_steps(bar, s, now)
        prefix = _PLAIN_PREFIX if bar.mode == "plain" else _TTY_PREFIX
        return prefix + text
    if t is not None and t.total > 0:
        if t.note is not None:
            body = t.note
        else:
            body = f"{t.prefix} · {t.label if t.index else 'starting...'}"
        try:
            suffix = _time_suffix(t, now)
        except Exception:  # a decoration must never break the bar
            suffix = ""
        text = f"[{t.index}/{t.total}] {body}{suffix}"
    elif bar.phase is not None:
        text = bar.phase
    else:
        return None
    if bar.detail:
        text = f"{text} · {bar.detail}"
    prefix = _PLAIN_PREFIX if bar.mode == "plain" else _TTY_PREFIX
    return prefix + text


# ---------------------------------------------------------------------------
# Terminal primitives — each returns the bytes for its part of a frame
# ---------------------------------------------------------------------------

def _detect_mode() -> str:
    # 3.1.0-B7: dry-run is deliberately NOT a rung here. The other half of
    # set_dry_run_mode() redirects log output to stdout, and pairing that with
    # plain progress looks like it avoids ANSI cursor control interleaving with
    # the report — but progress writes to *stderr* (the TTY renderer and the
    # isatty check below both target it), so the two are on separate descriptors
    # and cannot interleave. Forcing plain mode instead turned a dry run over a
    # large source sync into hundreds of discrete lines; the remaining rungs
    # already cover every case where plain output is genuinely required,
    # including the piped-to-a-file shape a scripted dry run actually takes.
    if not sys.stderr.isatty():
        return "plain"
    if os.environ.get("TERM", "") in ("", "dumb"):
        return "plain"
    if os.environ.get("CI"):
        return "plain"
    # Defer the colour decision to the single authority: NO_COLOR, FORCE_COLOR
    # and --color=never/auto all resolve there. Ask it about *stderr* explicitly
    # rather than about log's active stream: under --dry-run that stream is
    # stdout, and a redirected stdout would otherwise drag progress back into
    # plain mode through the colour rung — the same dry-run coupling B7 removed
    # above, arriving one rung later.
    if not log.use_color(sys.stderr):
        return "plain"
    return "tty"


def _write(seq: str) -> None:
    try:
        sys.stderr.write(seq)
        sys.stderr.flush()
    except Exception:  # noqa: S110 — best-effort terminal write, failure is non-fatal
        pass


def _refresh_size() -> None:
    sz = shutil.get_terminal_size(fallback=(80, 24))
    _bar.cols, _bar.rows = sz.columns, sz.lines


def _establish_seq() -> str:
    if _bar.reserved:
        return ""
    _refresh_size()
    rows = _bar.rows
    if rows < 3:
        _trace(f"establish-skipped rows={rows} (terminal too short)")
        return ""
    # Reserve the bar's row (N) and guarantee the cursor ends up INSIDE the
    # scroll region regardless of where the shell prompt started. A DECSTBM
    # region only scrolls for a cursor within [1, N-1]: if the prompt began at
    # the bottom of the screen, the post-Enter cursor sits at row N (below the
    # region) and newlines would pile every line onto the bottom row instead of
    # scrolling. So:
    #   1. save the content cursor,
    #   2. jump to the absolute bottom row and emit one index (ESC D): at the
    #      bottom this scrolls the whole screen up by one, freeing row N for the
    #      bar (works whether or not the screen was full),
    #   3. set the DECSTBM region [1, N-1],
    #   4. restore the saved (absolute) cursor, then move up one line to undo
    #      the scroll — landing the cursor back on its content, now inside the
    #      region.
    # This also avoids the fresh-shell blank-line gap the old cursor-park caused.
    _bar.reserved = True
    _trace(f"establish rows={rows} cols={_bar.cols} region=[1,{rows - 1}]")
    return (f"{_SAVE}{_ESC}[{rows};1H{_INDEX}{_ESC}[1;{rows - 1}r"
            f"{_RESTORE}{_ESC}[1A")


def _release_seq() -> str:
    _bar.drawn = None
    if not _bar.reserved:
        return ""
    # Resetting the region also homes the cursor, and clearing the bar drives it
    # to the absolute bottom row — bracket both with save/restore so the shell
    # resumes from the real content row, not the bottom of an otherwise-empty
    # screen (the stranded-blank-lines bug).
    _bar.reserved = False
    if _bar.resize_pending:
        # After a resize the bar is no longer reliably on row `rows`: a taller
        # window pulls scrollback in and shifts everything, cursor included,
        # down by the rows gained. Clearing the old row then erases a content
        # line and leaves the stale bar for establish's index to scroll into
        # the output. What a resize preserves is order: the bar is still below
        # the content cursor. Erase from the cursor down (normally only the
        # cursor line's empty tail plus the bar).
        _trace(f"release-resized rows={_bar.rows}")
        return f"{_SAVE}{_RESET_REGION}{_RESTORE}{_ERASE_BELOW}"
    _trace(f"release rows={_bar.rows}")
    return f"{_SAVE}{_RESET_REGION}{_ESC}[{_bar.rows};1H{_CLEAR_LINE}{_RESTORE}"


def _blank_seq() -> str:
    """Blank the bar row in place, keeping the region (a prompt yield)."""
    _bar.drawn = None
    if not _bar.reserved:
        return ""
    return f"{_SAVE}{_ESC}[{_bar.rows};1H{_CLEAR_LINE}{_RESTORE}"


def _paint_seq(text: str) -> str:
    if _bar.cols <= 0:
        _refresh_size()
    # Downgrade decorative glyphs (·, block-bar fills) before width-truncating —
    # ASCII fallbacks change length, so this must precede the column clamp.
    truncated = log.downgrade_glyphs(text)[: max(0, _bar.cols - 1)]
    _trace(f"paint row={_bar.rows} text={truncated!r}")
    return f"{_SAVE}{_ESC}[{_bar.rows};1H{_CLEAR_LINE}{truncated}{_RESTORE}"


def _draw() -> None:
    """Sole bar writer (TTY). Caller holds ``_lock``.

    Builds the whole frame — any resize, region establishment or release and
    the painted row — as one string and emits it with a single write, so a
    log line from another thread can land before or after a frame but never
    inside one.
    """
    bar = _bar
    if bar.mode != "tty" or bar.yielded is not None:
        return
    frame = ""
    if bar.resize_pending:
        _trace("resize")
        frame += _release_seq()          # reads resize_pending; clear it after
        bar.resize_pending = False
        _refresh_size()
    text = _compose(bar, _monotonic())
    if text is None:
        frame += _release_seq()
        if frame:
            _write(frame)
        return
    if text == bar.drawn:
        return
    frame += _establish_seq()
    if bar.reserved:
        frame += _paint_seq(text)
        bar.drawn = text
        _ensure_ticker()
    if frame:
        _write(frame)


def _emit_plain() -> None:
    """Plain-mode counterpart of _draw(): one log line per change of text."""
    text = _compose(_bar, _monotonic())
    if text is None:
        _bar.drawn = None
        return
    if text != _bar.drawn:
        log.ui("[PROGRESS]", text)
        _bar.drawn = text


def _changed() -> None:
    """Show a state change made by an event (tick, phase). Caller holds ``_lock``."""
    if _bar.mode == "plain":
        _emit_plain()
    else:
        _draw()


def _ensure_init() -> None:
    if _bar.mode is None:
        init()


def _on_sigwinch(*_args) -> None:
    # Flag only: no I/O and no lock from a signal handler, which may have
    # interrupted a paint holding both. The scheduled owner applies it.
    _bar.resize_pending = True


# ---------------------------------------------------------------------------
# Scheduled repaint (3.3.0-F2)
# ---------------------------------------------------------------------------
# Exactly one painter on a schedule at a time. By default that is this ticker
# thread; while pty_runner forwards a raw byte stream (which can be mid-escape
# or mid-UTF-8 at any moment) it stands down and pty_runner calls refresh() at
# stream boundaries instead. Frame dedup means it writes nothing until the
# composed text changes — in practice once a second while a tracker's clock is
# showing.

_ticker_enabled = True        # tests/conftest.py turns this off
_TICK_S = 1.0
_ticker_thread: Optional[threading.Thread] = None
_ticker_stop = threading.Event()


def _ticker_step() -> None:
    with _lock:
        if _bar.mode != "tty" or _bar.yielded is not None or _bar.forwarding:
            return
        _draw()


def _ticker_loop(stop: threading.Event) -> None:
    while not stop.wait(_TICK_S):
        try:
            _ticker_step()
        except Exception as e:  # noqa: BLE001 — a repaint must never kill the run
            _trace(f"ticker-error {e!r}")


def _ensure_ticker() -> None:
    global _ticker_thread, _ticker_stop
    if not _ticker_enabled:
        return
    if _ticker_thread is not None and _ticker_thread.is_alive():
        return
    _ticker_stop = threading.Event()
    _ticker_thread = threading.Thread(
        target=_ticker_loop, args=(_ticker_stop,),
        name="sysforge-progress", daemon=True,
    )
    _ticker_thread.start()


def _stop_ticker() -> None:
    """Stop and join the ticker. Must not be called holding ``_lock``."""
    global _ticker_thread
    thread = _ticker_thread
    if thread is None:
        return
    _ticker_stop.set()
    if thread is not threading.current_thread():
        thread.join(timeout=1.0)
    _ticker_thread = None


def refresh() -> None:
    """Repaint now if the composed text changed (the forwarding owner's call)."""
    with _lock:
        if _bar.mode == "tty":
            _draw()


@contextlib.contextmanager
def forwarding_output() -> Generator[None, None, None]:
    """Mark a raw byte stream as being forwarded; the ticker stands down."""
    with _lock:
        _bar.forwarding += 1
    try:
        yield
    finally:
        with _lock:
            _bar.forwarding -= 1


# ---------------------------------------------------------------------------
# Pause and terminal handover
# ---------------------------------------------------------------------------

def _pause() -> None:
    """Stop the tracker clock. Depth-counted; independent of yielding."""
    with _lock:
        _bar.pause_depth += 1
        t = _bar.tracker
        if _bar.pause_depth == 1:
            now = _monotonic()
            _bar.paused_since = now
            if t is not None:
                t.paused_since = now


def _unpause() -> None:
    with _lock:
        if _bar.pause_depth == 0:
            return
        _bar.pause_depth -= 1
        t = _bar.tracker
        if _bar.pause_depth == 0:
            now = _monotonic()
            if _bar.paused_since is not None:
                _bar.paused_total += now - _bar.paused_since
                _bar.paused_since = None
            if t is not None and t.paused_since is not None:
                t.paused_total += now - t.paused_since
                t.paused_since = None


def paused_seconds() -> float:
    """Total seconds spent paused so far, including any pause in progress.

    Monotonic, so a caller timing work takes the delta across its own span
    and subtracts it — ``makepkg_wrapper`` does, so a build that sat at a
    prompt does not record the wait as build time (3.3.0-F1).
    """
    with _lock:
        total = _bar.paused_total
        if _bar.paused_since is not None:
            total += _monotonic() - _bar.paused_since
        return total


@contextlib.contextmanager
def yield_terminal(kind: str = "prompt") -> Generator[None, None, None]:
    """Hand the terminal to someone else for the body, then take it back.

    ``"prompt"`` blanks the bar row in place and keeps the scroll region, so
    the prompt prints in the normal content flow — a full release resets the
    region (``ESC[r``), and the cursor-restore across that reset is unreliable
    on some terminals; it left prompts rendering off-view. ``"child"`` releases
    the region entirely, for a TTY-inheriting child that addresses the whole
    screen (a pager, a full-screen editor), which the region would otherwise
    clamp into the reserved band.

    While yielded, state keeps updating but nothing draws, and an open
    tracker's clock is paused. Yields nest; the strongest kind applies, and
    the outermost exit restores the region if it was released and repaints
    immediately. No-op in plain mode.
    """
    if kind not in _YIELD_STRENGTH:
        raise ValueError(f"unknown yield kind {kind!r}")
    with _lock:
        _ensure_init()
        active = _bar.mode == "tty"
        if active:
            _bar.yield_depth += 1
            _pause()
            prev = _bar.yielded
            if prev is None or _YIELD_STRENGTH[kind] > _YIELD_STRENGTH[prev]:
                _bar.yielded = kind
                frame = _release_seq() if kind == "child" else _blank_seq()
                if frame:
                    _write(frame)
    if not active:
        yield
        return
    try:
        yield
    finally:
        with _lock:
            _bar.yield_depth -= 1
            _unpause()
            if _bar.yield_depth == 0:
                _bar.yielded = None
                _draw()


def reserved_rows() -> int:
    """Rows the progress bar has reserved at the bottom of the terminal (0 or 1).

    A TTY-inheriting pty child must be sized to ``terminal_height -
    reserved_rows()`` so its full-screen redraws/scrolling stay inside the
    DECSTBM region (``[1, N-1]``) and never touch the bar row. This keeps the
    bar permanently visible *during* a subprocess build instead of having it
    collapse output onto the reserved row. See ``pty_runner.run_with_pty``'s
    ``reserve_bottom_rows`` argument. 1 while prompt-yielded (the region is
    kept), 0 while child-yielded (it is released).
    """
    return 1 if (_bar.mode == "tty" and _bar.reserved) else 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init() -> None:
    """Detect mode and install lifecycle hooks. Safe to call repeatedly."""
    global _sigwinch_installed, _atexit_installed
    with _lock:
        _bar.mode = _detect_mode()
    if not _atexit_installed:
        # The backstop for importers that don't go through cli.main's session().
        atexit.register(shutdown)
        _atexit_installed = True
    if _bar.mode == "tty" and not _sigwinch_installed:
        try:
            signal.signal(signal.SIGWINCH, _on_sigwinch)
            _sigwinch_installed = True
        except (OSError, ValueError):
            pass


def shutdown() -> None:
    """Stop the ticker and restore the terminal. Idempotent. Registered atexit."""
    _stop_ticker()
    with _lock:
        if _bar.mode == "tty":
            frame = _release_seq()
            if frame:
                _write(frame)


@contextlib.contextmanager
def session() -> Generator[None, None, None]:
    """The run's lifetime: the terminal is released however the body exits."""
    try:
        yield
    finally:
        shutdown()


def clear() -> None:
    """Release the reserved region; the bar's state is untouched (teardown only)."""
    _stop_ticker()
    with _lock:
        if _bar.mode == "tty":
            frame = _release_seq()
            if frame:
                _write(frame)
        _bar.drawn = None


# ---------------------------------------------------------------------------
# What the bar says
# ---------------------------------------------------------------------------

def phase(label: Optional[str]) -> None:
    """Set the uncounted phase status; ``phase(None)`` clears it.

    A single slot, not a stack: ``run_verb`` paints ``<verb>: starting…`` and
    ``update`` deliberately overrides it, and a stack would leave stale labels
    beneath. While a tracker is open its line wins; the phase shows again once
    it exits, so the bottom line stays populated between counted batches. In
    plain mode a repeated phase is not logged twice.
    """
    with _lock:
        _ensure_init()
        _bar.phase = label
        _bar.detail = None
        _bar.ninja.clear()  # a new phase's builds count afresh
        _changed()


def heartbeat(detail: str) -> None:
    """Attach live *detail* to the current line, advancing nothing.

    A whole package build sits inside a single ``tick()``; ``pty_runner``'s
    idle callback knows the child is alive and what it is compiling every
    ``MAKEPKG_HEARTBEAT_S`` (3.2.0-B13). The detail replaces rather than
    appends, and the next tick/note/resume/phase clears it. This only sets
    state — the scheduled repaint (the ticker, or ``pty_runner`` while it
    forwards output) puts it on screen. Never shown in plain mode, where a
    30-second line would append to the log forever.
    """
    with _lock:
        _ensure_init()
        _bar.detail = _heartbeat_text(detail) if detail else None


def build_started() -> None:
    """Forget the ninja totals a previous build left (3.5.0-B91).

    Totals are otherwise cleared on tick, tracker entry/exit and step or phase
    changes; a second build inside one step with none of those (the
    ``--bolt-only`` repackage) was measured against the largest earlier total.
    ``makepkg_invoke`` calls this as each makepkg run starts.
    """
    with _lock:
        _ensure_init()
        _bar.ninja.clear()


# ninja's status line: '[7412/10023] Building CXX object lib/…/Foo.cpp.o'.
_NINJA_RE = re.compile(r"^\[(\d+)/(\d+)\]\s*(.*)$")
# A sub-build's name, from the first path segment of what it is building.
_SUBBUILD_RE = re.compile(r"(?:^|\s)([a-z][a-z0-9_+-]{1,15})/")


def _heartbeat_text(detail: str) -> str:
    """The bar's text for a heartbeat *detail* (3.5.0-F6).

    A ninja status becomes a percentage: the path it carried is cut by the
    terminal width anyway, and only its position said anything. One package
    build can run several ninja builds (llvm's runtimes, libclc), each counting
    from 1 again, so the largest total seen this item is taken as the main
    build and any other is named beside it — '[1895/1900]' for the runtimes no
    longer reads as nearly done. Anything else passes through unchanged.
    Caller holds ``_lock``.
    """
    m = _NINJA_RE.match(detail.strip())
    if m is None:
        return detail
    done, total, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    if total <= 0:
        return detail
    _bar.ninja[total] = done
    main = max(_bar.ninja)
    text = f"ninja {_bar.ninja[main] * 100 // main}%"
    if total != main:
        name = _SUBBUILD_RE.search(rest)
        text += f" · {name.group(1) if name else 'sub-build'} {done * 100 // total}%"
    return text


def require_no_tracker(owner: str) -> None:
    """Refuse to start *owner* (which opens its own tracker) inside a tracker.

    Called first thing by the functions that open trackers, so a mis-nested
    caller fails before any sync/resolution side effects rather than when the
    inner tracker opens. Raises under tests; logs at debug in production.
    """
    with _lock:
        t = _bar.tracker
    if t is None:
        return
    msg = f"{owner} called inside tracker '{t.prefix}'"
    if _strict_nesting:
        raise RuntimeError(msg)
    _log.debug(msg)


class _TrackerTick:
    """The ``tick`` a tracker yields; a no-op once its tracker has closed."""

    def __init__(self, t: _Tracker) -> None:
        self._t = t

    def __call__(self, label: str) -> None:
        with _lock:
            t = self._t
            if _bar.tracker is not t:
                return
            t.index += 1
            t.label = label
            t.note = None
            t.last_tick = _active(t, _monotonic())
            _bar.detail = None
            _bar.ninja.clear()  # a new item's builds count afresh
            _changed()

    def note(self, text: str) -> None:
        with _lock:
            if _bar.tracker is not self._t:
                return
            self._t.note = text
            _bar.detail = None
            _changed()

    def resume(self) -> None:
        with _lock:
            if _bar.tracker is not self._t:
                return
            self._t.note = None
            _bar.detail = None
            _changed()


@contextlib.contextmanager
def tracker(total: int, prefix: str,
            expected: Optional[list[Optional[int]]] = None) -> Generator[Tick, None, None]:
    """Yield a tick(label) callable counting *total* items.

        with progress.tracker(len(items), "building") as tick:
            for item in items:
                tick(item.name)
                do_work(item)

    Paints a 0/total placeholder on entry so users see immediate feedback
    even when the first tick is far away (e.g. a batch of slow source syncs).

    One tracker at a time. For a sub-step inside an item use the yielded
    ``tick.note(text)`` / ``tick.resume()`` — e.g. a just-in-time dep install
    between two counted builds — never a nested tracker. A tracker opened
    inside another is a no-op here (debug-logged) and a ``RuntimeError``
    under tests.
    """
    t: Optional[_Tracker] = None
    with _lock:
        _ensure_init()
        outer = _bar.tracker
        if outer is None:
            now = _monotonic()
            t = _Tracker(total=total, prefix=prefix, started=now, last_tick=now,
                         expected=(list(expected)
                                   if expected is not None and len(expected) == total
                                   else None))
            if _bar.pause_depth:
                t.paused_since = now   # opened while yielded: starts paused
            _bar.tracker = t
            _bar.detail = None
            _bar.ninja.clear()
            if total > 0:
                _changed()
    if outer is not None:
        msg = f"tracker '{prefix}' opened inside '{outer.prefix}'"
        if _strict_nesting:
            raise RuntimeError(msg)
        _log.debug(msg)
        yield _progress_hooks._NoOpTick()
        return
    assert t is not None  # noqa: S101 — set above whenever outer is None
    try:
        yield _TrackerTick(t)
    finally:
        with _lock:
            if _bar.tracker is t:
                _bar.tracker = None
                _bar.detail = None
                _bar.ninja.clear()
                if _bar.mode == "plain":
                    # Whatever shows now (the phase) was already logged.
                    _bar.drawn = _compose(_bar, _monotonic())
                else:
                    _draw()


# ---------------------------------------------------------------------------
# A stage's own steps (3.5.0-F6)
# ---------------------------------------------------------------------------
# A long stage is a fixed sequence of steps (the toolchain's preflight,
# instrument, train, optimize, audit, install, …), each holding whatever
# trackers and phases it opens. Counting PKGBUILD dirs instead said '[1/1]' for
# an hour. The steps sit outside the tracker, so the one-tracker rule is
# untouched, and they carry the stage's clock: elapsed since the stage began,
# and an ETA summed from each remaining step's recorded duration.

def _finish_step(s: _Steps, now: float) -> Optional[Callable[[], None]]:
    """The call reporting the current step's active duration to ``on_done``,
    or ``None``. Caller holds ``_lock`` and runs the result after releasing
    it: ``on_done`` writes a file, and ``_lock`` is never held across a
    blocking call."""
    if s.on_done is None or not 0 <= s.index < len(s.names):
        return None
    on_done, name = s.on_done, s.names[s.index]
    secs = _bar_active(_bar, now) - s.step_started

    def report() -> None:
        try:
            on_done(name, secs)
        except Exception as e:  # noqa: BLE001 — timing must never break the run
            _log.debug(f"step timing not recorded: {e!r}")
    return report


@contextlib.contextmanager
def steps(prefix: str, names: list[str], *,
          expected: Optional[dict[str, int]] = None,
          on_done: Optional[Callable[[str, float], None]] = None) -> Generator[None, None, None]:
    """Count a stage's own *names* in order; advance with :func:`step`.

    ``expected`` maps a step name to its usual length in seconds (the stage's
    recorded history); ``on_done(name, seconds)`` hears each step that
    completes, with time paused at a prompt left out. A step completes when the
    next one begins, or when the body returns; a step the body raises out of
    is not reported, since a failure's duration predicts nothing. Opening
    steps inside open steps is a no-op, like a nested tracker.
    """
    with _lock:
        _ensure_init()
        outer = _bar.steps
        s: Optional[_Steps] = None
        saved_phase = _bar.phase
        if outer is None:
            now = _bar_active(_bar, _monotonic())
            s = _Steps(prefix=prefix, names=list(names), started=now,
                       step_started=now, expected=dict(expected or {}),
                       on_done=on_done)
            _bar.steps = s
    if outer is not None:
        msg = f"steps '{prefix}' opened inside '{outer.prefix}'"
        if _strict_nesting:
            raise RuntimeError(msg)
        _log.debug(msg)
        yield
        return
    assert s is not None  # noqa: S101 — set above whenever outer is None
    ok = False
    report: Optional[Callable[[], None]] = None
    try:
        yield
        ok = True
    finally:
        with _lock:
            if _bar.steps is s:
                if ok:
                    report = _finish_step(s, _monotonic())
                _bar.steps = None
                _bar.phase = saved_phase
                _bar.detail = None
                if _bar.mode == "plain":
                    _bar.drawn = _compose(_bar, _monotonic())
                else:
                    _draw()
        if report is not None:
            report()


def step(name: str) -> None:
    """Begin step *name* of the open steps; a no-op when none are open.

    The plan is a prediction, and the run corrects it: planned steps between
    the current one and *name* did not run, so they are dropped (the count
    stays honest instead of jumping), and a *name* not planned at all is
    inserted next. The phase is cleared, as a tracker's tick clears detail.
    """
    with _lock:
        s = _bar.steps
        if s is None:
            return
        now = _monotonic()
        if 0 <= s.index < len(s.names) and s.names[s.index] == name:
            return
        report = _finish_step(s, now)
        ahead = s.names[s.index + 1:]
        ahead = ahead[ahead.index(name):] if name in ahead else [name, *ahead]
        s.names = s.names[:s.index + 1] + ahead
        s.index += 1
        s.step_started = _bar_active(_bar, now)
        _bar.phase = None
        _bar.detail = None
        _bar.ninja.clear()
        _changed()
    if report is not None:
        report()


def plan_steps(names: list[str]) -> None:
    """Replace the steps planned after the current one with *names*.

    For a plan that firms up mid-stage (which passes run is known only once
    the PKGBUILDs resolve). A no-op when no steps are open.
    """
    with _lock:
        s = _bar.steps
        if s is None:
            return
        s.names = s.names[:s.index + 1] + [n for n in names
                                           if n not in s.names[:s.index + 1]]
        _changed()


# ---------------------------------------------------------------------------
# Registration with the leaf-layer protocol (3.2.0-F1b)
# ---------------------------------------------------------------------------
# Primitives ask ``primitives.progress_hooks.hooks()`` for this surface instead
# of importing upward into ui/. The module object satisfies the protocol as-is,
# so every call still resolves through this module's globals — monkeypatching a
# name here behaves exactly as it did before.
_progress_hooks.register(sys.modules[__name__])
