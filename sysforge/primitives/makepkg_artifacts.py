# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_artifacts.py — built-package file discovery and filename parsing

Pure helpers over the ``.pkg.tar*`` artifacts a makepkg build leaves in the
PKGBUILD directory: locate them, and parse a built filename back into its
resolved ``(epoch, pkgver, pkgrel)``.  No I/O beyond a directory glob and no
logging (``find_artifacts``'s newest-wins mode calls ``vercmp``, which is the
module's one subprocess-capable dependency) — leaf utilities consumed by the build orchestrator
(``makepkg_wrapper``) and the post-build version readers (``build_core``,
``pacman``, ``vcs_pkgver``).
"""
import contextlib
import re
from functools import cmp_to_key
from pathlib import Path


def _find_built_packages(build_dir: Path) -> list:
    """Return .pkg.tar* files in build_dir (excludes .sig files).

    Matches both compressed (.pkg.tar.zst, .pkg.tar.xz) and uncompressed
    (.pkg.tar) packages — the latter is produced when PKGEXT='.pkg.tar'.
    """
    return [p for p in Path(build_dir).glob("*.pkg.tar*")
            if not p.name.endswith(".sig")]


# Trailing compression suffix is optional: PKGEXT='.pkg.tar' produces
# uncompressed package files, and `makepkg --packagelist` always prints
# names that match the configured PKGEXT.
_PKG_FILENAME_EXT = re.compile(r"\.pkg\.tar(?:\.[^.]+)?$")


def _parse_built_pkg_filename(pkgname: str, filename: str) -> tuple[str, str, str] | None:
    """
    Parse a built Arch package filename into ``(epoch, pkgver, pkgrel)``.

    Expected form: ``<pkgname>-[epoch:]<pkgver>-<pkgrel>-<arch>.pkg.tar[.<ext>]``.
    Returns None if the filename does not match this layout for ``pkgname``.

    This is the canonical post-build source of truth for a package's version:
    the filename always carries the fully resolved values, whereas the static
    PKGBUILD parser intentionally leaves shell parameter-expansion forms like
    ``${_ver/[a-z]/.${_ver//[0-9.]/}}`` untouched. Anchoring on the known
    ``pkgname`` is required because pkgnames may themselves contain hyphens
    (e.g. ``openssl-1.1``).
    """
    m = _PKG_FILENAME_EXT.search(filename)
    if not m:
        return None
    stem = filename[:m.start()]
    prefix = pkgname + "-"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix):]
    # A valid tail is exactly ``[epoch:]pkgver-pkgrel-arch`` — three
    # hyphen-delimited fields, because PKGBUILD(5) forbids hyphens in pkgver,
    # pkgrel and arch. Anything else means ``pkgname`` is only a hyphen-prefix
    # of a *different* package (e.g. ``linux`` vs ``linux-custom``), which must
    # not match — a looser rsplit would let pkgver absorb the extra hyphens.
    parts = rest.split("-")
    if len(parts) != 3:
        return None
    ver_part, pkgrel, _arch = parts
    epoch = "0"
    if ":" in ver_part:
        epoch, _, ver_part = ver_part.partition(":")
    if not ver_part or not pkgrel:
        return None
    return (epoch, ver_part, pkgrel)


def select_built_version(pkgname: str, paths) -> tuple[str, str, str] | None:
    """Pick ``(epoch, pkgver, pkgrel)`` for ``pkgname`` from built artifacts.

    ``paths`` is an iterable of candidate ``.pkg.tar*`` paths. Only those whose
    filename parses as ``pkgname`` are considered, and the **most recently
    modified** one wins — never merely the first the caller happened to yield.

    PKGDEST is commonly a shared, long-lived package archive holding every
    historical build of every package (``/home/packages``-style), so a glob
    over it returns many versions of ``pkgname`` in arbitrary order. Taking the
    first match there records a years-old version as "last built", which makes
    the next vercmp report the PKGBUILD has moved and re-triggers the build
    forever. mtime answers the question actually being asked: which artifact
    did the build that just finished produce (3.1.0-B1).
    """
    newest, newest_mtime = None, None
    for p in paths:
        path = Path(p)
        parsed = _parse_built_pkg_filename(pkgname, path.name)
        if parsed is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest, newest_mtime = parsed, mtime
    return newest


def find_artifacts(
    search_dir,
    pkgnames: list,
    pkgbuild_ver: str | None = None,
    installed_ver: str | None = None,
    exact_ver: str | None = None,
) -> list:
    """Locate already-built ``.pkg.tar*`` artifacts for *pkgnames*.

    Three selection modes, because PKGDEST is commonly a long-lived archive
    holding every historical build of every package (3.1.0-B1), so "the file
    for this package" is genuinely ambiguous in there:

    ``exact_ver``
        Return only artifacts whose filename version equals it exactly, and
        skip the other two stages entirely. Used by the sandbox's dependency
        injection (3.1.0-F9), which must hand the container the version the
        *host actually runs* — selecting the newest instead would hand it a
        version the host does not have, recreating the skew the injection
        exists to remove. No ``vercmp``: an exact string match is the whole
        question, and it holds for VCS packages too, whose installed version
        is by definition the one their artifact filename carries.

    ``pkgbuild_ver`` (strict stage)
        Glob ``{pkgname}-{pkgbuild_ver}-*``. Matches non-VCS packages, where
        the static PKGBUILD parse equals the filename version.

    newest (fallback stage)
        Filename parse + ``vercmp``, newest wins. Required for VCS packages,
        whose ``pkgver()`` bumps at build time (PKGBUILD ``pkgver=0.1.0`` →
        artifact ``0.1.0.r45.g1234567``) so ``pkgbuild_ver`` never matches.
        With ``installed_ver`` set, only artifacts strictly newer than it are
        considered — ``--install-only``\'s guard against redundant reinstalls
        and downgrades.
    """
    from sysforge.primitives.version import vercmp

    if not search_dir or not Path(search_dir).is_dir():
        return []

    found: list = []
    for pkgname in pkgnames:
        if exact_ver is not None:
            for path in sorted(Path(search_dir).glob(f"{pkgname}-*.pkg.tar*")):
                if path.name.endswith(".sig"):
                    continue
                parsed = _parse_built_pkg_filename(pkgname, path.name)
                if parsed is None:
                    continue
                if _version_string(parsed) == exact_ver:
                    found.append(path)
                    break
            continue

        if pkgbuild_ver:
            strict = [
                p for p in Path(search_dir).glob(
                    f"{pkgname}-{pkgbuild_ver}-*.pkg.tar*"
                )
                if not p.name.endswith(".sig")
            ]
            if strict:
                found.extend(strict)
                continue

        candidates: list = []
        for p in Path(search_dir).glob(f"{pkgname}-*-*-*.pkg.tar*"):
            if p.name.endswith(".sig"):
                continue
            parsed = _parse_built_pkg_filename(pkgname, p.name)
            if parsed is None:
                continue
            ver_string = _version_string(parsed)
            if installed_ver is not None:
                try:
                    if vercmp(ver_string, installed_ver) <= 0:
                        continue
                except RuntimeError:
                    continue
            candidates.append((ver_string, p))

        if not candidates:
            continue

        with contextlib.suppress(RuntimeError):
            candidates.sort(key=cmp_to_key(lambda a, b: vercmp(a[0], b[0])))
        found.append(candidates[-1][1])

    return found


def _version_string(parsed: tuple) -> str:
    """Render a parsed ``(epoch, pkgver, pkgrel)`` the way pacman prints it.

    Epoch ``0`` is omitted, matching ``pacman -Q`` output and the artifact
    filenames themselves — both write ``1.2.3-1``, never ``0:1.2.3-1``.
    """
    epoch, ver, rel = parsed
    return f"{epoch}:{ver}-{rel}" if epoch != "0" else f"{ver}-{rel}"
