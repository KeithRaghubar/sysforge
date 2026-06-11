"""
pacman.py — shared pacman query and batch build/install operations

Single home for all subprocess-level pacman interaction and batch build
infrastructure shared across update, converge, and other commands that
build and install packages in batch.

Public API:
    BATCH_STRIP_FLAGS                          — frozenset
    BATCH_EXTRA_FLAGS                          — list[str]
    get_pkgdest()                   → Path | None
    snapshot_pkg_dir(directory)     → frozenset
    get_pacman_cache_dirs()         → list[Path]
    cached_pkg_files_for(names)     → dict[str, Path | None]
    batch_install_pkgs(pkg_paths)   → bool
    read_pkgname_from_file(path)    → str | None
    filter_pkgs_to_installed(paths, installed) → (keep, dropped)
    collect_makedeps(pkgbuild_paths) → list
    collect_builddeps(pkgbuild_paths) → list
    filter_missing_deps(deps)       → list
    batch_install_makedeps(deps)    → None
    get_installed_version(pkgname)  → str | None
    get_all_installed_packages()    → dict[str, str]
    get_foreign_packages()          → dict[str, str]
    get_pacman_sync_version(pkgname) → str | None
    checkupdates_map(timeout=60.0)  → dict[str, str] | None
    get_local_db_entry(pkgname)     → Path | None
    get_package_files(pkgname)      → list[str]
    get_package_depends(pkgname)    → list[str]
    get_pkgbase(pkgname)            → str | None
"""
import os
import re
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur_resolve import _looks_unresolved, _strip_version
from sysforge.primitives.makepkg_flags import INSTALL_FLAGS, SYNC_FLAGS

_log = log.get_logger("PACMAN")


# ---------------------------------------------------------------------------
# pyalpm — optional fast path for read queries
#
# Set SYSFORGE_PACMAN_NO_PYALPM=1 to force the subprocess fallback even when
# pyalpm is installed (for parity testing).
# ---------------------------------------------------------------------------

try:
    import pyalpm  # type: ignore[import-not-found]
    _HAS_PYALPM = True
except ImportError:
    pyalpm = None  # type: ignore[assignment]
    _HAS_PYALPM = False


def _use_pyalpm() -> bool:
    return _HAS_PYALPM and not os.environ.get("SYSFORGE_PACMAN_NO_PYALPM")


_PACMAN_CONF = Path("/etc/pacman.conf")
_alpm_handle = None


def _read_sync_repo_names() -> list[str]:
    """Parse /etc/pacman.conf for [<repo>] section names, skipping [options].

    Honours nothing else (Include, SigLevel, etc.) — we only need the names
    to register sync DBs. Order is preserved so multilib stays last.
    """
    if not _PACMAN_CONF.is_file():
        return ["core", "extra"]
    repos: list[str] = []
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    try:
        with open(_PACMAN_CONF, encoding="utf-8") as f:
            for line in f:
                m = section_re.match(line)
                if m and m.group(1) != "options":
                    repos.append(m.group(1))
    except OSError:
        return ["core", "extra"]
    return repos or ["core", "extra"]


def _get_alpm_handle():
    """Return a memoized libalpm handle with sync DBs registered."""
    global _alpm_handle
    if _alpm_handle is not None:
        return _alpm_handle
    handle = pyalpm.Handle("/", "/var/lib/pacman")
    for repo in _read_sync_repo_names():
        try:
            handle.register_syncdb(repo, 0)
        except pyalpm.error:
            continue
    _alpm_handle = handle
    return _alpm_handle


# ---------------------------------------------------------------------------
# Batch build flags
# ---------------------------------------------------------------------------

# Flags stripped from each per-package makepkg call during batch update/converge.
# Deps are pre-installed in one shot; packages are installed in one shot at the end.
BATCH_STRIP_FLAGS = SYNC_FLAGS | INSTALL_FLAGS

# Always clean the build tree on update — prevents stale $srcdir from a previous
# failed run causing patch-already-applied errors in prepare().
BATCH_EXTRA_FLAGS = ["-C"]


# ---------------------------------------------------------------------------
# PKGDEST
# ---------------------------------------------------------------------------

def get_pkgdest() -> Path | None:
    """Return PKGDEST from the layered system makepkg.conf, or None if unset."""
    try:
        from sysforge.primitives.config import parse_system_makepkg_conf
        sys_conf = parse_system_makepkg_conf()
        raw = sys_conf.get("PKGDEST", "").strip().strip("\"'")
        if raw:
            return Path(raw).expanduser()
    except Exception:
        pass
    return None


def detect_orphan_artifacts(
    pkgdest: Path,
    installed: dict[str, str],
) -> dict[str, list[Path]]:
    """Classify ``.pkg.tar*`` files in ``pkgdest`` as superseded build leftovers.

    Returns ``{"superseded": [...]}``: artifacts whose pkgname IS installed
    AND whose version is strictly older than the installed version. These
    are stale build outputs that ``--prune`` can safely delete because the
    installed package is, by definition, newer.

    Files newer than the installed version, files matching the installed
    version exactly, files whose pkgname is not installed at all, files
    whose ``.PKGINFO`` can't be read, and files whose filename doesn't parse
    are intentionally NOT classified — the caller can't safely tell whether
    they're stale or kept on purpose (e.g. a build of a kernel branch with
    local commits the user wants to keep around for later install). The
    guiding rule: if ``--prune`` wouldn't safely delete it, don't
    list it. ``.sig`` files are ignored.
    """
    from sysforge.primitives.version import vercmp  # avoid import cycle at module load

    superseded: list[Path] = []
    if not pkgdest.is_dir():
        return {"superseded": superseded}

    for path in sorted(pkgdest.glob("*.pkg.tar*")):
        if path.name.endswith(".sig"):
            continue
        pkgname = read_pkgname_from_file(path)
        if pkgname is None:
            continue
        if pkgname not in installed:
            # Not installed — could be a kept-for-later build, a test
            # artifact, or genuinely abandoned. We can't tell, so we
            # don't surface it.
            continue
        from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
        parsed = _parse_built_pkg_filename(pkgname, path.name)
        if parsed is None:
            continue
        epoch, pkgver, pkgrel = parsed
        artifact_ver = (
            f"{epoch}:{pkgver}-{pkgrel}" if epoch and epoch != "0"
            else f"{pkgver}-{pkgrel}"
        )
        try:
            cmp = vercmp(artifact_ver, installed[pkgname])
        except RuntimeError:
            continue
        if cmp < 0:
            superseded.append(path)

    return {"superseded": superseded}


# ---------------------------------------------------------------------------
# Package file collection
# ---------------------------------------------------------------------------

def snapshot_pkg_dir(directory: Path) -> frozenset:
    """Return frozenset of .pkg.tar* paths (not .sig) in directory.

    Matches both compressed (.pkg.tar.zst, .pkg.tar.xz) and uncompressed
    (.pkg.tar) packages — the latter is produced when PKGEXT='.pkg.tar'.
    """
    if not directory.exists():
        return frozenset()
    return frozenset(
        p for p in directory.glob("*.pkg.tar*")
        if not p.name.endswith(".sig")
    )


# ---------------------------------------------------------------------------
# Pacman cache (offline rollback source)
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_DIR = Path("/var/cache/pacman/pkg")


def get_pacman_cache_dirs() -> list[Path]:
    """Return the configured pacman package cache directories.

    Reads ``CacheDir`` lines from the ``[options]`` section of
    /etc/pacman.conf (there may be several; pacman searches them in order),
    falling back to ``/var/cache/pacman/pkg`` when none are set — pacman's own
    default. Used to locate a previously-installed ``.pkg.tar*`` for offline
    rollback without re-downloading.
    """
    if not _PACMAN_CONF.is_file():
        return [_DEFAULT_CACHE_DIR]
    dirs: list[Path] = []
    in_options = False
    try:
        with open(_PACMAN_CONF, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    in_options = line == "[options]"
                    continue
                if not in_options or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == "CacheDir":
                    for token in val.split():
                        dirs.append(Path(token))
    except OSError:
        return [_DEFAULT_CACHE_DIR]
    return dirs or [_DEFAULT_CACHE_DIR]


def cached_pkg_files_for(names) -> dict[str, Path | None]:
    """Locate each installed package's ``.pkg.tar*`` in the pacman cache.

    For every name in ``names`` that is currently installed, resolves its
    installed ``pkgver-pkgrel`` (``pacman -Q``) and finds the matching package
    archive under the configured cache dir(s). The returned dict maps:

      - name → Path     when the exact installed version's archive is cached
      - name → None     when the package is not installed, or its archive is
                        not present in any cache dir (e.g. cleared by paccache)

    The caller (toolchain stage snapshot) uses a complete mapping as the
    offline-undo source: ``batch_install_pkgs(list-of-Paths)`` reinstalls the
    prior-good set in one ``pacman -U`` transaction. ``None`` entries are how
    the caller learns auto-undo can't fully restore and warns up front.

    Matching is exact on ``<name>-<pkgver>-<pkgrel>-<arch>.pkg.tar*`` so a
    prefix collision (``llvm`` vs ``llvm-libs``) can't return the wrong file.
    """
    cache_dirs = get_pacman_cache_dirs()
    result: dict[str, Path | None] = {}
    for name in names:
        ver = get_installed_version(name)
        if ver is None:
            result[name] = None
            continue
        # Built archives are <name>-<pkgver>-<pkgrel>-<arch>.pkg.tar* — the
        # name+version prefix is followed by a '-<arch>' segment, so anchor on
        # "<name>-<ver>-" to avoid matching <name>-libs-<ver>.
        prefix = f"{name}-{ver}-"
        found: Path | None = None
        for cache_dir in cache_dirs:
            if not cache_dir.is_dir():
                continue
            for cand in sorted(cache_dir.glob(f"{name}-{ver}-*.pkg.tar*")):
                if cand.name.endswith(".sig"):
                    continue
                if cand.name.startswith(prefix):
                    found = cand
                    break
            if found is not None:
                break
        result[name] = found
    return result


# ---------------------------------------------------------------------------
# Package install
# ---------------------------------------------------------------------------

def read_pkgname_from_file(path) -> str | None:
    """Return the pkgname recorded in a built package's .PKGINFO, or None.

    Uses bsdtar (already required by makepkg) to read the embedded
    .PKGINFO without fully extracting the archive.
    """
    try:
        result = subprocess.run(
            ["bsdtar", "-xOqf", str(path), ".PKGINFO"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("pkgname = "):
            return line[len("pkgname = "):].strip()
    return None


def filter_pkgs_to_installed(
    pkg_paths: list, installed: set,
) -> tuple[list, list]:
    """Split pkg files into (keep, dropped) by whether pkgname is in ``installed``.

    Built split-pkgbase runs emit .pkg.tar files for every sub-package, even
    ones the user never installed. Returns ``(keep, dropped)`` where
    ``dropped`` is ``[(path, pkgname)]``. Files whose pkgname can't be read
    fall through to ``keep`` so the caller can let pacman surface the error.
    """
    keep: list = []
    dropped: list = []
    for p in pkg_paths:
        pn = read_pkgname_from_file(p)
        if pn is None:
            keep.append(p)
        elif pn in installed:
            keep.append(p)
        else:
            dropped.append((p, pn))
    return keep, dropped


def batch_install_pkgs(pkg_paths: list) -> bool:
    """Install all built packages in one sudo pacman -U call. Returns True on success."""
    missing = [p for p in pkg_paths if not Path(p).exists()]
    if missing:
        for p in missing:
            _log.warn(f"Package file gone before install (removed by hook?): {p}")
        pkg_paths = [p for p in pkg_paths if Path(p).exists()]
    if not pkg_paths:
        _log.error("No package files remain to install after filtering missing paths")
        return False
    _log.info(f"Batch-installing {len(pkg_paths)} built package file(s)")
    result = subprocess.run(
        ["sudo", "pacman", "-U", "--noconfirm"] + [str(p) for p in pkg_paths],
        stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        if result.stderr:
            for line in result.stderr.splitlines():
                _log.error(line)
        return False
    return True


# ---------------------------------------------------------------------------
# Makedep handling
# ---------------------------------------------------------------------------

# PKGBUILD globals keys that name packages which must be installed before a
# `-s`-stripped batch build can proceed. makepkg checks runtime ``depends`` as
# well as ``makedepends``/``checkdepends`` before building (see /usr/bin/makepkg
# "Checking runtime dependencies…"); with ``-s`` stripped it does not install
# them, so every one must already be present or the build aborts (exit 8).
_BUILD_DEP_KEYS = ("depends", "makedepends", "checkdepends")


def _collect_dep_names(pkgbuild_paths: list, keys) -> list:
    """Parse PKGBUILDs and return a sorted unique list of names from ``keys``.

    Version constraints are stripped ("cmake>=3.16" → "cmake") and any token the
    static parser left as un-evaluated shell syntax is skipped so it is never
    handed to the repo ``pacman -S`` transaction as a bogus name.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    deps: set = set()
    for path in pkgbuild_paths:
        try:
            globals_ = parse_pkgbuild(path).get("globals", {})
            for key in keys:
                raw = globals_.get(key, [])
                if isinstance(raw, str):
                    raw = [raw]
                for dep in raw:
                    if _looks_unresolved(dep):
                        continue
                    deps.add(_strip_version(dep))
        except (OSError, KeyError, ValueError) as e:
            _log.warn(f"deps parse error ({Path(path).parent.name}): {e}")
    return sorted(deps)


def collect_makedeps(pkgbuild_paths: list) -> list:
    """Parse PKGBUILDs and return a sorted unique list of their makedepends."""
    return _collect_dep_names(pkgbuild_paths, ("makedepends",))


def collect_builddeps(pkgbuild_paths: list) -> list:
    """Parse PKGBUILDs and return their depends + makedepends + checkdepends.

    The full set of packages makepkg requires present before building. Used by
    ``build_core.prepare_deps`` so the ``-s``-stripped batch build does not abort
    on a missing repo runtime ``depends`` (makepkg checks those too, not only
    makedepends).
    """
    return _collect_dep_names(pkgbuild_paths, _BUILD_DEP_KEYS)


def filter_missing_deps(deps: list) -> list:
    """Return the subset of deps not satisfiable by current pacman packages."""
    if not deps:
        return []
    if _use_pyalpm():
        try:
            localdb = _get_alpm_handle().get_localdb()
            return [
                dep for dep in deps
                if pyalpm.find_satisfier(localdb.pkgcache, dep) is None
            ]
        except pyalpm.error:
            pass
    result = subprocess.run(
        ["pacman", "-T"] + deps,
        capture_output=True,
        text=True,
    )
    # pacman -T exits 0 if all satisfied, 127 if any are missing.
    # The missing deps are printed to stdout.
    return result.stdout.split()


def batch_install_makedeps(deps: list) -> None:
    _log.info(f"Batch-installing {len(deps)} missing makedep(s): {deps}")
    result = subprocess.run(
        ["sudo", "pacman", "-S", "--needed", "--noconfirm"] + deps
    )
    if result.returncode != 0:
        raise RuntimeError(f"makedep install failed (exit {result.returncode})")


# ---------------------------------------------------------------------------
# Package queries
# ---------------------------------------------------------------------------

def get_installed_version(pkgname: str) -> str | None:
    """Run `pacman -Q pkgname`, return version string or None if not installed."""
    if _use_pyalpm():
        try:
            pkg = _get_alpm_handle().get_localdb().get_pkg(pkgname)
            return pkg.version if pkg else None
        except pyalpm.error:
            pass
    result = subprocess.run(
        ["pacman", "-Q", pkgname],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    # Output format: "pkgname version\n"
    parts = result.stdout.strip().split()
    return parts[1] if len(parts) >= 2 else None


def get_all_installed_packages() -> dict[str, str]:
    """Run `pacman -Q` and return {pkgname: installed_version} for all installed packages."""
    if _use_pyalpm():
        try:
            localdb = _get_alpm_handle().get_localdb()
            return {pkg.name: pkg.version for pkg in localdb.pkgcache}
        except pyalpm.error:
            pass
    result = subprocess.run(["pacman", "-Q"], capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    packages = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            packages[parts[0]] = parts[1]
    return packages


def get_foreign_packages() -> dict[str, str]:
    """
    Run `pacman -Qm` and return {pkgname: installed_version} for all
    foreign (non-repo) packages currently installed.
    """
    if _use_pyalpm():
        try:
            handle = _get_alpm_handle()
            sync_names: set[str] = set()
            for db in handle.get_syncdbs():
                sync_names.update(pkg.name for pkg in db.pkgcache)
            return {
                pkg.name: pkg.version
                for pkg in handle.get_localdb().pkgcache
                if pkg.name not in sync_names
            }
        except pyalpm.error:
            pass
    result = subprocess.run(["pacman", "-Qm"], capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    packages = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            packages[parts[0]] = parts[1]
    return packages


def get_pacman_sync_version(pkgname: str) -> str | None:
    """Return the version available in pacman sync databases, or None if not found."""
    if _use_pyalpm():
        try:
            for db in _get_alpm_handle().get_syncdbs():
                pkg = db.get_pkg(pkgname)
                if pkg:
                    return pkg.version
            return None
        except pyalpm.error:
            pass
    result = subprocess.run(["pacman", "-Si", "--", pkgname], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Version"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


# Sentinel for checkupdates_map: distinguishes "tool unavailable" (None) from
# "tool ran, nothing to upgrade" (empty dict).
CHECKUPDATES_UNAVAILABLE = None


def checkupdates_map(timeout: float = 60.0) -> dict[str, str] | None:
    """Run ``checkupdates`` once and return ``{pkgname: new_version}``.

    Uses pacman-contrib's ``checkupdates`` because it refreshes the sync DBs
    to a side-copy of /var/lib/pacman/sync rather than the live one — safe to
    run without sudo and without partial-upgrade risk. Output lines are
    ``pkgname oldver -> newver``.

    Returns:
      - ``dict`` (possibly empty) when checkupdates ran. Empty means no repo
        upgrades pending.
      - ``None`` when ``checkupdates`` is not installed, timed out, or hit an
        I/O error. Caller treats this as "fast path unavailable" and surfaces
        a one-shot warning.

    checkupdates exit codes: 0 = updates pending (output is on stdout),
    2 = no updates (exit-0-with-empty-output on older releases; both handled
    by returning ``{}``), other = error (returns ``None``).
    """
    try:
        result = subprocess.run(
            ["checkupdates"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warn(f"checkupdates failed: {e}")
        return None

    # checkupdates exits 2 when there are no updates — not an error.
    if result.returncode not in (0, 2):
        if result.stderr.strip():
            _log.warn(f"checkupdates exited {result.returncode}: {result.stderr.strip()}")
        else:
            _log.warn(f"checkupdates exited {result.returncode}")
        return None

    updates: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        # Expected: "pkgname oldver -> newver" (4 tokens). Tolerate the
        # alternate 3-token "pkgname oldver newver" form some forks emit.
        if len(parts) == 4 and parts[2] == "->":
            updates[parts[0]] = parts[3]
        elif len(parts) == 3:
            updates[parts[0]] = parts[2]
    return updates


# ---------------------------------------------------------------------------
# Local database queries (/var/lib/pacman/local)
# ---------------------------------------------------------------------------

_LOCAL_DB_ROOT = Path("/var/lib/pacman/local")


def get_local_db_entry(pkgname: str, root: Path | None = None) -> Path | None:
    """
    Return the path to /var/lib/pacman/local/<pkgname>-<ver>/ for an installed
    package, or None if not installed. `root` overrides the DB root (for tests).

    Matches the DB directory by name (<pkgname>-<pkgver>-<pkgrel>). Multiple
    matches can occur if a pkgname is a prefix of another (e.g. `llvm` vs
    `llvm-libs`); this filters to exact-name matches only.
    """
    db_root = root or _LOCAL_DB_ROOT
    if not db_root.is_dir():
        return None
    # Directory names have the form <pkgname>-<pkgver>-<pkgrel>. Split on the
    # last two '-' separators to extract the name portion and compare exactly.
    matches: list[Path] = []
    for child in db_root.iterdir():
        if not child.is_dir():
            continue
        parts = child.name.rsplit("-", 2)
        if len(parts) == 3 and parts[0] == pkgname:
            matches.append(child)
    if not matches:
        return None
    if len(matches) > 1:
        # Shouldn't happen in practice — pacman keeps one entry per pkg.
        _log.warn(f"multiple local-db entries for {pkgname}: {[m.name for m in matches]}")
    return matches[0]


def get_package_files(pkgname: str, root: Path | None = None) -> list[str]:
    """
    Return the list of paths owned by an installed package, as recorded in
    /var/lib/pacman/local/<pkg>-<ver>/files. Paths are returned verbatim
    (no leading slash, e.g. "usr/lib/libfoo.so.1"). Directory entries
    (trailing '/') are filtered out. Empty list if not installed or unreadable.
    """
    entry = get_local_db_entry(pkgname, root=root)
    if entry is None:
        return []
    files_path = entry / "files"
    if not files_path.is_file():
        return []
    paths: list[str] = []
    in_files = False
    for line in files_path.read_text().splitlines():
        if line == "%FILES%":
            in_files = True
            continue
        if not in_files:
            continue
        if not line or line.endswith("/"):
            continue
        if line.startswith("%"):
            break
        paths.append(line)
    return paths


def get_pkgbase(pkgname: str, root: Path | None = None) -> str | None:
    """
    Return the %BASE% (pkgbase) recorded in /var/lib/pacman/local/<pkg>-<ver>/desc
    for an installed package. None if not installed, desc unreadable, or
    %BASE% not recorded (some older entries omit it; non-split packages
    typically don't record it either).

    Canonical source for mapping a split-subpackage name back to its parent
    pkgbase — works for any installed package (repo or foreign), no AUR
    access needed.
    """
    entry = get_local_db_entry(pkgname, root=root)
    if entry is None:
        return None
    desc_path = entry / "desc"
    if not desc_path.is_file():
        return None
    in_section = False
    for line in desc_path.read_text().splitlines():
        if line == "%BASE%":
            in_section = True
            continue
        if not in_section:
            continue
        if not line or line.startswith("%"):
            return None
        return line
    return None


def get_package_depends(pkgname: str, root: Path | None = None) -> list[str]:
    """
    Return the %DEPENDS% array from /var/lib/pacman/local/<pkg>-<ver>/desc
    for an installed package. Empty list if not installed, unreadable, or
    the package declares no depends.
    """
    entry = get_local_db_entry(pkgname, root=root)
    if entry is None:
        return []
    desc_path = entry / "desc"
    if not desc_path.is_file():
        return []
    depends: list[str] = []
    in_section = False
    for line in desc_path.read_text().splitlines():
        if line == "%DEPENDS%":
            in_section = True
            continue
        if not in_section:
            continue
        if not line:
            break
        if line.startswith("%"):
            break
        depends.append(line)
    return depends
