# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pager.py — pipe ``print()`` through ``$PAGER`` when stdout is a TTY.

Shared by the verbs that emit potentially long output (``state list``,
``state orphans``, ``log``). Honors ``$PAGER`` if set; otherwise tries
``less -RFX`` then ``more``. Degrades to passthrough when stdout isn't
a TTY (CI, redirect, pipe), when the caller passes ``use_pager=False``,
or when no pager binary can be spawned.

Public API:
    maybe_pager(use_pager) -> context manager
"""
import os
import subprocess
import sys
from contextlib import contextmanager


@contextmanager
def maybe_pager(use_pager: bool):
    """Pipe ``print()`` through ``$PAGER`` when stdout is a TTY and the user
    hasn't disabled paging.

    Falls back to a passthrough write when stdout isn't a TTY (CI, redirect),
    when ``use_pager`` is False, or when the pager binary can't be spawned.
    Honors ``$PAGER`` if set; otherwise tries ``less -RFX`` then ``more``.
    """
    if not use_pager or not sys.stdout.isatty():
        yield
        return
    pager_cmd = os.environ.get("PAGER")
    candidates: list[list[str]] = []
    if pager_cmd:
        candidates.append([pager_cmd])
    else:
        # -R: pass ANSI through; -F: quit if one screen; -X: don't clear.
        candidates.append(["less", "-RFX"])
        candidates.append(["more"])
    for cmd in candidates:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
        except (FileNotFoundError, OSError):
            continue
        old_stdout = sys.stdout
        try:
            sys.stdout = proc.stdin  # type: ignore[assignment]
            yield
        finally:
            sys.stdout = old_stdout
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except BrokenPipeError:
                pass
            proc.wait()
        return
    yield
