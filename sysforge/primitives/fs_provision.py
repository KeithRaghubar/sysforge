# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
fs_provision.py — one home for sysforge runtime-directory provisioning.

sysforge writes into a handful of FHS-rooted directories that the
unprivileged build user does not own out of the box:

    /var/lib/sysforge          — state (build_state.toml, logs, sentinels)
    /var/cache/sysforge        — regenerable PGO profdata store

Historically three independent code paths provisioned these and disagreed on
ownership (a world-writable ``0777`` tmpfiles hack, a ``root:sysforge 02775``
bootstrap path, and a runtime ``sudo mkdir`` fallback that chowned only the
leaf to the invoking user's *login* group). The mismatch left dirs that the
user could populate but not remove — e.g. ``rmtree`` of ``/var/cache/sysforge/
llvm-pgo`` failing with ``EACCES`` on the final ``rmdir`` because the parent was
``root:root``.

This primitive is the single home for that logic. :func:`ensure_writable_dir`
creates the directory tree, owns it ``root:sysforge`` with the setgid mode
``2775`` (so freshly-created leaves inherit the group), and — when the user is
not yet in the ``sysforge`` group — both adds them durably (``usermod -aG``,
takes effect next login) and repairs the on-disk group/mode this run (``chgrp``
/ ``chmod``) so the directory is usable *immediately*. It is fail-safe: when
``sudo`` is unavailable it raises :class:`FsProvisionError` so callers can fall
back to a user-writable location (e.g. the XDG state dir).

This primitive does I/O and logs (via the ``FSPROV`` logger); it never imports
the pipeline layer.
"""
import getpass
import os
import shutil
import subprocess
from pathlib import Path

from sysforge import log

_log = log.get_logger("FSPROV")

# The group that owns sysforge's writable runtime directories. Members can
# read/write state and the PGO cache across runs (and across users on a shared
# host). Created by the shipped sysusers.d at install time, or on demand here.
SYSFORGE_GROUP = "sysforge"

# setgid (leading 2) + rwxrwxr-x: group-writable, and the setgid bit makes every
# subdirectory created underneath inherit SYSFORGE_GROUP automatically.
SYSFORGE_DIR_MODE = 0o2775


class FsProvisionError(Exception):
    """Raised when a directory cannot be provisioned writable (e.g. sudo is
    unavailable or a privileged step failed). Callers may catch this to fall
    back to a user-writable location."""


def build_user() -> str:
    """Return the unprivileged user the build runs as.

    ``SUDO_USER`` wins when sysforge was launched under ``sudo`` (so we chown to
    the human who invoked it, not ``root``); otherwise the current login.
    """
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()


def _run_priv(argv: list[str]) -> None:
    """Run a privileged command via sudo, translating failure into
    FsProvisionError so callers get one exception type to catch."""
    try:
        subprocess.run(["sudo", *argv], check=True)
    except FileNotFoundError as e:  # sudo not installed
        raise FsProvisionError(f"sudo not available: {e}") from e
    except subprocess.CalledProcessError as e:
        raise FsProvisionError(
            f"privileged step failed ({' '.join(argv)}): exit {e.returncode}"
        ) from e


def _group_exists(group: str) -> bool:
    try:
        import grp

        grp.getgrnam(group)
        return True
    except KeyError:
        return False


def _user_in_group(user: str, group: str) -> bool:
    try:
        import grp

        return user in grp.getgrnam(group).gr_mem
    except KeyError:
        return False


def ensure_writable_dir(
    path: Path | str,
    *,
    group: str = SYSFORGE_GROUP,
    mode: int = SYSFORGE_DIR_MODE,
    dry_run: bool = False,
    allow_sudo: bool = True,
) -> Path:
    """Ensure ``path`` exists and is writable by the build user.

    Fast path: a plain ``mkdir`` that lands a writable directory (already
    provisioned, or a user-owned location like an XDG dir) returns immediately
    and never touches sudo.

    Slow path (root-owned ancestor / not writable): provision under ``group``
    with setgid ``mode`` via sudo —

      1. create the group if missing (``groupadd -f``);
      2. add the build user to it durably (``usermod -aG`` — takes effect on
         next login) when not already a member;
      3. create/repair the tree: ``install -d -m <mode> -g <group>`` for the
         target, then ``chgrp``/``chmod`` to heal an already-existing dir whose
         ownership predates this policy.

    Returns the resolved ``Path``. Raises :class:`FsProvisionError` when the dir
    is not writable and sudo cannot fix it (so callers may XDG-fall-back).
    """
    path = Path(path)
    if dry_run:
        return path

    # Fast path — direct create, no elevation.
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.access(path, os.W_OK):
            return path
    except PermissionError:
        pass

    if not allow_sudo:
        raise FsProvisionError(
            f"{path} is not writable and sudo provisioning is disabled"
        )

    user = build_user()
    _log.info(
        f"Provisioning {path} as root:{group} (mode {mode:#o}) via sudo "
        f"(build user: {user})"
    )

    if not _group_exists(group):
        _run_priv(["groupadd", "-f", group])
    if user != "root" and not _user_in_group(user, group):
        _run_priv(["usermod", "-aG", group, user])
        _log.warn(
            f"Added {user} to group {group}; this takes effect on next login. "
            f"This run's writes are enabled via a direct chmod/chgrp repair."
        )

    # Create the target (idempotent) with the right group+mode in one step, then
    # repair ownership/mode in case it (or an ancestor) already existed wrong.
    _run_priv(["install", "-d", "-m", f"{mode:o}", "-g", group, str(path)])
    _run_priv(["chgrp", group, str(path)])
    _run_priv(["chmod", f"{mode:o}", str(path)])
    return path


def empty_dir_contents(path: Path | str, *, dry_run: bool = False) -> None:
    """Remove the *contents* of ``path`` while leaving the directory node intact.

    Removing the node itself (``rmtree(path)`` / ``rmdir``) requires write
    permission on the **parent**, which fails when the parent is root-owned even
    though every child is user-written. Emptying the contents needs no parent
    write and no sudo, and a subsequent :func:`ensure_writable_dir` re-verifies
    the node. Used for the PGO ``pgo_store`` "start fresh" purge.
    """
    path = Path(path)
    if dry_run or not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
