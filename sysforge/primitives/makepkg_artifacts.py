# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_artifacts.py — built-package file discovery and filename parsing

Pure helpers over the ``.pkg.tar*`` artifacts a makepkg build leaves in the
PKGBUILD directory: locate them, and parse a built filename back into its
resolved ``(epoch, pkgver, pkgrel)``.  No I/O beyond a directory glob, no
subprocess, no logging — leaf utilities consumed by the build orchestrator
(``makepkg_wrapper``) and the post-build version readers (``build_core``,
``pacman``, ``vcs_pkgver``).
"""
import re
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
    try:
        ver_rel, _arch = rest.rsplit("-", 1)
        ver_part, pkgrel = ver_rel.rsplit("-", 1)
    except ValueError:
        return None
    epoch = "0"
    if ":" in ver_part:
        epoch, _, ver_part = ver_part.partition(":")
    if not ver_part or not pkgrel:
        return None
    return (epoch, ver_part, pkgrel)
