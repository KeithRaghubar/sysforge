# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_lock.py — advisory cross-process lock for build-bearing scopes.

A thin ``flock``-based context manager used to refuse two concurrent runs
that would clobber a shared on-disk build area (the PGO staging dirs +
``pgo_store`` for the toolchain stage, ``~/builds/<pkgbase>`` for the kernel
stage). The holder PID is written into the lock file so the loser can
surface a useful error.

This is *not* the stage sentinel, though the sentinel builds on it. The
sentinel records an interrupted boot-critical mutation durably so the next
run prompts for recovery; this lock is a transient mutual-exclusion guard
held only for the duration of one run. ``stage_sentinel.sentinel_scope``
pairs the two: it holds a lock here (``stage_in_progress.lock``) for the
stage's lifetime so "is the sentinel's owner still alive?" reduces to "is
the lock takeable?" — a question presence of the sentinel file cannot
answer. The toolchain stage, the kernel stage and the sentinel all share
this one primitive — don't roll a second flock path.

Callers name the contended thing via ``label`` + ``noun`` (e.g.
``label="kernel", noun="build"`` → "Another sysforge kernel build is
running"). ``noun`` is required rather than defaulted to ``"build"`` so a
non-build caller cannot silently inherit build wording.
"""
import contextlib
import fcntl
import os
from pathlib import Path


@contextlib.contextmanager
def build_lock(lock_path: Path, *, label: str, noun: str):
    """Acquire an exclusive advisory lock at ``lock_path`` for the scope.

    Raises RuntimeError immediately (non-blocking) if another process holds
    the lock, naming the holder PID when it can be read. ``label`` and
    ``noun`` are woven into the error message (e.g. ``label="PGO
    toolchain", noun="build"`` / ``label="install", noun="stage"``).

    ``O_CLOEXEC`` keeps the fd out of exec'd children. Every subprocess path
    in the tree already prevents inheritance (``subprocess`` defaults to
    ``close_fds=True``), so this is hardening: it stops a future
    ``close_fds=False`` caller from leaving a surviving grandchild holding
    the lock after the owner exits, which would report a dead owner as alive.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                with lock_path.open(encoding="utf-8") as f:
                    holder = f.read().strip()
            except OSError:
                pass
            os.close(fd)
            if holder:
                raise RuntimeError(
                    f"Another sysforge {label} {noun} is running "
                    f"(pid {holder}); refuse to start a second one."
                ) from None
            raise RuntimeError(
                f"sysforge {label} {noun} lock at {lock_path} is held; "
                f"refuse to start a second concurrent {noun}."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
