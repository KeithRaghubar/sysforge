"""
pty_runner.py — run a subprocess attached to a pseudo-terminal.

Allocates a pty for the child's stdout+stderr so tools that gate live UI on
isatty() (cargo, configure scripts with spinners) emit their progress
animation. Reads raw bytes from the pty master in the parent and:

  - forwards them verbatim to sys.stdout.buffer when forward_bytes=True (so
    \\r-based progress redraws render live), and
  - decodes + splits on \\n and delivers each clean line to line_callback,
    regardless of forward_bytes.

Line splitting is on \\n only: cargo's progress bar uses \\r to redraw the
current row in place, so treating \\r as a line boundary would deliver
every redraw as a separate "line". rstrip("\\r") strips redraw remnants
before invoking line_callback.

Heartbeat: when idle_callback is set, the runner uses select() to wake up
every idle_timeout_s seconds. If no line has been delivered in that window,
idle_callback is invoked with either the most recent \\r-overwritten buffer
segment (so tools like ninja that redraw a status line in place still report
progress) or None (the child is producing no output at all). The buffer is
not consumed by idle_callback — subsequent \\n still delivers the original
inter-newline content unchanged to line_callback.

Public API:
    run_with_pty(cmd, *, cwd, env, line_callback, forward_bytes,
                 preexec_fn=None, idle_callback=None, idle_timeout_s=30.0) -> int
"""
import codecs
import errno
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# Terminal escape sequences a pty child may embed in its output. Because the
# child sees a tty, compilers emit SGR colors, erase-line (CSI K), and OSC-8
# hyperlinks even in "non-interactive" builds — GCC >= 16 wraps quoted option
# names in OSC-8 links, splitting the visible text mid-token. Order matters:
# OSC must precede the bare two-byte branch (ESC ] would otherwise match it).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (hyperlinks, titles), BEL/ST-terminated
    r"|\x1b\[[0-9;:?]*[ -/]*[@-~]"         # CSI (SGR colors, erase-line K)
    r"|\x1b[@-Z\\-_]"                      # remaining two-byte C1 escapes
)


def strip_ansi(text: str) -> str:
    """Remove ANSI/OSC terminal escape sequences from *text*.

    Use this before substring-matching pty-captured output: escape bytes can
    sit inside the token being matched (e.g. GCC's OSC-8-hyperlinked
    ``'-flto='``), so patterns must run against the visible text only.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def run_with_pty(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    line_callback: Callable[[str], None],
    forward_bytes: bool,
    preexec_fn: Optional[Callable[[], None]] = None,
    idle_callback: Optional[Callable[[Optional[str]], None]] = None,
    idle_timeout_s: float = 30.0,
) -> int:
    """Spawn cmd with stdout+stderr attached to a pty. Returns the child's
    return code. stdin is inherited from the parent (DEVNULL if unavailable).

    If idle_callback is set, it is invoked when no line has been delivered for
    idle_timeout_s seconds. It receives either the latest ``\\r``-overwritten
    buffer segment (so tools like ninja that redraw a status line in place
    still surface progress) or ``None`` when the child has produced no output
    at all in the window. The buffer is not mutated by the heartbeat path."""
    master_fd, slave_fd = pty.openpty()

    sz = shutil.get_terminal_size(fallback=(80, 24))
    _set_winsize(slave_fd, sz.lines, sz.columns)

    try:
        stdin_arg = sys.stdin.fileno() if sys.stdin and sys.stdin.fileno() >= 0 else subprocess.DEVNULL
    except (OSError, ValueError):
        stdin_arg = subprocess.DEVNULL

    prev_winch = None
    is_main_thread = threading.current_thread() is threading.main_thread()

    def _on_winch(sig, frame):
        new_sz = shutil.get_terminal_size(fallback=(80, 24))
        _set_winsize(master_fd, new_sz.lines, new_sz.columns)
        if callable(prev_winch):
            prev_winch(sig, frame)

    proc: Optional[subprocess.Popen] = None
    try:
        if is_main_thread:
            try:
                prev_winch = signal.signal(signal.SIGWINCH, _on_winch)
            except (OSError, ValueError):
                prev_winch = None

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=stdin_arg,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=preexec_fn,
        )
        os.close(slave_fd)
        slave_fd = -1

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        last_activity = time.monotonic()
        while True:
            if idle_callback is not None:
                wait = max(0.0, idle_timeout_s - (time.monotonic() - last_activity))
            else:
                wait = None
            try:
                ready, _, _ = select.select([master_fd], [], [], wait)
            except (OSError, ValueError):
                break
            if not ready:
                # Idle timeout: surface progress without consuming the buffer.
                # buf may hold an incomplete line (no \n yet); pass only the
                # latest \r-overwritten segment so ninja-style status redraws
                # report the current step instead of the whole redraw history.
                if idle_callback is not None:
                    idle_callback(buf.split("\r")[-1] if buf else None)
                    last_activity = time.monotonic()
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            if forward_bytes:
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, AttributeError):
                    pass
            buf += decoder.decode(chunk)
            while (idx := buf.find("\n")) != -1:
                line, buf = buf[:idx], buf[idx + 1:]
                line_callback(line.rstrip("\r"))
                last_activity = time.monotonic()

        buf += decoder.decode(b"", final=True)
        if buf:
            line_callback(buf.rstrip("\r"))

        proc.wait()
        return proc.returncode
    finally:
        if is_main_thread and prev_winch is not None:
            try:
                signal.signal(signal.SIGWINCH, prev_winch)
            except (OSError, ValueError):
                pass
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
