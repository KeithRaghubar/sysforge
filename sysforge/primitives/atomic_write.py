# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
atomic_write.py — the one home for replacing an existing config file (2.6.1-F21).

Every writer that rewrites a user- or root-owned config in place goes through
:func:`replace_file`, which gives the same guarantees on both privilege paths:

- **Atomic.** The new content is staged in a temp file *in the destination's
  directory* (so ``rename(2)`` stays on one filesystem) and renamed over the
  destination. A kill or power loss mid-write leaves the old file or the new
  one, never a truncated one — the failure mode the hand-rolled
  ``write_text``/``sudo cp`` writers all had.
- **Symlink-preserving.** The destination is resolved first and the *target*
  is replaced, so a dotfile-manager link stays a link (``os.replace`` on the
  link path would clobber it with a regular file).
- **Mode/owner-preserving.** The replacement gets the existing file's mode
  and owner; a file that does not exist yet gets ``default_mode`` (and, when
  escalated, ``root:root``).

The direct path is used when the process can write the destination directory;
otherwise the content is staged to a private temp file and installed through
the privilege seam (``install -m/-o/-g`` into the destination directory, then
``mv -fT`` — a same-directory rename). Failures raise :class:`OSError`, the
contract the converted writers already had.

Out of scope: creating files with a class-defined mode (``artifacts.write_live``,
``pacman_hooks``' shipped artifacts) — there is no destination mode to honour,
and those already go through ``install -Dm``.

Public API:
    replace_file(path, text, *, tag, default_mode=0o644) -> Path
"""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from sysforge import log
from sysforge.primitives.privilege import run_privileged

_log = log.get_logger("WRITE")

_TMP_SUFFIX = ".sysforge-tmp"


def _existing_attrs(target: Path, default_mode: int) -> tuple[int, int | None, int | None]:
    """``(mode, uid, gid)`` to give the replacement; uid/gid None = no change."""
    try:
        st = target.stat()
    except FileNotFoundError:
        return default_mode, None, None
    return stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


def _fsync_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace_direct(target: Path, data: bytes, mode: int, uid, gid) -> None:
    """Stage beside *target*, match mode/owner, fsync, rename over it."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.",
                                    suffix=_TMP_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(mode)
        if uid is not None and (uid, gid) != (os.geteuid(), os.getegid()):
            os.chown(tmp, uid, gid)  # PermissionError → caller escalates
        tmp.replace(target)  # rename(2): same directory, atomic
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(target.parent)


def _replace_privileged(target: Path, data: bytes, mode: int, uid, gid, tag: str) -> None:
    """Install a staged copy into *target*'s directory as root, then rename."""
    fd, src_name = tempfile.mkstemp(prefix="sysforge-write-")
    src = Path(src_name)
    staged = target.parent / f".{target.name}{_TMP_SUFFIX}"
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        src.chmod(0o600)
        owner = ["-o", str(uid if uid is not None else 0),
                 "-g", str(gid if gid is not None else 0)]
        _log.info(f"  Writing (sudo): {target}")
        try:
            run_privileged(["install", "-m", f"{mode:o}", *owner, str(src), str(staged)],
                           tag=tag, operation="install")
            run_privileged(["mv", "-fT", str(staged), str(target)], tag=tag, operation="mv")
        except RuntimeError as e:
            with contextlib.suppress(RuntimeError):
                run_privileged(["rm", "-f", str(staged)], tag=tag, operation="rm")
            raise OSError(f"{e} — {target} unchanged") from e
    finally:
        src.unlink(missing_ok=True)


def replace_file(path, text: str, *, tag: str, default_mode: int = 0o644) -> Path:
    """Atomically replace *path* with *text*; return the file actually written.

    Follows symlinks (the link's target is replaced, the link kept), keeps the
    existing mode and owner, creates parent directories when it can, and
    escalates through the privilege seam only when the direct path is refused.
    """
    target = Path(os.path.realpath(Path(path).expanduser()))
    data = text.encode("utf-8")
    mode, uid, gid = _existing_attrs(target, default_mode)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_direct(target, data, mode, uid, gid)
    except PermissionError:
        _replace_privileged(target, data, mode, uid, gid, tag)
    return target
