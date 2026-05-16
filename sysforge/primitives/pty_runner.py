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

Public API:
    run_with_pty(cmd, *, cwd, env, line_callback, forward_bytes, preexec_fn=None) -> int
"""
import codecs
import errno
import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
from pathlib import Path
from typing import Callable, Optional


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
) -> int:
    """Spawn cmd with stdout+stderr attached to a pty. Returns the child's
    return code. stdin is inherited from the parent (DEVNULL if unavailable)."""
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
        while True:
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
