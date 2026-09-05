# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
import time
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
_INDEX = _ESC + "D"
_RESET_REGION = _ESC + "[r"
_CLEAR_LINE = _ESC + "[2K"

_mode: Optional[str] = None
_reserved: bool = False
_last_status: Optional[str] = None
_last_base: Optional[str] = None
_phase: Optional[str] = None
_rows: int = 0
_cols: int = 0
_sigwinch_installed: bool = False

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
_atexit_installed: bool = False


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
    global _rows, _cols
    sz = shutil.get_terminal_size(fallback=(80, 24))
    _cols, _rows = sz.columns, sz.lines


def _establish_region() -> None:
    global _reserved
    if _reserved:
        return
    _refresh_size()
    if _rows < 3:
        _trace(f"establish-skipped rows={_rows} (terminal too short)")
        return
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
    _write(_SAVE)
    _write(f"{_ESC}[{_rows};1H")
    _write(_INDEX)
    _write(f"{_ESC}[1;{_rows - 1}r")
    _write(_RESTORE)
    _write(f"{_ESC}[1A")
    _reserved = True
    _trace(f"establish rows={_rows} cols={_cols} region=[1,{_rows - 1}]")


def _release_region() -> None:
    global _reserved
    if not _reserved:
        return
    # Resetting the region also homes the cursor, and clearing the bar drives it
    # to the absolute bottom row — bracket both with save/restore so the shell
    # resumes from the real content row, not the bottom of an otherwise-empty
    # screen (the stranded-blank-lines bug).
    _write(_SAVE)
    _write(_RESET_REGION)
    _write(f"{_ESC}[{_rows};1H{_CLEAR_LINE}")
    _write(_RESTORE)
    _reserved = False
    _trace(f"release rows={_rows}")


def _paint(text: str) -> None:
    if _cols <= 0:
        _refresh_size()
    # Downgrade decorative glyphs (·, block-bar fills) before width-truncating —
    # ASCII fallbacks change length, so this must precede the column clamp.
    text = log.downgrade_glyphs(text)
    truncated = text[: max(0, _cols - 1)]
    _write(_SAVE)
    _write(f"{_ESC}[{_rows};1H{_CLEAR_LINE}")
    _write(truncated)
    _write(_RESTORE)
    _trace(f"paint row={_rows} text={truncated!r}")


def _on_sigwinch(*_args) -> None:
    if _mode != "tty" or not _reserved:
        _trace(f"sigwinch-ignored mode={_mode} reserved={_reserved}")
        return
    _trace("sigwinch")
    _release_region()
    _establish_region()
    if _last_status is not None:
        _paint(_last_status)


def reserved_rows() -> int:
    """Rows the progress bar has reserved at the bottom of the terminal (0 or 1).

    A TTY-inheriting pty child must be sized to ``terminal_height -
    reserved_rows()`` so its full-screen redraws/scrolling stay inside the
    DECSTBM region (``[1, N-1]``) and never touch the bar row. This keeps the
    bar permanently visible *during* a subprocess build instead of having it
    collapse output onto the reserved row. See ``pty_runner.run_with_pty``'s
    ``reserve_bottom_rows`` argument.
    """
    return 1 if (_mode == "tty" and _reserved) else 0


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
    global _last_base
    _last_base = text
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
    global _last_base
    _last_base = text
    if not _reserved:
        _establish_region()
    if _reserved:
        _paint(text)



def heartbeat(detail: str) -> None:
    """Repaint the current status with live *detail*, advancing nothing.

    A whole package build sits inside a single ``tick()``, so the bar was
    painted once and then left untouched for the build's entire duration — a
    trace of one real run showed a 379-second gap between paints against a
    4-second runner-up, which is a bar that is stale for six minutes rather
    than one that is working (3.2.0-B13). ``pty_runner``'s idle callback
    already knows the child is alive and what it is compiling every
    ``MAKEPKG_HEARTBEAT_S``; this is the channel that was missing between the
    two. The periodic repaint also makes the reserved row self-healing, since
    anything that corrupts it is now overwritten within the heartbeat interval
    rather than persisting for the rest of the build.

    Detail replaces rather than appends — it fires for the length of a build,
    so appending would grow the line without bound — and the next ``render()``
    or ``phase()`` overwrites it, since those set a new base. No-op before any
    status exists, and in plain mode, where painting goes through ``log.ui()``
    and a 30-second repaint would append to the log forever (the per-package
    log already receives the same heartbeat from ``makepkg_invoke``).
    """
    global _last_status
    if _mode is None:
        init()
    if _mode != "tty" or not _last_base:
        return
    text = f"{_last_base} · {detail}" if detail else _last_base
    _last_status = text
    if not _reserved:
        _establish_region()
    if _reserved:
        _paint(text)


def clear() -> None:
    """Release the reserved region. Safe to call unconditionally."""
    if _mode == "tty":
        _release_region()


def suspend_for_prompt() -> None:
    """Make the bottom bar safe for an interactive prompt without releasing
    the region. Safe to call unconditionally.

    Blanks the reserved status row *in place* — the same
    save → goto-bottom → clear → restore dance ``_paint`` uses every frame —
    but does **not** reset the DECSTBM region and does **not** move the
    logical cursor. The prompt is therefore printed in the normal content
    flow (where it is visible) with no stale bar text to collide with.

    This is deliberately *not* :func:`clear`: a full release resets the
    scroll region (``ESC[r``), and the cursor-restore across that reset is
    unreliable on some terminals — it left prompts rendering off-view. The
    region survives, so the next ``render()``/``phase()``/tick simply repaints
    the bar; nothing needs to re-establish it.
    """
    global _last_status
    if _mode != "tty" or not _reserved:
        return
    _write(_SAVE)
    _write(f"{_ESC}[{_rows};1H{_CLEAR_LINE}")
    _write(_RESTORE)
    _last_status = None


@contextlib.contextmanager
def suspended() -> Iterator[None]:
    """Fully release the region for the body, then restore the bar.

    Wrap a TTY-inheriting subprocess that forwards its **own** cursor-addressed
    output (``makepkg`` → cargo/ninja/cmake live progress) — the scroll region
    would otherwise clamp the child's full-screen redraws into the reserved
    band and collapse its output onto the bar row. Releasing the region hands
    the child a clean, unconstrained terminal; on exit the bar is re-established
    and repainted (lazily anyway on the next ``render()``/``phase()``/tick).

    No-op outside TTY mode. Safe to nest / call when nothing is reserved.
    """
    was_reserved = _reserved
    status = _last_status
    if _mode == "tty":
        _release_region()
    try:
        yield
    finally:
        if _mode == "tty" and was_reserved and status is not None:
            _establish_region()
            if _reserved:
                _paint(status)


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
