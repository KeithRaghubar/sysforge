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
"""
from __future__ import annotations

import os
import subprocess

from sysforge.primitives.run import run_or_raise


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
    """Escalate *argv* and run it through :func:`run_or_raise` (raise on non-zero)."""
    return run_or_raise(privileged_argv(argv), tag=tag, **kwargs)
