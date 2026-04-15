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
    collect_makedeps(pkgbuild_paths) → list
    filter_missing_deps(deps)       → list
    batch_install_makedeps(deps)    → None
    get_installed_version(pkgname)  → str | None
    get_all_installed_packages()    → dict[str, str]
    get_foreign_packages()          → dict[str, str]
    get_pacman_sync_version(pkgname) → str | None
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
