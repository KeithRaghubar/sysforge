"""
build_lock.py — advisory cross-process lock for build-bearing scopes.

A thin ``flock``-based context manager used to refuse two concurrent runs
that would clobber a shared on-disk build area (the PGO staging dirs +
``pgo_store`` for the toolchain stage, ``~/builds/<pkgbase>`` for the kernel
stage). The holder PID is written into the lock file so the loser can
surface a useful error.

This is *not* the stage sentinel: the sentinel records an interrupted
boot-critical mutation so the next run prompts for recovery, and lives in
the state dir. The build lock is a transient mutual-exclusion guard held
only for the duration of one run. Both the toolchain and kernel stages
share this one primitive — don't roll a second flock path.
"""
import contextlib
import fcntl
import os
from pathlib import Path


@contextlib.contextmanager
def build_lock(lock_path: Path, *, label: str):
    """Acquire an exclusive advisory lock at ``lock_path`` for the scope.

    Raises RuntimeError immediately (non-blocking) if another process holds
    the lock, naming the holder PID when it can be read. ``label`` is woven
    into the error message (e.g. ``"PGO toolchain"`` / ``"kernel"``).
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                with open(lock_path) as f:
                    holder = f.read().strip()
            except OSError:
                pass
            os.close(fd)
            if holder:
                raise RuntimeError(
                    f"Another sysforge {label} build is running "
                    f"(pid {holder}); refuse to start a second one."
                )
            raise RuntimeError(
                f"sysforge {label} build lock at {lock_path} is held; "
                "refuse to start a second concurrent build."
            )
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
