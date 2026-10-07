# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/run.py — the external-command seam

Three entry points, for the shapes external commands take here.

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

``probe`` is ``capture`` for callers that only branch on ``returncode``: a
missing binary is a ``CompletedProcess`` with status 127 rather than ``None``,
so the caller's failure branch covers it (3.2.0-F13).

Since 3.2.0-F13 a ruff ``banned-api`` rule (TID251) forbids ``subprocess.*``
everywhere except this module, ``pty_runner``, ``privilege`` and
``sudo_session``. The calls that legitimately stay raw (streaming or
TTY-inheriting invocations, a privileged command whose status the caller
inspects, a custom ``preexec_fn``) carry a ``# noqa: TID251`` naming why.
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
    ``input``, ``timeout``, ...). ``check`` is always ``False``;
    ``capture_output``/``text`` default on and may be overridden (``text=False``
    for bytes), and an explicit ``stdout``/``stderr`` replaces the capture.
    """
    # A caller routing the streams itself (``stdout=PIPE, stderr=DEVNULL``)
    # keeps them: subprocess rejects capture_output alongside either.
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs["check"] = False
    _log.debug(f"probe: {' '.join(str(c) for c in cmd)}")
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        _log.debug(f"probe: {cmd[0]} not found on PATH")
        return None


# The shell's "command not found" status, used by probe() for a missing binary.
MISSING_BINARY_RC = 127


def probe(
    cmd: list[str],
    **kwargs,
) -> subprocess.CompletedProcess:
    """:func:`capture`, but never ``None``: a missing binary is a failed command.

    For the many probes that only branch on ``returncode`` (``git rev-parse``,
    ``pacman -Q``, ``bsdtar -t``): a missing binary comes back as a
    ``CompletedProcess`` with ``returncode`` :data:`MISSING_BINARY_RC` (127, the
    shell's command-not-found status), empty ``stdout`` and a ``stderr`` saying
    so, so the caller's existing non-zero branch handles it — instead of the
    ``FileNotFoundError`` a bare ``subprocess.run`` raised at every such site.
    Use :func:`capture` where "not installed" must be told apart from "ran and
    failed". Same keyword handling as :func:`capture` (3.2.0-F13).
    """
    result = capture(cmd, **kwargs)
    if result is not None:
        return result
    text = kwargs.get("text", True)
    empty = "" if text else b""
    msg = f"{cmd[0]}: command not found"
    return subprocess.CompletedProcess(
        cmd, MISSING_BINARY_RC, empty, msg if text else msg.encode())
