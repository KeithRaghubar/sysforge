# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/run.py — `run_or_raise` subprocess wrapper

Centralizes the "run a command, raise a tagged error with stderr on failure"
pattern that recurs across pipeline stages. The default captures stderr so
the failure message has diagnostic context; pass capture=False for
long-running commands whose progress should stream live to the terminal
(e.g. pacstrap, makepkg).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def run_or_raise(
    cmd: list[str],
    *,
    tag: str,
    operation: str | None = None,
    hint: str | None = None,
    capture: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """
    Run *cmd* and raise RuntimeError on non-zero exit.

    Args:
        cmd:        argv list passed to subprocess.run.
        tag:        stage/component tag (e.g. "PARTITION") prefixed to the
                    error message as ``[TAG]``.
        operation:  short label for the operation (defaults to ``cmd[0]``'s
                    basename). Appears as ``{operation} failed`` in the error.
        hint:       extra guidance appended when no stderr is captured (e.g.
                    "Check network connectivity and pacman keyring.").
        capture:    True (default) captures stderr+stdout for diagnostics;
                    False lets both streams flow to the terminal directly.
        **kwargs:   forwarded to subprocess.run (cwd, env, input, ...).

    Returns:
        The CompletedProcess on success — callers can read .stdout when
        needed (e.g. genfstab piping to fstab).

    Raises:
        RuntimeError: ``[TAG] {operation} failed (exit N): {detail}``
                      where detail is captured stderr, then hint, then a
                      generic fallback.
    """
    if capture:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)

    result = subprocess.run(cmd, **kwargs)
    if result.returncode == 0:
        return result

    op = operation or Path(cmd[0]).name
    detail = ""
    if capture and result.stderr:
        detail = result.stderr.strip()
    if not detail and hint:
        detail = hint
    if not detail:
        detail = "no output captured"

    raise RuntimeError(
        f"[{tag}] {op} failed (exit {result.returncode}): {detail}"
    )
