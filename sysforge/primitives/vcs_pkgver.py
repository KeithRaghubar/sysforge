"""
vcs_pkgver.py — resolve effective [epoch:]pkgver-pkgrel for a VCS PKGBUILD.

For VCS packages (-git, -svn, -hg, -bzr) the static PKGBUILD pkgver is just a
seed; the real version is computed by pkgver() at build time after the
upstream sources are checked out. This module runs that pkgver() pass without
performing the build, so `sysforge update --devel` can vercmp against the
installed version and skip packages whose upstream HEAD has not advanced.

Two-step makepkg invocation:
    1. ``makepkg -od --nobuild --noprepare --nodeps --skippgpcheck --noconfirm``
       fetches/updates VCS sources and runs pkgver().
    2. ``makepkg --packagelist`` prints the resolved
       ``<pkgname>-[epoch:]<pkgver>-<pkgrel>-<arch>.pkg.tar`` filenames.

Caller policy: any failure (non-zero exit, timeout, unparseable output)
returns None. update.py treats None as "skip with warn" rather than
"rebuild on doubt" — explicit user choice; missed updates are preferable
to wasted rebuilds for transient pkgver() flakes.

Public API:
    evaluate_vcs_pkgver(pkgbuild_dir, *, timeout=300) -> str | None
"""
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename

_log = log.get_logger("VCS_PKGVER")


_RESOLVE_CMD = [
    "makepkg", "-od",
    "--nobuild", "--noprepare", "--nodeps",
    "--skippgpcheck", "--noconfirm",
]
_PACKAGELIST_CMD = ["makepkg", "--packagelist"]


def evaluate_vcs_pkgver(pkgbuild_dir: Path, *, timeout: int = 300) -> str | None:
    """Run pkgver() in pkgbuild_dir and return ``[epoch:]pkgver-pkgrel`` or None.

    Returns None on any failure; stderr (or the exception) is logged at WARN
    so operators can investigate transient or persistent breakage.
    """
    pkgbuild_dir = Path(pkgbuild_dir)
    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        _log.warn(f"{pkgbuild_dir}: PKGBUILD missing — cannot evaluate pkgver()")
        return None

    try:
        resolve = subprocess.run(
            _RESOLVE_CMD, cwd=pkgbuild_dir, capture_output=True,
            text=True, timeout=timeout,
        )
    except FileNotFoundError:
        _log.warn(f"{pkgbuild_dir}: makepkg not on PATH")
        return None
    except subprocess.TimeoutExpired:
        _log.warn(f"{pkgbuild_dir}: pkgver() resolve timed out after {timeout}s")
        return None

    if resolve.returncode != 0:
        # makepkg's stderr is the operator-actionable signal here (network,
        # broken pkgver(), missing makedeps). Truncate to the last few lines
        # to keep the log readable when makepkg is verbose.
        tail = "\n".join(resolve.stderr.strip().splitlines()[-5:])
        _log.warn(
            f"{pkgbuild_dir}: makepkg pkgver-resolve exited "
            f"{resolve.returncode}\n{tail}"
        )
        return None

    try:
        listing = subprocess.run(
            _PACKAGELIST_CMD, cwd=pkgbuild_dir, capture_output=True,
            text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        _log.warn(f"{pkgbuild_dir}: makepkg --packagelist timed out")
        return None

    if listing.returncode != 0:
        tail = "\n".join(listing.stderr.strip().splitlines()[-5:])
        _log.warn(
            f"{pkgbuild_dir}: makepkg --packagelist exited "
            f"{listing.returncode}\n{tail}"
        )
        return None

    # --packagelist prints one filename per pkgname, e.g.
    #   /pkgdest/foo-git-0.1.r5.gabcdef-1-x86_64.pkg.tar
    # Any line that resolves to a valid (epoch, pkgver, pkgrel) tuple is
    # equivalent for vercmp purposes (split packages share pkgver/pkgrel).
    for line in listing.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        filename = Path(line).name
        # Re-derive pkgname from the filename: strip the trailing
        # ``-<pkgver>-<pkgrel>-<arch>.pkg.tar.*`` segment. We don't know
        # pkgname up-front because split packages produce multiple lines.
        stem = filename
        for ext in (".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz",
                    ".pkg.tar.bz2", ".pkg.tar.lz4", ".pkg.tar.lzo",
                    ".pkg.tar.lrz", ".pkg.tar.Z", ".pkg.tar"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        # stem now looks like ``<pkgname>-[epoch:]<pkgver>-<pkgrel>-<arch>``;
        # peel off the trailing three segments to recover pkgname so we can
        # hand the filename to the canonical parser for validation.
        try:
            head, _ = stem.rsplit("-", 1)   # drop arch
            head, _ = head.rsplit("-", 1)   # drop pkgrel
            pkgname, _ = head.rsplit("-", 1)  # drop [epoch:]pkgver
        except ValueError:
            continue
        parsed = _parse_built_pkg_filename(pkgname, filename)
        if parsed is None:
            continue
        epoch, pkgver, pkgrel = parsed
        if epoch and epoch != "0":
            return f"{epoch}:{pkgver}-{pkgrel}"
        return f"{pkgver}-{pkgrel}"

    _log.warn(
        f"{pkgbuild_dir}: makepkg --packagelist produced no parseable "
        f"filename (stdout={listing.stdout!r})"
    )
    return None
