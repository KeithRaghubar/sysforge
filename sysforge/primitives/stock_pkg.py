# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stock_pkg.py — the stock repo build of a package, unprivileged (3.4.0-F6).

``bench`` compares an installed sysforge build against the repo build of the
same name, and it is a read-only verb: it must never escalate. So this does not
use ``pacman -Sw`` (root-only, writes the system cache). It asks
``pacman -Sp --print-format`` for the location and the sync DB's own SHA256
(``%h``; pacman verified the signed DB when it synced), uses a cached archive
when one exists, otherwise downloads as the user into a temp dir, and checks
the checksum before anything is extracted. ``build_sandbox.fetch_repo_packages``
is the privileged sibling for builds that install into a container.
"""
from __future__ import annotations

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


class StockUnavailable(RuntimeError):
    """The stock build could not be obtained; the message says why."""


@dataclass(frozen=True)
class StockPkg:
    name: str
    version: str
    repo: str
    filename: str
    location: str
    sha256: str


def resolve(names: Sequence[str]) -> list[StockPkg]:
    """The sync-DB build of each of *names*, in order. Raises ``StockUnavailable``.

    ``-Sp`` also prints any dependency pacman would pull in; only the requested
    names are kept.
    """
    res = run.probe(["pacman", "-Sp", "--print-format", _FORMAT, *names])
    rows: dict[str, StockPkg] = {}
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        parts = line.strip().split("|")
        if len(parts) == 6:
            rows[parts[0]] = StockPkg(*parts)
    missing = [n for n in names if n not in rows]
    if missing:
        detail = (res.stderr or "").strip() or "not in any sync database"
        raise StockUnavailable(f"no stock repo carries {', '.join(missing)} ({detail})")
    local = [n for n in names if rows[n].repo == REPO_NAME]
    if local:
        raise StockUnavailable(
            f"{', '.join(local)} resolves to {REPO_NAME}, not a stock repo")
    return [rows[n] for n in names]


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


def obtain(pkg: StockPkg, workdir: Path, *, offline: bool,
           opener=urllib.request.urlopen) -> Path:
    """A verified archive of *pkg*: cached in place, else downloaded into *workdir*."""
    path = _cached(pkg)
    if path is None:
        if offline:
            raise StockUnavailable(
                f"{pkg.filename} is not in the pacman cache and --offline was given")
        path = Path(workdir) / pkg.filename
        try:
            with opener(pkg.location, timeout=_TIMEOUT_S) as src, path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError/HTTPError and socket timeouts; a response
            # cut short raises IncompleteRead (HTTPException), a location
            # urllib cannot open raises ValueError.
            raise StockUnavailable(f"could not download {pkg.location}: {exc}") from exc
    if _sha256(path) != pkg.sha256:
        raise StockUnavailable(
            f"{path} does not match the sync database checksum; not using it")
    return path


def extract(archives: Sequence[Path], prefix: Path) -> None:
    """Unpack each archive's ``usr/`` tree under *prefix*. Raises ``StockUnavailable``."""
    Path(prefix).mkdir(parents=True, exist_ok=True)
    for archive in archives:
        res = run.probe(["bsdtar", "-xf", str(archive), "-C", str(prefix), "usr"])
        if res.returncode != 0:
            raise StockUnavailable(
                f"could not extract {archive}: {(res.stderr or '').strip()}")
