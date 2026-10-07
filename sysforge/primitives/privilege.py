# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/privilege.py — the privilege-escalation seam (2.3.0-F10 / STD row 18)

The ONE home for "run this as root". Every root-escalating subprocess
invocation routes its argv through :func:`privileged_argv` so escalation has a
single audit point and one consistent "am I already root?" decision.

Two entry points, mirroring the streaming/returncode carve-out in
``primitives/run.py``:

- :func:`privileged_argv` — build the escalated argv; the caller runs it with
  its own ``subprocess.run`` (for sites that stream to the TTY or inspect the
  return code themselves).
- :func:`run_privileged` — escalate + ``run_or_raise`` (for sites that just want
  raise-on-failure).

Out of scope (NOT escalation): auth probes (``sudo -v``, ``sudo -n true``) and
drop-privilege (``sudo -u <user>``). Those stay as raw calls and are allowlisted
by the ``privilege_seam`` standards check.

:func:`ensure_credentials` is the one place a password prompt sysforge raises
itself is made: it probes first and, only when sudo would ask, prompts with the
terminal yielded so the progress bar is hidden and its clock paused (3.3.0-F3).
Call it right before running something privileged — never from
:func:`privileged_argv`, which also builds argv that a ``--dry-run`` only prints.
"""
from __future__ import annotations

import os
import subprocess

from sysforge.primitives import progress_hooks
from sysforge.primitives.run import run_or_raise


def _credentials_cached() -> bool:
    """``sudo -n true``: would sudo run without asking? The auth probe."""
    return subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                          capture_output=True, check=False).returncode == 0


def ensure_credentials() -> bool:
    """Make sure the next ``sudo`` will not prompt mid-output; return usability.

    ``True`` without running anything when already root. Otherwise probes
    ``sudo -n true`` (cached credentials → done, no yield). When sudo would
    prompt, runs ``sudo -v`` inside ``yield_terminal("prompt")`` so the
    operator answers a prompt that is not drawn over by the bar and whose wait
    does not count as build time. Returns ``False`` when that prompt fails
    (wrong password, timeout) — the following command would prompt again and
    fail on its own, so callers may ignore it.
    """
    if os.geteuid() == 0 or _credentials_cached():
        return True
    with progress_hooks.hooks().yield_terminal("prompt"):
        return subprocess.run(["sudo", "-v"], check=False).returncode == 0


def privileged_argv(argv: list[str], *, noninteractive: bool = False) -> list[str]:
    """Return *argv* escalated to root.

    When already root (euid 0) the argv is returned unchanged. Otherwise it is
    prefixed with ``sudo``; ``noninteractive=True`` inserts ``-n`` so sudo fails
    fast instead of prompting (moot when already root).
    """
    if os.geteuid() == 0:
        return list(argv)
    return ["sudo", *(["-n"] if noninteractive else []), *argv]


def run_privileged(
    argv: list[str], *, tag: str, **kwargs
) -> subprocess.CompletedProcess:
    """Escalate *argv* and run it through :func:`run_or_raise` (raise on non-zero).

    Credentials are acquired first via :func:`ensure_credentials`, so a prompt
    never lands under the progress bar.
    """
    ensure_credentials()
    return run_or_raise(privileged_argv(argv), tag=tag, **kwargs)
