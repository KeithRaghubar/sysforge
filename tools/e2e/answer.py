#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""answer.py -- run a command on a terminal and answer its prompts.

Usage: answer.py [PATTERN REPLY]... -- COMMAND [ARG...]

The command runs on a pseudo-terminal, so it takes its interactive paths (the
review gate, the repo opt-in) as at a real one. Each PATTERN is a regular
expression waited for in order on the output seen since the previous match;
on a match its REPLY is typed (``\\n`` in REPLY is Enter). Output is copied to
stdout without what would place it on the screen (``TerminalFilter``), and the
exit status is the command's.

A reply with no Enter answers a single-key prompt, which switches the terminal
to cbreak mode only after printing itself and discards what was typed before
(``tty.setcbreak`` flushes input). So, as a person would, it is typed once the
terminal has left line mode, not as soon as the prompt shows.

A prompt that never appears is not an error here: the command just runs on,
and the scenario's assertions catch what it did instead. A command still
running after E2E_TIMEOUT seconds (default 1800) is killed, exit 124.
"""

import os
import pty
import re
import select
import signal
import sys
import termios
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")

# Any complete escape sequence: CSI, OSC (BEL- or ST-terminated), or the short
# ESC forms (DECSC/DECRC, IND, charset designation, RIS). The short form never
# ends in `[` or `]`: those open a CSI or OSC, which an unfinished read must
# hold rather than take as complete.
_SEQ = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[0-Z\\^-~])")
# Position-independent, so kept: colour, and erase to end of line (a \r repaint).
_KEEP = re.compile(rb"\x1b\[[0-9;:]*m|\x1b\[0?K")
_SAVE = (b"\x1b7", b"\x1b[s")
_RESTORE = (b"\x1b8", b"\x1b[u")
# An unfinished sequence at the end of a read is held for the next one, up to
# this long; longer, its ESC is dropped and the rest passes as text.
_MAX_PENDING = 256


class TerminalFilter:
    """Copy a pty stream to a terminal the child never measured (4.0.0-DEV3).

    The pty has no window size, so sysforge assumes 80x24: its progress bar
    sets a scroll region for rows 1-23 and paints row 24, and through
    ``podman exec`` that lands on the terminal running ``make e2e``, whatever
    its real height. Text, newlines, ``\\r`` and the sequences in ``_KEEP``
    pass; every other sequence is dropped, and so is text drawn between a
    cursor save and its restore, which is drawn away from the content (the
    bar row). A bar paint is one row, so a newline inside the bracket means it
    was not one, and the bracket's text is kept.
    """

    def __init__(self) -> None:
        self._pending = b""
        self._held: bytearray | None = None

    def feed(self, data: bytes) -> bytes:
        data, self._pending = self._pending + data, b""
        out = bytearray()
        i = 0
        while i < len(data):
            esc = data.find(b"\x1b", i)
            if esc < 0:
                self._text(data[i:], out)
                break
            self._text(data[i:esc], out)
            m = _SEQ.match(data, esc)
            if m is None:
                if len(data) - esc < _MAX_PENDING:
                    self._pending = data[esc:]
                    break
                i = esc + 1  # a stray ESC
                continue
            self._sequence(m.group(), out)
            i = m.end()
        return bytes(out)

    def finish(self) -> bytes:
        """What is still held at the end: an unrestored bracket's text."""
        out = bytes(self._held or b"")
        self._held, self._pending = None, b""
        return out

    def _text(self, text: bytes, out: bytearray) -> None:
        if self._held is None:
            out += text
        elif b"\n" in text:
            out += self._held + text
            self._held = None
        else:
            self._held += text

    def _sequence(self, seq: bytes, out: bytearray) -> None:
        if seq in _SAVE:
            if self._held is not None:
                out += self._held
            self._held = bytearray()
        elif seq in _RESTORE:
            self._held = None
        elif _KEEP.fullmatch(seq):
            if self._held is not None:
                self._held += seq
            else:
                out += seq


def _wait_for_key_mode(fd: int, deadline: float) -> None:
    """Wait until the terminal leaves line mode (the master reports the slave's
    settings), or 10 seconds pass: a prompt that never switches gets the reply
    anyway."""
    give_up = min(deadline, time.monotonic() + 10)
    while time.monotonic() < give_up:
        if not termios.tcgetattr(fd)[3] & termios.ICANON:
            time.sleep(0.1)  # past the flush, should the mode predate the prompt
            return
        time.sleep(0.05)


def main(argv: list[str]) -> int:
    if "--" not in argv:
        sys.exit(__doc__)
    split = argv.index("--")
    pairs, command = argv[:split], argv[split + 1:]
    if len(pairs) % 2 or not command:
        sys.exit(__doc__)
    script = [(re.compile(pairs[i].encode()), pairs[i + 1].replace("\\n", "\r").encode())
              for i in range(0, len(pairs), 2)]
    deadline = time.monotonic() + float(os.environ.get("E2E_TIMEOUT", "1800"))

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)  # noqa: S606 — the scenario's own argv, no shell

    seen = b""
    shown = TerminalFilter()
    while True:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            print("\nanswer.py: timed out", file=sys.stderr)
            return 124
        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # EIO: the child closed the terminal
            break
        if not chunk:
            break
        sys.stdout.buffer.write(shown.feed(chunk))
        sys.stdout.buffer.flush()
        seen += ANSI.sub(b"", chunk)
        if script and script[0][0].search(seen):
            reply = script.pop(0)[1]
            if not reply.endswith(b"\r"):
                _wait_for_key_mode(fd, deadline)
            os.write(fd, reply)
            seen = b""
    sys.stdout.buffer.write(shown.finish())
    sys.stdout.buffer.flush()
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
