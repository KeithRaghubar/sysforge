# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pager.py — pipe ``print()`` through ``$PAGER`` when stdout is a TTY.

Shared by the verbs that emit potentially long output (``state list``,
``state orphans``, ``log``). Honors ``$PAGER`` if set (parsed as a shell
word list, so ``PAGER="less -RF"`` works); otherwise tries ``less -RF``
then ``more``. Degrades to passthrough when stdout isn't a TTY (CI,
redirect, pipe), when the caller passes ``use_pager=False``, or when no
pager binary can be spawned.

Public API:
    maybe_pager(use_pager) -> context manager
"""
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager


def _pager_candidates() -> list[list[str]]:
    """Ordered argv candidates to try, most-preferred first.

    ``$PAGER`` (if set) is parsed with :func:`shlex.split` so a value that
    carries its own flags — ``PAGER="less -RF"`` — becomes a real argv rather
    than a single un-spawnable token. The built-in fallbacks follow, so a
    mis-set or missing ``$PAGER`` still degrades to a working pager.

    The ``less`` fallback is ``-RF`` — **never** ``-X``: ``-X`` suppresses the
    terminal's alternate-screen switch, so less paints inline into scrollback
    and its redraws desync on modern terminals (blank open, scroll-up-only,
    looping the top — the B5 mangling). ``-R`` passes ANSI colour through;
    ``-F`` skips the pager entirely when the output fits one screen.
    """
    candidates: list[list[str]] = []
    pager_env = os.environ.get("PAGER")
    if pager_env:
        parts = shlex.split(pager_env)
        if parts:
            candidates.append(parts)
    candidates.append(["less", "-RF"])
    candidates.append(["more"])
    return candidates


@contextmanager
def maybe_pager(use_pager: bool):
    """Pipe ``print()`` through ``$PAGER`` when stdout is a TTY and the user
    hasn't disabled paging.

    Falls back to a passthrough write when stdout isn't a TTY (CI, redirect),
    when ``use_pager`` is False, or when the pager binary can't be spawned.
    Honors ``$PAGER`` if set; otherwise tries ``less -RF`` then ``more`` (see
    :func:`_pager_candidates`).
    """
    if not use_pager or not sys.stdout.isatty():
        yield
        return
    # A verb may still hold an active ui.progress scroll region (DECSTBM
    # ``ESC[1;N-1r``) when it reaches its paged output — `state orphans`
    # paints a "starting…" phase before scanning, then pages the result. A
    # pager launched *inside* that region is clamped to ``[1, N-1]``: less
    # can't own the bottom row, so its alternate-screen redraws desync into
    # the blank-open / scroll-up-only / looping-top corruption (B5). Release
    # the region for the pager's lifetime — the same contract `suspended()`
    # already provides for makepkg's TTY-inheriting child. Writes to stderr,
    # so it's unaffected by the stdout swap below; no-op outside TTY mode.
    from sysforge.ui import progress

    for cmd in _pager_candidates():
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
        except (FileNotFoundError, OSError):
            continue
        old_stdout = sys.stdout
        with progress.suspended():
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
