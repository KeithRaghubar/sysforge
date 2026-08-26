# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
editor.py — shared editor / merge-tool resolution and TTY-safe launch.

Single home for spawning an interactive editor or diff/merge tool from a verb
or stage. Every caller that opens ``$EDITOR``/``vimdiff``/etc. on a file goes
through here so the ``/dev/tty`` rebinding (needed when sysforge runs under
output redirection) and the resolution order are consistent and tested once.

Public API:
    run_tty_argv(argv)      -> int            TTY-safe subprocess launch
    resolve_editor()        -> (str, str)     single-file editor + source
    describe_editor_chain() -> (list[ChainRung], int)  ordered rungs + winner
    editor_usable(editor)   -> bool
    resolve_merge_tool()    -> (list[str], str)  diff/merge argv prefix + source
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass

from sysforge.primitives.config import load_sysforge_toml


def run_tty_argv(argv: list[str]) -> int:
    """Run ``argv`` with stdin/stdout/stderr bound to ``/dev/tty`` when one is
    available. Without this, sysforge invoked under output redirection
    (e.g. ``sysforge ... | tee log``) would launch the editor with a piped
    stdout, and TUI editors like nvim detect the non-tty and exit silently
    without ever drawing. Works for both single-file editors (``$EDITOR file``)
    and two-file diff/merge tools (``vimdiff a b``).

    The child also runs inside :func:`sysforge.ui.progress.suspended`. A verb
    may hold an active progress bar when it reaches an edit prompt — the
    recovery menu in ``makepkg_invoke`` opens ``[e]`` from inside `update`'s
    ``"building"`` tracker — and the bar reserves the bottom row with a DECSTBM
    region. A full-screen editor handed the raw tty sizes itself to the whole
    terminal and paints its own status line onto that reserved row, so the two
    fight over it and the bottom line is left corrupted on exit (3.1.0-B10).
    Releasing the region for the child's lifetime is the same contract
    ``maybe_pager`` already relies on (B5); doing it here covers every editor
    launch at once. No-op outside TTY mode.

    Returns the child's exit code, or -1 if the binary couldn't be found.
    """
    from sysforge.ui import progress

    tty_fd: int | None = None
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        tty_fd = None

    try:
        with progress.suspended():
            if tty_fd is not None:
                result = subprocess.run(
                    argv, stdin=tty_fd, stdout=tty_fd, stderr=tty_fd)
            else:
                result = subprocess.run(argv)
        return result.returncode
    except FileNotFoundError:
        return -1
    finally:
        if tty_fd is not None:
            os.close(tty_fd)


_DETECT_FALLBACKS = ("vim", "nano", "vi")


@dataclass(frozen=True)
class ChainRung:
    """One rung of the editor resolution order, for display.

    ``usable`` is False when the rung holds a value that does not resolve on
    PATH — resolution skips it, and the display says so rather than leaving
    the user to wonder why a set variable lost.
    """
    index: int          # 1-based, as displayed
    label: str
    source: str         # matches resolve_editor()'s second return value
    value: str
    usable: bool
    detail: str


def describe_editor_chain() -> tuple[list[ChainRung], int]:
    """Return the editor resolution order plus the winning rung's index.

    The single home for editor precedence: :func:`resolve_editor` is a thin
    reader over this, so the rendered chain cannot disagree with the editor
    that actually launches. Returns ``(rungs, winner)`` where ``winner`` is a
    0-based index into ``rungs``, or ``-1`` when nothing resolves.
    """
    cfg = load_sysforge_toml()
    raw = [
        ("SYSFORGE_EDITOR", "SYSFORGE_EDITOR", os.environ.get("SYSFORGE_EDITOR")),
        ("sysforge.toml [ui]", "sysforge.toml", cfg.get("ui", {}).get("editor")),
        ("$EDITOR", "$EDITOR", os.environ.get("EDITOR")),
        ("$VISUAL", "$VISUAL", os.environ.get("VISUAL")),
    ]

    rungs: list[ChainRung] = []
    winner = -1
    for i, (label, source, value) in enumerate(raw):
        usable = bool(value) and shutil.which(value) is not None
        detail = "" if not value or usable else "not on PATH"
        rungs.append(
            ChainRung(
                index=i + 1, label=label, source=source,
                value=value or "", usable=usable, detail=detail,
            )
        )
        if usable and winner < 0:
            winner = i

    found = [c for c in _DETECT_FALLBACKS if shutil.which(c)]
    # Derived, never literal: the display index and the winner it claims both
    # follow `raw`'s length, so adding or removing a rung above cannot
    # mis-number the chain or point `resolve_editor` at the wrong row.
    rungs.append(
        ChainRung(
            index=len(raw) + 1,
            label="detected on PATH",
            source="detected",
            value=found[0] if found else "",
            usable=bool(found),
            detail=", ".join(found) if found else "none found",
        )
    )
    if winner < 0 and found:
        winner = len(raw)
    return rungs, winner


def resolve_editor() -> tuple[str, str]:
    """Resolve a single-file editor and the source it came from.

    Order: ``SYSFORGE_EDITOR`` → ``sysforge.toml [ui].editor`` → ``$EDITOR`` →
    ``$VISUAL`` → detected (``vim``/``nano``/``vi``). Each candidate must
    resolve on PATH; otherwise a stale env var or config entry would propagate
    as the "editor" and every subsequent edit prompt would silently fail to
    open anything. Returns ``("", "none")`` when nothing resolves so callers
    can force the user to pick one rather than lie about a non-existent ``vi``.

    Precedence lives in :func:`describe_editor_chain`; this is the reader.
    """
    rungs, winner = describe_editor_chain()
    if winner < 0:
        return "", "none"
    return rungs[winner].value, rungs[winner].source


def editor_usable(editor: str) -> bool:
    """True when ``editor`` is set and resolves on PATH."""
    return bool(editor) and shutil.which(editor) is not None


def resolve_merge_tool() -> tuple[list[str], str]:
    """Resolve the diff/merge tool argv prefix for ``.sfnew`` adoption.

    Resolution mirrors :func:`resolve_editor`'s shape:
      ``SYSFORGE_MERGE`` → ``sysforge.toml [ui].merge`` → ``$DIFFPROG`` →
      ``vimdiff``.
    Each candidate is whitespace-split (so ``"nvim -d"`` and ``"meld"`` both
    work) and its first token validated on PATH; the first resolvable
    candidate wins. ``$DIFFPROG`` is honoured for muscle-memory parity with
    pacdiff. Returns ``(argv_prefix, source)``; ``argv_prefix`` is empty with
    source ``"none"`` when nothing resolves.
    """
    cfg = load_sysforge_toml()
    candidates = [
        (os.environ.get("SYSFORGE_MERGE"), "SYSFORGE_MERGE"),
        (cfg.get("ui", {}).get("merge"), "sysforge.toml"),
        (os.environ.get("DIFFPROG"), "$DIFFPROG"),
        ("vimdiff", "vimdiff"),
    ]
    for value, source in candidates:
        if not value:
            continue
        parts = shlex.split(value)
        if parts and shutil.which(parts[0]):
            return parts, source
    return [], "none"
