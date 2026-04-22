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
    batch_install_pkgs(pkg_paths)   → bool
    read_pkgname_from_file(path)    → str | None
    filter_pkgs_to_installed(paths, installed) → (keep, dropped)
    collect_makedeps(pkgbuild_paths) → list
    filter_missing_deps(deps)       → list
    batch_install_makedeps(deps)    → None
    get_installed_version(pkgname)  → str | None
    get_all_installed_packages()    → dict[str, str]
    get_foreign_packages()          → dict[str, str]
    get_pacman_sync_version(pkgname) → str | None
    get_local_db_entry(pkgname)     → Path | None
    get_package_files(pkgname)      → list[str]
    get_package_depends(pkgname)    → list[str]
"""
import subprocess
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur_resolve import _strip_version

_log = log.get_logger("PACMAN")


# ---------------------------------------------------------------------------
# Batch build flags
# ---------------------------------------------------------------------------

# Flags stripped from each per-package makepkg call during batch update/converge.
# Deps are pre-installed in one shot; packages are installed in one shot at the end.
BATCH_STRIP_FLAGS = frozenset({"--syncdeps", "-s", "--install", "-i"})

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

def collect_makedeps(pkgbuild_paths: list) -> list:
    """Parse PKGBUILDs and return a sorted unique list of their makedepends."""
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    deps: set = set()
    for path in pkgbuild_paths:
        try:
            pkgmeta = parse_pkgbuild(path)
            raw = pkgmeta.get("globals", {}).get("makedepends", [])
            if isinstance(raw, str):
                raw = [raw]
            # Strip version constraints (e.g. "cmake>=3.16" → "cmake")
            for dep in raw:
                deps.add(_strip_version(dep))
        except (OSError, KeyError, ValueError) as e:
            _log.warn(f"makedeps parse error ({Path(path).parent.name}): {e}")
    return sorted(deps)


def filter_missing_deps(deps: list) -> list:
    """Return the subset of deps not satisfiable by current pacman packages."""
    if not deps:
        return []
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
    result = subprocess.run(["pacman", "-Si", "--", pkgname], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Version"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


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
