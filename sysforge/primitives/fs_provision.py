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
not yet in the ``sysforge`` group — adds them durably (``usermod -aG``, takes
effect next login), repairs the on-disk group/mode (``chgrp`` / ``chmod``) and,
until the group reaches this session, grants them a per-user ACL so the
directory is usable *immediately* (3.5.0-B134). It is fail-safe: when
``sudo`` is unavailable it raises :class:`FsProvisionError` so callers can fall
back to a user-writable location (e.g. the XDG state dir).

This primitive does I/O and logs (via the ``FSPROV`` logger); it never imports
the pipeline layer.
"""
import contextlib
import getpass
import os
import shutil
import stat
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives.resource_guard import lift_for_child

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
        subprocess.run(privileged_argv(argv), check=True, preexec_fn=lift_for_child)  # noqa: TID251 — privileged; CalledProcessError translated to FsProvisionError
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


def _process_holds_group(group: str) -> bool:
    """Whether this process carries ``group`` now, not just in ``/etc/group``.

    ``usermod -aG`` updates the group database at once, but a running login
    session keeps the groups it started with until the next login.
    """
    try:
        import grp

        return grp.getgrnam(group).gr_gid in os.getgroups()
    except KeyError:
        return False


def _heal_owned_dir_group(path: Path, group: str, mode: int) -> None:
    """Best-effort, sudo-free repair of the fast-path group/setgid gap (2.2.0-B6).

    A directory first created by the unprivileged build user lands
    ``user:user 0755`` with no setgid bit, so children never inherit
    ``SYSFORGE_GROUP`` and a second writer at a different uid (the root libalpm
    hooks, a later ``sudo`` rebuild) can't write the tree. When we *own* the node
    and *hold* ``group`` among our process groups, a plain ``chgrp``/``chmod``
    needs no elevation — so heal it here and let the sudo slow path handle only
    root-owned ancestors. Any failure is swallowed: the dir is already writable,
    the group/setgid fix is a bonus.
    """
    try:
        import grp

        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return  # group not created yet — nothing to heal to
    try:
        st = path.stat()
    except OSError:
        return
    if st.st_uid != os.geteuid():
        return  # not the owner — chgrp/chmod would need sudo (slow path's job)
    if gid not in os.getgroups():
        return  # can't chgrp to a group we don't hold without sudo
    try:
        if st.st_gid != gid:
            os.chown(path, -1, gid)
        if (st.st_mode & 0o7777) != mode:
            path.chmod(mode)
    except OSError:
        pass


def _refuse_planted_symlink(path: Path) -> None:
    """Refuse to escalate over a symlink another user could have planted.

    ``install -d``, ``chgrp`` and ``chmod`` all follow symlinks, so a group
    member who swapped a provisioned directory under the shared, group-writable
    state tree for a symlink would get another user's sudo applied to its
    target (3.5.0-B77). The path itself must not be a symlink, nor may any
    ancestor that sits in a group- or world-writable directory; a system
    symlink such as ``/home`` (in ``/``, writable by root only) is fine.
    """
    for p in (path, *path.parents):
        try:
            if not p.is_symlink():
                continue
            parent_mode = p.parent.lstat().st_mode
        except OSError:
            continue
        if p == path or parent_mode & 0o022:
            raise FsProvisionError(
                f"refusing to provision {path} with sudo: {p} is a symlink "
                f"in a directory others can write to")


def check_private_dir(path: Path | str, *, check_mode: bool = True) -> None:
    """Refuse *path* unless it is a real directory this user owns that no one
    else can write to.

    For build-time prefixes in a shared directory such as ``/var/tmp`` whose
    contents later run (``PATH``, ``LD_LIBRARY_PATH``, ``CC``): another local
    user who created the name first, or planted a symlink there, would have
    their files run in the build (3.5.0-B54). The sticky bit on ``/var/tmp``
    keeps anyone else from replacing a directory this user owns, so ownership
    plus mode is enough. ``check_mode=False`` skips the mode test, for a
    directory about to be deleted, whose contents will never run. Raises
    :class:`FsProvisionError`; a missing *path* passes (nothing there to run).
    """
    path = Path(path)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise FsProvisionError(f"refusing {path}: it is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise FsProvisionError(f"refusing {path}: it is not a directory")
    if st.st_uid != os.geteuid():
        raise FsProvisionError(
            f"refusing {path}: owned by uid {st.st_uid}, not this user; another "
            "user may have created it. Inspect it, remove it, and run again")
    if check_mode and st.st_mode & 0o022:
        raise FsProvisionError(
            f"refusing {path}: others can write to it "
            f"(mode {stat.S_IMODE(st.st_mode):04o}); remove it and run again")


def ensure_private_dir(path: Path | str) -> Path:
    """Create *path* mode 0700 (parents as usual), or verify an existing one
    with :func:`check_private_dir`. Returns *path*."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    check_private_dir(path)
    return path


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
    and never touches sudo. If we own the landed dir and hold ``group``, its
    group + setgid ``mode`` are best-effort self-healed sudo-free
    (:func:`_heal_owned_dir_group`, 2.2.0-B6) so children inherit the group.

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
            _heal_owned_dir_group(path, group, mode)
            return path
    except PermissionError:
        pass

    if not allow_sudo:
        raise FsProvisionError(
            f"{path} is not writable and sudo provisioning is disabled"
        )

    _refuse_planted_symlink(path)

    user = build_user()
    _log.info(
        f"Provisioning {path} as root:{group} (mode {mode:#o}) via sudo "
        f"(build user: {user})"
    )

    if not _group_exists(group):
        _run_priv(["groupadd", "-f", group])
    if user != "root" and not _user_in_group(user, group):
        _run_priv(["usermod", "-aG", group, user])
        _log.warn(f"Added {user} to group {group}; this takes effect on next login.")

    # Create the target (idempotent) with the right group+mode in one step, then
    # repair ownership/mode in case it (or an ancestor) already existed wrong.
    _run_priv(["install", "-d", "-m", f"{mode:o}", "-g", group, str(path)])
    _run_priv(["chgrp", group, str(path)])
    _run_priv(["chmod", f"{mode:o}", str(path)])

    # The group only reaches this session at its next login, so until then a
    # group-writable tree is still closed to it (3.5.0-B134). A per-user ACL,
    # inherited (d:) by what is created below, opens it now; once the group
    # applies, it changes nothing.
    if user != "root" and not _process_holds_group(group):
        _run_priv(["setfacl", "-R", "-m", f"u:{user}:rwX,d:u:{user}:rwX", str(path)])
        _log.info(f"Granted {user} access to {path} until the {group} group applies.")
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
