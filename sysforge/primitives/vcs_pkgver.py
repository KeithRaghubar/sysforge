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
    peek_upstream_commit(pkgbuild_dir, *, timeout=30) -> str | None
    read_built_upstream_commit(pkgbuild_dir, *, timeout=10) -> str | None
"""
import re
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

_log = log.get_logger("VCS_PKGVER")


_RESOLVE_CMD = [
    "makepkg", "-od",
    "--nobuild", "--noprepare", "--nodeps",
    "--skippgpcheck", "--noconfirm",
]
_PACKAGELIST_CMD = ["makepkg", "--packagelist"]

# Any leftover bash variable reference after parse_pkgbuild's scalar pass —
# e.g. ${pkgver/-/_}, $_commit, ${var:-default}. Such URLs/fragments are
# unsafe to feed to git ls-remote because the literal we hold doesn't match
# what makepkg would resolve at build time.
_UNRESOLVED_BASH_VAR = re.compile(r"\$(?:\{[^}]*\}|\w+)")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


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


def _single_git_source(globals_: dict) -> tuple[str, str, str | None] | None:
    """Identify the unique git source in a parsed PKGBUILD's ``source=()``.

    Returns ``(clone_name, url, fragment)`` where ``clone_name`` is the
    directory makepkg uses under ``srcdir/`` (per the ``name::vcs+url`` form
    or, lacking that, the URL basename minus a trailing ``.git``), ``url``
    is the bare upstream URL, and ``fragment`` is the literal ``#...`` body
    (without the leading ``#``) or None.

    Returns None when:
      - there is no git source, or more than one (multi-git PKGBUILDs fall
        through to the slow ``evaluate_vcs_pkgver`` path);
      - any of clone_name/url/fragment still contains an unresolved bash
        variable reference, since the literal we'd compare against is not
        what makepkg would resolve at build time.

    Recognises the makepkg(8) git source forms:
        ``git+<url>[#frag]``
        ``git://...[#frag]``
        ``<name>::<either-of-the-above>``
    """
    sources = globals_.get("source", []) or []
    if isinstance(sources, str):
        sources = [sources]
    git_entries: list[tuple[str, str, str | None]] = []
    for raw in sources:
        s = raw
        clone_name: str | None = None
        if "::" in s:
            clone_name, _, s = s.partition("::")
        if s.startswith("git+"):
            s = s[len("git+"):]
        elif s.startswith("git://"):
            pass
        else:
            continue
        url, _, fragment = s.partition("#")
        fragment = fragment or None
        if clone_name is None:
            base = url.rstrip("/").split("/")[-1]
            if base.endswith(".git"):
                base = base[:-4]
            clone_name = base
        git_entries.append((clone_name, url, fragment))
    if len(git_entries) != 1:
        return None
    clone_name, url, fragment = git_entries[0]
    for piece in (clone_name, url, fragment or ""):
        if _UNRESOLVED_BASH_VAR.search(piece):
            return None
    return (clone_name, url, fragment)


def peek_upstream_commit(pkgbuild_dir: Path, *, timeout: int = 30) -> str | None:
    """Resolve the current upstream commit SHA via ``git ls-remote``.

    Cheap probe used by ``sysforge update --devel`` to short-circuit the
    full ``evaluate_vcs_pkgver`` pass when nothing has moved since the last
    successful build. Returns a 40-char SHA on success.

    Returns None for non-single-git-source PKGBUILDs, unresolved variable
    references, missing PKGBUILD, ``git`` not on PATH, ls-remote failure
    or timeout, or empty/malformed ls-remote output.
    """
    pkgbuild_dir = Path(pkgbuild_dir)
    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        return None
    try:
        meta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        _log.warn(f"{pkgbuild_dir}: PKGBUILD parse failed for ls-remote peek: {e}")
        return None
    src = _single_git_source(meta.get("globals", {}))
    if src is None:
        return None
    _, url, fragment = src

    # makepkg fragment grammar (PKGBUILD(5)): key=value pairs joined by '&'.
    # commit=<sha> is a hard pin — no network needed.
    # tag=<t>, branch=<b>, fragment=<ref> all become a refspec for ls-remote.
    ref = "HEAD"
    if fragment:
        for part in fragment.split("&"):
            key, _, val = part.partition("=")
            if key == "commit":
                if _SHA1.match(val):
                    return val
                # Short SHA or non-hex commit pin: we can't compare directly,
                # so fall through to ls-remote against HEAD as a best effort.
                continue
            if key in ("tag", "branch", "fragment") and val:
                ref = val
                break

    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        _log.warn(f"{pkgbuild_dir}: git not on PATH for ls-remote peek")
        return None
    except subprocess.TimeoutExpired:
        _log.warn(f"{pkgbuild_dir}: git ls-remote {url} timed out after {timeout}s")
        return None

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-3:])
        _log.warn(
            f"{pkgbuild_dir}: git ls-remote exited {result.returncode}\n{tail}"
        )
        return None

    for line in result.stdout.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if _SHA1.match(sha):
            return sha
    return None


def read_built_upstream_commit(pkgbuild_dir: Path, *, timeout: int = 10) -> str | None:
    """Read the just-built upstream commit SHA from ``srcdir/<clone>/HEAD``.

    Called from the build pipeline immediately after a successful
    ``makepkg`` so the SHA can be persisted in ``build_state.toml`` for the
    next ``--devel`` short-circuit. Returns None for non-single-git-source
    PKGBUILDs, missing clone dir (e.g. SRCDEST elsewhere), or rev-parse
    failures.
    """
    pkgbuild_dir = Path(pkgbuild_dir)
    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        return None
    try:
        meta = parse_pkgbuild(pkgbuild_path)
    except Exception:
        return None
    src = _single_git_source(meta.get("globals", {}))
    if src is None:
        return None
    clone_name, _, _ = src
    src_dir = pkgbuild_dir / "src" / clone_name
    if not src_dir.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if _SHA1.match(sha) else None
