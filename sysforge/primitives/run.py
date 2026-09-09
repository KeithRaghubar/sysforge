# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/run.py — the external-command seam

Two entry points, for the two shapes external commands take here.

``run_or_raise`` centralizes "run a command, raise a tagged error with stderr
on failure" — the pattern that recurs across pipeline stages. The default
captures stderr so the failure message has diagnostic context; pass
capture=False for long-running commands whose progress should stream live to
the terminal (e.g. pacstrap, makepkg).

``capture`` is the *probe* form: ask the system a question, tolerate every
answer including "that tool is not installed". Probes were the reason raw
``subprocess`` calls kept reappearing outside this module — ``run_or_raise``
raises, which is exactly wrong for a probe — so they got a bare call each, and
with it a hand-written ``try/except FileNotFoundError`` and no record in the
run log. ``capture`` gives them a home (3.2.0-F5).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from sysforge import log

_log = log.get_logger("RUN")


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


def capture(
    cmd: list[str],
    **kwargs,
) -> subprocess.CompletedProcess | None:
    """Run *cmd* as a probe: capture text output, never raise.

    The probe contract, in one place:

    * **argv list only** — never a string command (standards row 17).
    * **Non-zero is data, not an error.** ``check=False`` always; the caller
      reads ``returncode`` and decides. A probe that raised would turn "this
      machine does not have that feature" into a stack trace.
    * **A missing binary returns ``None``**, distinct from a command that ran
      and failed. Callers that need to tell "``nm`` is not installed" from "``nm``
      found nothing" can; callers that do not can treat ``None`` as failure.
      This replaces a ``try/except FileNotFoundError`` at every call site, which
      is the kind of thing that is correct in nine places and forgotten in the
      tenth.
    * **Logged at debug**, so ``-vvv`` shows the probes as well as the builds.
      Bare calls appeared nowhere in the run log, which made "why did sysforge
      decide that?" unanswerable from a log alone.

    ``**kwargs`` are forwarded to ``subprocess.run`` (``cwd``, ``env``,
    ``input``, ...). ``capture_output``/``text``/``check`` are set here and
    should not be passed.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs["check"] = False
    _log.debug(f"probe: {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        _log.debug(f"probe: {cmd[0]} not found on PATH")
        return None
