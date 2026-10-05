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
    tracker(total, prefix)  context manager yielding a tick(label) callable.
                            While open its line wins over the phase and carries
                            ' · <elapsed>' (past _ELAPSED_FLOOR_S) and, after
                            two completions, ' · ~<eta> left'. The callable
                            also carries:
                              tick.note(text)  show '[i/n] <text>' instead of
                                               the label (no increment)
                              tick.resume()    drop the note
                            One tracker at a time: a tracker opened inside
                            another is a no-op (RuntimeError under tests).
    heartbeat(detail)       live detail appended to the line; does not paint
    yield_terminal(kind)    hand the terminal to a "prompt" (blank the row,
                            keep the region) or a "child" (release it); the
                            tracker clock pauses for the duration
    clear()                 teardown: release the region, keep the state
    reserved_rows()         0/1 — rows a pty child must not use
    refresh() / forwarding_output() / require_no_tracker(owner)
                            see ``primitives/progress_hooks.py``
"""
import atexit
import contextlib
import os
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional, Protocol

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
    expected: Optional[list[int]] = None  # per-item seconds; reserved for 3.3.0-F1
    paused_total: float = 0.0          # wall seconds spent paused since entry
    paused_since: Optional[float] = None  # set while paused


@dataclass
class _Bar:
    mode: Optional[str] = None         # "tty" | "plain" | None (uninitialised)
    phase: Optional[str] = None        # single slot: phase(x) replaces, phase(None) clears
    tracker: Optional[_Tracker] = None  # at most one
    detail: Optional[str] = None       # heartbeat detail; cleared by tick/note/resume/phase
    yielded: Optional[str] = None      # None | "prompt" | "child"
    yield_depth: int = 0
    pause_depth: int = 0               # yields + bare pauses; the clock stops while > 0
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


def _time_suffix(t: _Tracker, now: float) -> str:
    """' · <elapsed>[ · ~<eta> left]' for an open tracker, or ''."""
    active = _active(t, now)
    elapsed = active - t.started
    if elapsed < _ELAPSED_FLOOR_S:
        return ""
    out = f" · {_fmt_span(elapsed)}"
    # Item `index` is still in flight, so only 1..index-1 have measured durations.
    completed = t.index - 1
    if completed >= 2 and t.total > completed:
        rate = (t.last_tick - t.started) / completed
        # Remaining work includes the in-flight item; subtract what it has
        # already burned so the figure decays between ticks.
        eta = rate * (t.total - completed) - (active - t.last_tick)
        if eta > 0:
            out += f" · ~{_fmt_span(eta)} left"
    # eta <= 0 means the estimate is overrun, not that we are done: drop it
    # rather than pin '~0s left' to the bar for the rest of a long item.
    return out


def _compose(bar: _Bar, now: float) -> Optional[str]:
    """The only line constructor: what the bar says right now, or None."""
    t = bar.tracker
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
        bar.resize_pending = False
        _trace("resize")
        frame += _release_seq()
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
def forwarding_output() -> Iterator[None]:
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
        if _bar.pause_depth == 1 and t is not None:
            t.paused_since = _monotonic()


def _unpause() -> None:
    with _lock:
        if _bar.pause_depth == 0:
            return
        _bar.pause_depth -= 1
        t = _bar.tracker
        if _bar.pause_depth == 0 and t is not None and t.paused_since is not None:
            t.paused_total += _monotonic() - t.paused_since
            t.paused_since = None


@contextlib.contextmanager
def yield_terminal(kind: str = "prompt") -> Iterator[None]:
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
def session() -> Iterator[None]:
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
        _bar.detail = detail or None


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
def tracker(total: int, prefix: str) -> Iterator[Tick]:
    """Yield a tick(label) callable counting *total* items.

        with progress.tracker(len(items), "building") as tick:
            for item in items:
                tick(item.name)
                do_work(item)

    Paints a 0/total placeholder on entry so users see immediate feedback
    even when the first tick is far away (e.g. a batch of slow git pulls).

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
            t = _Tracker(total=total, prefix=prefix, started=now, last_tick=now)
            if _bar.pause_depth:
                t.paused_since = now   # opened while yielded: starts paused
            _bar.tracker = t
            _bar.detail = None
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
                if _bar.mode == "plain":
                    # Whatever shows now (the phase) was already logged.
                    _bar.drawn = _compose(_bar, _monotonic())
                else:
                    _draw()


# ---------------------------------------------------------------------------
# Registration with the leaf-layer protocol (3.2.0-F1b)
# ---------------------------------------------------------------------------
# Primitives ask ``primitives.progress_hooks.hooks()`` for this surface instead
# of importing upward into ui/. The module object satisfies the protocol as-is,
# so every call still resolves through this module's globals — monkeypatching a
# name here behaves exactly as it did before.
_progress_hooks.register(sys.modules[__name__])
