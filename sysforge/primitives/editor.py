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
    editor_usable(editor)   -> bool
    resolve_merge_tool()    -> (list[str], str)  diff/merge argv prefix + source
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from sysforge.primitives.config import load_sysforge_toml


def run_tty_argv(argv: list[str]) -> int:
    """Run ``argv`` with stdin/stdout/stderr bound to ``/dev/tty`` when one is
    available. Without this, sysforge invoked under output redirection
    (e.g. ``sysforge ... | tee log``) would launch the editor with a piped
    stdout, and TUI editors like nvim detect the non-tty and exit silently
    without ever drawing. Works for both single-file editors (``$EDITOR file``)
    and two-file diff/merge tools (``vimdiff a b``).

    Returns the child's exit code, or -1 if the binary couldn't be found.
    """
    tty_fd: int | None = None
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        tty_fd = None

    try:
        if tty_fd is not None:
            result = subprocess.run(argv, stdin=tty_fd, stdout=tty_fd, stderr=tty_fd)
        else:
            result = subprocess.run(argv)
        return result.returncode
    except FileNotFoundError:
        return -1
    finally:
        if tty_fd is not None:
            os.close(tty_fd)


def resolve_editor() -> tuple[str, str]:
    """Resolve a single-file editor and the source it came from.

    Order: ``SYSFORGE_EDITOR`` → ``sysforge.toml [ui].editor`` → ``$EDITOR`` →
    ``$VISUAL`` → detected (``vim``/``nano``/``vi``). Each candidate must
    resolve on PATH; otherwise a stale env var or config entry would propagate
    as the "editor" and every subsequent edit prompt would silently fail to
    open anything. Returns ``("", "none")`` when nothing resolves so callers
    can force the user to pick one rather than lie about a non-existent ``vi``.
    """
    cfg = load_sysforge_toml()
    candidates = [
        (os.environ.get("SYSFORGE_EDITOR"), "SYSFORGE_EDITOR"),
        (cfg.get("ui", {}).get("editor"), "sysforge.toml"),
        (os.environ.get("EDITOR"), "$EDITOR"),
        (os.environ.get("VISUAL"), "$VISUAL"),
    ]
    for value, source in candidates:
        if value and shutil.which(value):
            return value, source
    for fallback in ("vim", "nano", "vi"):
        if shutil.which(fallback):
            return fallback, "detected"
    return "", "none"


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
