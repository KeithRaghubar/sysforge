# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stock_pkg.py — the stock repo build of a package, unprivileged (3.4.0-F6).

``bench`` compares an installed sysforge build against the repo build of the
same name, and it is a read-only verb: it must never escalate. So this does not
use ``pacman -Sw`` (root-only, writes the system cache). It asks
``pacman -Sp --print-format`` for the location and the sync DB's own SHA256
(``%h``), uses a cached archive when one exists, otherwise downloads as the
user into a temp dir, and checks the checksum before anything is extracted.
``build_sandbox.fetch_repo_packages`` is the privileged sibling for builds that
install into a container.

It gets through where pacman would (3.4.0-B19): ``-Sp -dd`` so an unrelated
dependency problem on the host can't block two archive lookups, the repo's
other ``Server`` mirrors when the first one fails, and a fresh download when a
cached archive fails its checksum. Only what the compiler needs is unpacked,
and a downloaded archive is deleted once it is (3.4.0-B21): ``$TMPDIR`` is
usually a tmpfs, so the full ~440 MB ``usr/`` tree would sit in RAM.

The checksum is only as trustworthy as the sync DB it came from. The stock
repos ship unsigned databases (``SigLevel = DatabaseOptional``), so that is
the mirror's TLS, the same trust a system ``pacman -Sy`` has.

A stale sync DB names a build every mirror has already dropped, so each
download 404s (``StockDownloadFailed``). ``private_sync`` refreshes a copy of
the sync DBs under the caller's temp dir with ``fakeroot`` (3.5.0-F1), and
``resolve(dbpath=...)`` reads it. The system DB is never touched: a bare
``pacman -Sy`` needs root and would leave the host partially upgraded.
"""
from __future__ import annotations

import fnmatch
import hashlib
import http.client
import shutil
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives import pacman, run
from sysforge.primitives.local_repo import REPO_NAME

_FORMAT = "%n|%v|%r|%f|%l|%h"
_TIMEOUT_S = 60
_DEFAULT_DBPATH = Path("/var/lib/pacman")
# What a stock clang needs to compile -O2 -c: the driver and its symlinks, the
# two shared libraries it loads, and the resource dir (builtin headers). Globs,
# so a file moving between packages is still found (``check_complete``).
EXTRACT_MEMBERS = ("usr/bin/clang", "usr/bin/clang++", "usr/bin/clang-[0-9]*",
                   "usr/lib/libclang-cpp.so*",
                   "usr/lib/libLLVM*.so*", "usr/lib/clang")


class StockUnavailable(RuntimeError):
    """The stock build could not be obtained; the message says why."""


class StockDownloadFailed(StockUnavailable):
    """No mirror served a verified copy: most often a stale sync DB."""


@dataclass(frozen=True)
class StockPkg:
    name: str
    version: str
    repo: str
    filename: str
    location: str
    sha256: str


def _lookup(names: Sequence[str], dbpath: Path | None) -> tuple[dict[str, StockPkg], str]:
    """``pacman -Sp -dd`` rows for *names*, and pacman's stderr.

    One unknown target fails the whole call with no rows at all.
    """
    argv = ["pacman", "-Sp", "-dd", "--print-format", _FORMAT]
    if dbpath is not None:
        argv += ["--dbpath", str(dbpath)]
    res = run.probe([*argv, *names])
    rows: dict[str, StockPkg] = {}
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        parts = line.strip().split("|")
        if len(parts) == 6:
            rows[parts[0]] = StockPkg(*parts)
    return rows, (res.stderr or "").strip()


def resolve(names: Sequence[str], *, optional: Sequence[str] = (),
            dbpath: Path | None = None) -> list[StockPkg]:
    """The sync-DB build of each of *names*, then of each *optional* name the
    repo carries, in order. Raises ``StockUnavailable``.

    ``-dd`` skips dependency resolution: the bench needs the named archives
    only, and resolving against the installed system lets an unrelated conflict
    or versioned dependency fail the lookup. Only the requested names are kept.
    *optional* names a package a repo split may or may not have produced yet
    (3.5.0-B146); each is looked up on its own, since an unknown target would
    fail the required lookup too. *dbpath* reads a ``private_sync`` copy
    instead of the system sync DB.
    """
    rows, stderr = _lookup(names, dbpath)
    missing = [n for n in names if n not in rows]
    if missing:
        detail = stderr or "not in any sync database"
        raise StockUnavailable(f"no stock repo carries {', '.join(missing)} ({detail})")
    found = list(names)
    for name in optional:
        extra, _ = _lookup([name], dbpath)
        if name in extra:
            rows[name] = extra[name]
            found.append(name)
    local = [n for n in found if rows[n].repo == REPO_NAME]
    if local:
        raise StockUnavailable(
            f"{', '.join(local)} resolves to {REPO_NAME}, not a stock repo")
    return [rows[n] for n in found]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached(pkg: StockPkg) -> Path | None:
    if pkg.location.startswith("file://"):
        path = Path(pkg.location[len("file://"):])
        if path.is_file():
            return path
    for d in pacman.get_pacman_cache_dirs():
        path = d / pkg.filename
        if path.is_file():
            return path
    return None


def _repo_servers(repo: str) -> list[str]:
    """Every ``Server`` pacman would try for *repo*, in order (empty on failure)."""
    res = run.probe(["pacman-conf", "--repo", repo, "Server"])
    if res.returncode != 0:
        return []
    return [line.strip().rstrip("/") for line in res.stdout.splitlines() if line.strip()]


def _urls(pkg: StockPkg) -> list[str]:
    """``%l`` (the first mirror) first, then the repo's other mirrors."""
    urls = [pkg.location]
    for server in _repo_servers(pkg.repo):
        url = f"{server}/{pkg.filename}"
        if url not in urls:
            urls.append(url)
    return urls


def _download(pkg: StockPkg, dest: Path, opener) -> None:
    """Fetch *pkg* into *dest* from the first mirror that serves a verified copy."""
    errors = []
    for url in _urls(pkg):
        try:
            with opener(url, timeout=_TIMEOUT_S) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError/HTTPError and socket timeouts; a response
            # cut short raises IncompleteRead (HTTPException), a location
            # urllib cannot open raises ValueError.
            errors.append(f"{url}: {exc}")
            continue
        if _sha256(dest) == pkg.sha256:
            return
        errors.append(f"{url}: does not match the sync database checksum")
    dest.unlink(missing_ok=True)
    raise StockDownloadFailed(f"could not download {pkg.filename} ({'; '.join(errors)})")


def obtain(pkg: StockPkg, workdir: Path, *, offline: bool,
           opener=urllib.request.urlopen) -> Path:
    """A verified archive of *pkg*: cached in place, else downloaded into *workdir*.

    A cached copy that fails the checksum is passed over (never modified) for
    a fresh download; with *offline* there is nothing to fall back to.
    """
    cached = _cached(pkg)
    if cached is not None:
        if _sha256(cached) == pkg.sha256:
            return cached
        if offline:
            raise StockUnavailable(
                f"{cached} does not match the sync database checksum and "
                "--offline was given; not using it")
    elif offline:
        raise StockUnavailable(
            f"{pkg.filename} is not in the pacman cache and --offline was given")
    path = Path(workdir) / pkg.filename
    _download(pkg, path, opener)
    return path


def _local_db() -> Path:
    """The system's local package DB (``<DBPath>/local``), per ``pacman-conf``."""
    res = run.probe(["pacman-conf", "DBPath"])
    dbpath = res.stdout.strip() if res.returncode == 0 else ""
    return Path(dbpath or _DEFAULT_DBPATH) / "local"


def private_sync(workdir: Path) -> Path:
    """Sync fresh copies of the repo DBs into ``<workdir>/db``, unprivileged.

    ``pacman -Sy`` refuses a non-root user, so it runs under ``fakeroot`` with
    its own ``--dbpath`` and ``--logfile /dev/null``. ``--disable-sandbox``
    because the download sandbox drops root to ``DownloadUser``, which fakeroot
    only pretends to have; there is no privilege to shed. ``-Sp`` still needs a
    local DB, so the system's is symlinked in; nothing here writes to it.
    Returns the dbpath for ``resolve``. Raises ``StockUnavailable``.
    """
    db = Path(workdir) / "db"
    db.mkdir(parents=True, exist_ok=True)
    local = db / "local"
    if not local.is_symlink():
        local.symlink_to(_local_db())
    res = run.probe(["fakeroot", "--", "pacman", "-Sy", "--dbpath", str(db),
                     "--logfile", "/dev/null", "--disable-sandbox"])
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"exit {res.returncode}"
        raise StockUnavailable(f"could not refresh the sync database ({detail})")
    return db


def _matches(name: str, pattern: str) -> bool:
    """bsdtar's pattern rule: the entry itself or any parent directory matches."""
    parts = name.rstrip("/").split("/")
    return any(fnmatch.fnmatchcase("/".join(parts[:i]), pattern)
               for i in range(1, len(parts) + 1))


def _members(archive: Path) -> list[str]:
    """The :data:`EXTRACT_MEMBERS` patterns present in *archive*.

    bsdtar fails on a pattern an archive lacks, and each package carries only
    part of the set (``libLLVM`` is in ``llvm-libs``; from clang 23.1.3-4 the
    resource dir and ``libclang-cpp`` are in ``clang-libs``, the driver in ``clang``).
    """
    res = run.probe(["bsdtar", "-tf", str(archive)])
    if res.returncode != 0:
        raise StockUnavailable(f"could not read {archive}: {(res.stderr or '').strip()}")
    names = res.stdout.splitlines()
    return [pat for pat in EXTRACT_MEMBERS if any(_matches(n, pat) for n in names)]


def extract(archives: Sequence[Path], prefix: Path, *,
            downloaded_in: Path | None = None) -> None:
    """Unpack what the compiler needs from each archive under *prefix*.

    An archive under *downloaded_in* (the caller's temp dir, where ``obtain``
    downloads) is deleted once unpacked; a cached one is never touched. Raises
    ``StockUnavailable``.
    """
    Path(prefix).mkdir(parents=True, exist_ok=True)
    for archive in archives:
        members = _members(archive)
        if members:
            res = run.probe(["bsdtar", "-xf", str(archive), "-C", str(prefix), *members])
            if res.returncode != 0:
                raise StockUnavailable(
                    f"could not extract {archive}: {(res.stderr or '').strip()}")
        if downloaded_in is not None and Path(archive).parent == Path(downloaded_in):
            Path(archive).unlink(missing_ok=True)


# What the extracted prefix must hold, as (glob under the prefix, what it is).
# A library missing here is not an error to the loader: it falls back to the
# host's copy, which is the sysforge build (3.5.0-B146).
_REQUIRED_PARTS = (("usr/lib/libclang-cpp.so*", "libclang-cpp"),
                   ("usr/lib/libLLVM*.so*", "libLLVM"),
                   ("usr/lib/clang/*/include/stddef.h", "builtin headers"))


def check_complete(prefix: Path) -> None:
    """Raise ``StockUnavailable`` naming each part missing from *prefix*.

    The stock packages can move files between them on a repo split (clang
    23.1.3-4 moved the resource dir and ``libclang-cpp`` to ``clang-libs``).
    """
    missing = [what for pattern, what in _REQUIRED_PARTS
               if not any(Path(prefix).glob(pattern))]
    if missing:
        raise StockUnavailable(
            f"the stock packages lack {', '.join(missing)}: the repo may have "
            "split them into a package the bench does not fetch")
