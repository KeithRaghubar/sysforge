# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
local_repo.py — the sandbox's local pacman repo (3.1.0-F10).

A sandboxed build resolves its dependencies inside a container whose
``pacman.conf`` knows only the stock repos, so everything sysforge built for
the host — and is not ``-I``-injected — came back as the repo version: on
2026-09-11 a mesa linked the repo ``llvm-libs 22.1.8-2`` while the host ran a
PGO build with the same pkgver, and the desktop did not come up. This module
publishes the host's *installed* source-built packages into
``<state_dir>/local-repo`` as the repo ``[sysforge-local]``, which the chroot
lists first and mounts read-only.

Invariants: the repo is mounted read-only, by arch-nspawn itself from the
``file://`` Server in the managed block (sysforge adds no mount, and never a
writable one); it lives outside every PKGBUILD directory; only installed
versions are published, so a built-but-blocked package never reaches a later
container; and the repo mirrors the installed set, so it is bounded without a
retention policy. Whenever the block is present the host directory exists and
holds a valid db, because a missing bind source stops every container on the
chroot.

Public API:
    REPO_NAME
    repo_dir(state_dir) -> Path
    db_path(state_dir) -> Path
    SyncResult(added, removed, missing)
    sync(state_dir, pkgdest) -> SyncResult      (raises SandboxUnavailable)
    ensure_chroot_section(policy, state_dir)    (raises SandboxUnavailable)
    remove_chroot_section(policy)               (raises SandboxUnavailable)
    section_text(state_dir) -> str
    reset_session()
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives import build_sandbox as _sandbox
from sysforge.primitives import pacman, run
from sysforge.primitives.atomic_write import replace_file
from sysforge.primitives.build_sandbox import SandboxUnavailable

_log = log.get_logger("REPO")

REPO_NAME = "sysforge-local"
_PKGFILE_RE = re.compile(r"^(?P<name>.+)-(?P<ver>[^-]+-[^-]+)-[^-]+\.pkg\.tar(?:\.\w+)?$")


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


_synced: SyncResult | None = None


def reset_session() -> None:
    global _synced
    _synced = None


def repo_dir(state_dir) -> Path:
    return Path(state_dir) / "local-repo"


def db_path(state_dir) -> Path:
    return repo_dir(state_dir) / f"{REPO_NAME}.db.tar.gz"


def _split_pkgfile(fname: str) -> tuple[str, str]:
    m = _PKGFILE_RE.match(fname)
    if not m:
        raise ValueError(f"not a package filename: {fname}")
    return m.group("name"), m.group("ver")


def _read_db(path: Path) -> dict[str, tuple[str, str]]:
    """{name: (version, filename)} straight from the repo db — the one truth."""
    if not path.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    with tarfile.open(path) as t:
        for member in t.getmembers():
            if not member.name.endswith("/desc"):
                continue
            fields, key = {}, None
            for line in t.extractfile(member).read().decode().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key:
                    fields.setdefault(key, line)
            out[fields["NAME"]] = (fields["VERSION"], fields["FILENAME"])
    return out


def _same_file(a: Path, b: Path) -> bool:
    """The same build: one inode (a hard link), or a copy2 copy — same size and
    archive mtime. A rebuild at the same version is a new inode with a new mtime
    under the same filename, so a filename comparison alone misses it."""
    try:
        if Path(a).samefile(b):
            return True
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_size, sa.st_mtime_ns) == (sb.st_size, sb.st_mtime_ns)


def _link_in(src: Path, dest_dir: Path) -> Path:
    dest = dest_dir / src.name
    if dest.exists():
        if _same_file(src, dest):
            return dest
        dest.unlink()  # a same-version rebuild: replace the stale bytes
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)  # PKGDEST on another filesystem
    return dest


def sync(state_dir, pkgdest) -> SyncResult:
    """Make ``[sysforge-local]`` hold exactly the installed source-built set.

    Once per run. A pruned installed artifact warns and continues (the 3.1.0-F9
    rule: one dep, named); a tooling or filesystem failure refuses, because the
    alternative is building against stock — the failure this exists to stop.
    """
    global _synced
    if _synced is not None:
        return SyncResult()
    result = SyncResult()
    rdir = repo_dir(state_dir)
    db = db_path(state_dir)
    try:
        rdir.mkdir(parents=True, exist_ok=True)
        current = _read_db(db)
    except Exception as exc:
        raise SandboxUnavailable(
            f"refusing to sandbox: cannot read the local repo at {rdir} ({exc}), so the "
            f"container would resolve source-built deps from the stock repos") from exc

    desired: dict[str, Path] = {}
    kept: set[str] = set()
    for name in sorted(_sandbox.source_built_packages(state_dir)):
        art = _sandbox.installed_artifact(name, pkgdest) if pkgdest else None
        if art is None:
            version = pacman.get_installed_version(name)
            if not version:
                continue
            published = current.get(name)
            if published and published[0] == version and (rdir / published[1]).exists():
                # Pruned from PKGDEST, but the repo still holds the build the
                # host runs — possibly its last copy. Keep it.
                kept.add(name)
                continue
            result.missing.append(name)
            _log.warn(f"local repo: no built artifact for installed {name} in "
                      f"{pkgdest} — the container will use the repo version")
            continue
        desired[name] = art

    to_add = [n for n, art in desired.items()
              if current.get(n, (None, None))[1] != art.name
              or not _same_file(art, rdir / art.name)]
    to_remove = sorted(set(current) - set(desired) - kept)
    try:
        if to_add:
            files = [str(_link_in(desired[n], rdir)) for n in to_add]
            # No -R: it deletes <dir>/<old filename> after adding, which for a
            # same-version rebuild is the file just linked in. Superseded files
            # with a different name are deleted below instead.
            run.run_or_raise(["repo-add", "-q", str(db), *files], tag="REPO")
            for n in to_add:
                old = current.get(n, (None, None))[1]
                if old and old != desired[n].name:
                    (rdir / old).unlink(missing_ok=True)
        if to_remove:
            run.run_or_raise(["repo-remove", "-q", str(db), *to_remove], tag="REPO")
            for n in to_remove:
                (rdir / current[n][1]).unlink(missing_ok=True)
    except Exception as exc:
        raise SandboxUnavailable(
            f"refusing to sandbox: repo-add/repo-remove failed on {db} ({exc}), so the "
            f"container would resolve source-built deps from the stock repos") from exc
    result.added, result.removed = sorted(to_add), to_remove
    if to_add or to_remove:
        _log.info(f"local repo: +{len(to_add)} -{len(to_remove)} "
                  f"({len(desired)} package(s) published)")
    _synced = result
    return result


# ---------------------------------------------------------------------------
# The chroot's pacman.conf
# ---------------------------------------------------------------------------
#
# arch-nspawn reads the container's pacman.conf and bind-mounts every
# ``file://`` Server from the HOST — read-only for every entry after pacman's
# CacheDir. So the block below is both what lists the repo and what mounts it;
# no ``-D`` is needed, and the Server must name a directory that exists on the
# host whenever the block is present, or no container on this chroot starts.

_BEGIN = "# BEGIN sysforge-local (managed by sysforge; see [security] sandbox_local_repo)"
_END = "# END sysforge-local"
# The block plus the one blank separator line _with_section puts after it, so
# strip-then-insert is idempotent and removal restores the original text.
_BLOCK_RE = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?\n?", re.DOTALL)


def section_text(state_dir) -> str:
    """The managed ``[sysforge-local]`` block: one Server, the host repo dir."""
    return (
        f"{_BEGIN}\n[{REPO_NAME}]\nSigLevel = Never\n"
        f"Server = file://{repo_dir(state_dir)}\n{_END}\n"
    )


def _with_section(conf: str, block: str) -> str:
    """*conf* with exactly one managed block, placed before the first repo."""
    conf = _BLOCK_RE.sub("", conf)
    # Before the first repo section: pacman takes a name from the first repo
    # carrying it, so first place is what wins the identical-pkgver tie.
    m = re.search(r"^\[(?!options\])[^\]]+\]", conf, re.MULTILINE)
    if m is None:
        return conf.rstrip("\n") + "\n\n" + block
    return conf[:m.start()] + block + "\n" + conf[m.start():]


def _empty_db_bytes() -> bytes:
    """An empty sync database: a gzip tar with no entries (what ``repo-add``
    writes when no packages remain), which pacman syncs as an empty repo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz"):
        pass
    return buf.getvalue()


def _seed_repo(state_dir) -> None:
    """Make the host repo dir a valid (possibly empty) repo.

    Runs before the block is written: the block makes arch-nspawn bind this
    directory, and a missing bind source stops every container on the chroot.
    """
    rdir = repo_dir(state_dir)
    rdir.mkdir(parents=True, exist_ok=True)
    db = db_path(state_dir)
    if not db.exists():
        db.write_bytes(_empty_db_bytes())
    link = rdir / f"{REPO_NAME}.db"  # the name pacman fetches; repo-add keeps it
    if not link.exists():
        link.unlink(missing_ok=True)  # a dangling link
        link.symlink_to(db.name)


def ensure_chroot_section(policy, state_dir) -> None:
    """Idempotently list ``[sysforge-local]`` first in the chroot root's pacman.conf.

    The host repo is seeded first, so the directory the block names always
    exists. These are the only writes this feature makes to the root copy.
    """
    conf_path = policy.chroot_dir / "root" / "etc" / "pacman.conf"
    try:
        _seed_repo(state_dir)
        conf = conf_path.read_text()
        wanted = _with_section(conf, section_text(state_dir))
        if wanted != conf:
            # The root's own config: replaced atomically, mode and owner kept,
            # escalating only when the direct write is refused.
            replace_file(conf_path, wanted, tag="REPO")
            _log.info(f"local repo: listed [{REPO_NAME}] first in {conf_path}")
    except Exception as exc:
        raise SandboxUnavailable(
            f"refusing to sandbox: could not list [{REPO_NAME}] in {conf_path} ({exc})"
        ) from exc


def remove_chroot_section(policy) -> None:
    """Drop the managed block when the local repo is turned off.

    Left in place, the block would keep arch-nspawn mounting a repo that is no
    longer synced, so containers would resolve against stale host builds.
    """
    conf_path = policy.chroot_dir / "root" / "etc" / "pacman.conf"
    try:
        if not conf_path.exists():
            return
        conf = conf_path.read_text()
        stripped = _BLOCK_RE.sub("", conf)
        if stripped != conf:
            replace_file(conf_path, stripped, tag="REPO")
            _log.info(f"local repo: removed [{REPO_NAME}] from {conf_path}")
    except Exception as exc:
        raise SandboxUnavailable(
            f"refusing to sandbox: could not remove [{REPO_NAME}] from {conf_path} "
            f"({exc}), so containers would keep mounting a stale local repo"
        ) from exc
