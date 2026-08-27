# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/sudo_session.py — the sudo credential-*lifetime* seam (3.1.0-B5)

The ONE home for "keep sudo credentials warm across a long build" and "make
sure we are authenticated before entering a mutation window".

This is deliberately **not** the privilege seam. ``primitives/privilege.py``
owns *escalation* — turning an argv into a root-running argv — and its docstring
explicitly puts auth probes (``sudo -v``) out of scope. What this module owns is
orthogonal: how long an already-granted credential stays valid, and whether it
is valid *now*. Both are expressed through ``sudo -v``, which is structurally
allowlisted by the ``privilege_seam`` standards check precisely because it
escalates nothing.

Why it exists: a stage that builds for two hours and then installs with sudo
authenticates at stage entry and finds its timestamp long expired by the time
the install runs — so an unattended run stops on a password prompt that then
times out. The toolchain stage solved this with a private keepalive thread; the
kernel stage builds just as long and installs the same way and had no
equivalent. A second copy of the daemon is the drift the one-home invariants
exist to prevent, so the daemon moved here and both stages call it.

Contract: **best-effort and non-fatal**. A failed refresh warns and the loop
continues; it never raises and never blocks a build. Callers that need to know
whether credentials are actually usable ask :func:`authenticate` and branch on
its return value.
"""
from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sysforge import log

# How often (seconds) to refresh sudo credentials during a long build.
# Must stay well under the sudoers default timestamp_timeout (5 minutes); the
# refresh calls sudo from the sysforge process and the stage's install call does
# too, so they share one timestamp entry regardless of timestamp_type.
SUDO_KEEPALIVE_INTERVAL = 60


def authenticate() -> bool:
    """Cache sudo credentials up front; return whether they are usable.

    A no-op returning ``True`` when already root (euid 0) — there is no
    timestamp to warm. Otherwise runs the ``sudo -v`` auth probe with inherited
    stdio so the operator sees and can answer the prompt.

    Returns ``False`` when the probe exits non-zero, which covers the case this
    seam was built for: the prompt went unanswered and ``passwd_timeout`` fired,
    so nothing has been authorised and any privileged step that follows would
    fail without having done anything.
    """
    if os.geteuid() == 0:
        return True
    return subprocess.run(["sudo", "-v"]).returncode == 0


def _keepalive_daemon(stop_event: threading.Event, tag: str) -> None:
    """Background thread: refresh credentials until *stop_event* is set.

    Waits first, so the immediately-preceding :func:`authenticate` is not
    duplicated. Best-effort by contract — a failed refresh warns and the loop
    continues rather than raising into a thread nobody is joining for results.
    """
    while not stop_event.wait(SUDO_KEEPALIVE_INTERVAL):
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            log.warn(
                f"[{tag}]",
                "sudo keepalive failed — install step may prompt "
                "for a password",
            )


@contextmanager
def keepalive(*, tag: str, enabled: bool = True) -> Iterator[None]:
    """Refresh sudo credentials for the duration of the block.

    *tag* is the bare log tag of the owning stage (e.g. ``"PGO"``,
    ``"KERNEL"``) — bracketed here, so callers pass it undecorated. Pass
    ``enabled=False`` — for a dry run, or when already root — to make the whole
    thing a no-op while keeping one code path at the call site.

    The thread is a daemon and is always stopped and joined on exit, including
    on exception, so a stage cannot leak one by forgetting its teardown.
    """
    if not enabled or os.geteuid() == 0:
        yield
        return

    stop = threading.Event()
    thread = threading.Thread(
        target=_keepalive_daemon,
        args=(stop, tag),
        daemon=True,
        name="sysforge-sudo-keepalive",
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
