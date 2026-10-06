# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Rendered-listing tests for ``completions/_sysforge`` (3.3.0-B15).

The static parity checks in ``tools/check_shipped.py`` read the script as text
and cannot see how zsh lays out a listing. These drive a real interactive
``zsh -f`` on a pty, press TAB twice after a bare ``-`` and read the screen.
In a sound listing every row is ``<names>  -- <description>``. The B15 failure
mode prints the option names in one block and the descriptions in another, so
a row is a name with no description, or a description with no name."""
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import termios
import time
from pathlib import Path

import pytest

ZSH = shutil.which("zsh")
COMPLETIONS = Path(__file__).resolve().parent.parent / "completions"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")
# `--quiet  -q  -- suppress …`: one or more option names, the gutter, a description.
_ROW = re.compile(r"^-\S*(?:\s+-\S+)*\s+-- \S")

pytestmark = pytest.mark.skipif(ZSH is None, reason="zsh not installed")


def _read(fd: int, *, until: bytes | None, timeout: float, quiet: float = 0.5) -> str:
    """Read from ``fd`` until ``until`` appears, or (with ``until=None``) until
    output has been quiet for ``quiet`` seconds after the first byte."""
    buf = b""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            buf += chunk
            last = time.monotonic()
            if until is not None and until in buf:
                break
        elif until is None and last is not None and time.monotonic() - last > quiet:
            break
    return _ANSI.sub("", buf.decode(errors="replace"))


def _listing(line: str, tmp_path: Path) -> list[str]:
    """Rows zsh lists for ``line`` + TAB TAB, one string per screen row."""
    assert ZSH is not None
    env = {"HOME": str(tmp_path), "TERM": "xterm", "PATH": "/usr/bin:/bin"}
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover — child
        os.execve(ZSH, ["zsh", "-f", "-i"], env)
    try:
        # Wide enough that no row wraps, so a wrapped row cannot pass for a split one.
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 100, 200, 0, 0))
        os.write(fd, (
            f"PS1='> '; fpath=({COMPLETIONS} $fpath); autoload -Uz compinit; "
            "compinit -u -D; unsetopt beep; print READY$((1+1))\n").encode())
        _read(fd, until=b"READY2", timeout=10)
        os.write(fd, f"{line}\t\t".encode())
        screen = _read(fd, until=None, timeout=10)
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    return [row.rstrip() for row in screen.splitlines()[1:] if row.strip()]


# The five leaves the entry names, plus the bare `doctor` group, which has the
# same alias pair and the same split. `run kernel` (no alias pair) is the control.
@pytest.mark.parametrize("line", [
    "sysforge doctor -",
    "sysforge doctor system -",
    "sysforge doctor pkg -",
    "sysforge build -",
    "sysforge update -",
    "sysforge run toolchain -",
    "sysforge run kernel -",
])
def test_option_listing_keeps_each_name_beside_its_description(line, tmp_path):
    rows = _listing(line, tmp_path)
    assert rows, f"no listing for {line!r}"
    for row in rows:
        assert _ROW.match(row), (
            f"{line!r}: row detached from its name or description: {row!r}\n"
            + "\n".join(rows))
    help_rows = [r for r in rows if r.startswith("--help")]
    assert len(help_rows) == 1, f"{line!r}: expected one --help row:\n" + "\n".join(rows)
    assert " -h " in help_rows[0], f"{line!r}: -h is not aliased with --help"
