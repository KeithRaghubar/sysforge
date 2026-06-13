"""
ui/progress.py — bottom-anchored batch progress indicator.

Dual-mode renderer, picked once at init():
  TTY mode   — DECSTBM scroll region reserves the bottom row; other
               output (including subprocess output that inherits the
               TTY, e.g. makepkg / git / pacman) scrolls above it.
               Survives SIGWINCH and interactive input() via clear().
  Plain mode — non-TTY / dry-run / TERM=dumb / CI / NO_COLOR. Emits
               '[PROGRESS] [i/n] label' through log.ui() so the same
               data still reaches the user and the log file without
               ANSI garbage in pipes or journald.

Public API:
    init()                  once at CLI entry, after log.set_verbosity
    shutdown()              release the terminal; idempotent; atexit-registered
    render(i, n, label)     paint a status (reserves region on first call)
    phase(label)            paint an uncounted phase status that persists
                            across tracker() scopes; phase(None) clears it
    clear()                 release the region (call before input())
    tracker(total, prefix)  context manager yielding a tick(label) callable.
                            The callable also carries:
                              tick.note(text)  repaint at the current count
                                               with a one-off label (no
                                               increment) — for a transient
                                               sub-step overlay
                              tick.resume()    repaint the last tick state,
                                               undoing a prior note()

Reservation is lazy: entering tracker() alone touches nothing; the first
tick() establishes the region. Short-running invocations that never tick
leave the terminal untouched.

A phase set via phase() outlives any nested tracker(): when the tracker
exits it repaints the phase instead of releasing the region, so the bottom
line stays populated between counted batches. clear() still releases the
region unconditionally (the call-before-input() safety valve); the next
render()/phase() re-establishes it.
"""
import atexit
import contextlib
import os
import shutil
import signal
import sys
from typing import Iterator, Optional, Protocol

from sysforge import log


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
_RESET_REGION = _ESC + "[r"
_CLEAR_LINE = _ESC + "[2K"

_mode: Optional[str] = None
_reserved: bool = False
_last_status: Optional[str] = None
_phase: Optional[str] = None
_rows: int = 0
_cols: int = 0
_sigwinch_installed: bool = False
_atexit_installed: bool = False


def _detect_mode() -> str:
    if not sys.stderr.isatty():
        return "plain"
    if getattr(log, "_DRY_RUN", False):
        return "plain"
    if os.environ.get("TERM", "") in ("", "dumb"):
        return "plain"
    if os.environ.get("CI"):
        return "plain"
    # Defer the colour decision to the single authority: NO_COLOR, FORCE_COLOR
    # and --color=never/auto all resolve there. stderr is already known to be a
    # TTY here, so use_color()'s TTY rung is satisfied and this reduces to the
    # mode/env gate.
    if not log.use_color():
        return "plain"
    return "tty"


def _write(seq: str) -> None:
    try:
        sys.stderr.write(seq)
        sys.stderr.flush()
    except Exception:
        pass


def _refresh_size() -> None:
    global _rows, _cols
    sz = shutil.get_terminal_size(fallback=(80, 24))
    _cols, _rows = sz.columns, sz.lines


def _establish_region() -> None:
    global _reserved
    if _reserved:
        return
    _refresh_size()
    if _rows < 3:
        return
    _write(f"{_ESC}[1;{_rows - 1}r")
    _write(f"{_ESC}[{_rows - 1};1H")
    _reserved = True


def _release_region() -> None:
    global _reserved
    if not _reserved:
        return
    _write(_RESET_REGION)
    _write(f"{_ESC}[{_rows};1H{_CLEAR_LINE}")
    _reserved = False


def _paint(text: str) -> None:
    if _cols <= 0:
        _refresh_size()
    truncated = text[: max(0, _cols - 1)]
    _write(_SAVE)
    _write(f"{_ESC}[{_rows};1H{_CLEAR_LINE}")
    _write(truncated)
    _write(_RESTORE)


def _on_sigwinch(*_args) -> None:
    if _mode != "tty" or not _reserved:
        return
    _release_region()
    _establish_region()
    if _last_status is not None:
        _paint(_last_status)


def init() -> None:
    """Detect mode and install lifecycle hooks. Safe to call repeatedly."""
    global _mode, _sigwinch_installed, _atexit_installed
    _mode = _detect_mode()
    if not _atexit_installed:
        atexit.register(shutdown)
        _atexit_installed = True
    if _mode == "tty" and not _sigwinch_installed:
        try:
            signal.signal(signal.SIGWINCH, _on_sigwinch)
            _sigwinch_installed = True
        except (OSError, ValueError):
            pass


def shutdown() -> None:
    """Restore the terminal. Idempotent. Registered atexit."""
    if _mode == "tty":
        _release_region()


def render(current: int, total: int, label: str) -> None:
    """Paint a status line 'current/total label'."""
    global _last_status
    if _mode is None:
        init()
    if _mode == "plain":
        msg = f"[PROGRESS] [{current}/{total}] {label}"
        log.ui("[PROGRESS]", msg)
        _last_status = msg
        return
    text = f"[SYSFORGE][PROGRESS] [{current}/{total}] {label}"
    _last_status = text
    if not _reserved:
        _establish_region()
    if _reserved:
        _paint(text)


def phase(label: Optional[str]) -> None:
    """Paint an uncounted phase status; ``phase(None)`` clears it.

    The phase persists across nested tracker() scopes — tracker exit
    repaints it instead of releasing the region. Repeated calls with the
    same label are deduped in plain mode so log output stays one line per
    phase change.
    """
    global _phase, _last_status
    if _mode is None:
        init()
    if label is None:
        _phase = None
        _last_status = None
        clear()
        return
    deduped = label == _phase
    _phase = label
    if _mode == "plain":
        if not deduped:
            msg = f"[PROGRESS] {label}"
            log.ui("[PROGRESS]", msg)
            _last_status = msg
        return
    text = f"[SYSFORGE][PROGRESS] {label}"
    _last_status = text
    if not _reserved:
        _establish_region()
    if _reserved:
        _paint(text)


def clear() -> None:
    """Release the reserved region. Safe to call unconditionally."""
    if _mode == "tty":
        _release_region()


@contextlib.contextmanager
def tracker(total: int, prefix: str) -> Iterator[Tick]:
    """Yield a tick(label) callable. Releases the region on exit.

        with progress.tracker(len(items), "building") as tick:
            for item in items:
                tick(item.name)
                do_work(item)

    Paints a 0/total placeholder on entry so users see immediate feedback
    even when the first tick is far away (e.g. a batch of slow git pulls).

    The yielded ``tick`` also carries ``tick.note(text)`` (repaint at the
    current count with a one-off label, no increment) and ``tick.resume()``
    (repaint the last ``tick`` state) so a caller can overlay a transient
    sub-step — e.g. a just-in-time dep install between two counted builds —
    onto the same ``[i/total]`` counter without advancing it.
    """
    global _last_status
    if _mode is None:
        init()

    class _Tick:
        def __init__(self) -> None:
            self.i = 0
            self.label = ""

        def __call__(self, label: str) -> None:
            self.i += 1
            self.label = label
            render(self.i, total, f"{prefix} · {label}")

        def note(self, text: str) -> None:
            render(self.i, total, text)

        def resume(self) -> None:
            if self.i:
                render(self.i, total, f"{prefix} · {self.label}")

    tick = _Tick()

    if total > 0:
        render(0, total, f"{prefix} · starting...")

    try:
        yield tick
    finally:
        if _phase is not None:
            # An enclosing phase owns the bottom line — hand it back
            # instead of releasing the region between batches.
            label = _phase
            if _mode == "tty":
                _last_status = f"[SYSFORGE][PROGRESS] {label}"
                if _reserved:
                    _paint(_last_status)
            # Plain mode: the phase line was already logged; don't repeat it.
        else:
            clear()
            _last_status = None
