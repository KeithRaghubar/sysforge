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
stdout, and the exit status is the command's.

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
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        seen += ANSI.sub(b"", chunk)
        if script and script[0][0].search(seen):
            reply = script.pop(0)[1]
            if not reply.endswith(b"\r"):
                _wait_for_key_mode(fd, deadline)
            os.write(fd, reply)
            seen = b""
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
