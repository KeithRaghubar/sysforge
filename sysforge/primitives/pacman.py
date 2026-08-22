# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pacman.py — shared pacman query and batch build/install operations

Single home for all subprocess-level pacman interaction and batch build
infrastructure shared across update, build, and other commands that
build and install packages in batch.

Public API:
    BATCH_STRIP_FLAGS                          — frozenset
    BATCH_EXTRA_FLAGS                          — list[str]
    get_pkgdest()                   → Path | None
    get_builddir()                  → Path | None
    get_srcdest()                   → Path | None
    get_logdest()                   → Path | None
    snapshot_pkg_dir(directory)     → frozenset
    get_pacman_cache_dirs()         → list[Path]
    cached_pkg_files_for(names)     → dict[str, Path | None]
    batch_install_pkgs(pkg_paths)   → bool
    read_pkgname_from_file(path)    → str | None
    read_pkg_replaces_from_file(path) → set
    pkg_supersedes_installed(path, installed) → set
    filter_pkgs_to_installed(paths, installed) → (keep, dropped)
    collect_makedeps(pkgbuild_paths) → list
    collect_builddeps(pkgbuild_paths) → list
    filter_missing_deps(deps)       → list
    batch_install_makedeps(deps)    → None
    install_repo_pkgs(names)        → None
    remove_pkgs(names)              → None
    reinstall_repo_pkgs(names)      → None
    get_installed_version(pkgname)  → str | None
    get_all_installed_packages()    → dict[str, str]
    get_foreign_packages()          → dict[str, str]
    get_pacman_sync_version(pkgname) → str | None
    checkupdates_map(timeout=60.0)  → dict[str, str] | None
    get_repo_candidate_version(pkgname) → str | None
    reset_repo_candidate_cache()    → None
    get_local_db_entry(pkgname)     → Path | None
    get_package_files(pkgname)      → list[str]
    owners_of_paths(paths)          → dict[str, str]
    owners_of(candidates)           → dict[Path, str | None]
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
from sysforge.primitives.privilege import privileged_argv

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


def _alpm():
    """Return the ``pyalpm`` module, narrowed away from its None fallback.

    pyalpm has no type stubs and is an optional import (``pyalpm = None`` when
    absent, see above) so every attribute access on it reads as Optional to
    pyright. Every call site below is already gated by ``_use_pyalpm()``,
    which is True only when the real import succeeded — so this is always
    non-None in practice; the assert makes that provable at each use.
    """
    assert pyalpm is not None  # noqa: S101 — internal invariant, guarded by _use_pyalpm()
    return pyalpm


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
        with _PACMAN_CONF.open(encoding="utf-8") as f:
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
    handle = _alpm().Handle("/", "/var/lib/pacman")
    for repo in _read_sync_repo_names():
        try:
            handle.register_syncdb(repo, 0)
        except _alpm().error:
            continue
    _alpm_handle = handle
    return _alpm_handle


# ---------------------------------------------------------------------------
# Batch build flags
# ---------------------------------------------------------------------------

# Flags stripped from each per-package makepkg call during batch update/build.
# Deps are pre-installed in one shot; packages are installed in one shot at the end.
BATCH_STRIP_FLAGS = SYNC_FLAGS | INSTALL_FLAGS

# Always clean the build tree on update — prevents stale $srcdir from a previous
# failed run causing patch-already-applied errors in prepare().
BATCH_EXTRA_FLAGS = ["-C"]


# ---------------------------------------------------------------------------
# PKGDEST
# ---------------------------------------------------------------------------

def _resolve_makepkg_path(key: str) -> Path | None:
    """Resolve a makepkg path variable, mirroring makepkg's own precedence.

    makepkg lets the environment override ``makepkg.conf``, so we check
    ``os.environ`` first, then the layered system conf (``/etc/makepkg.conf``
    → ``$XDG_CONFIG_HOME/pacman/makepkg.conf`` → ``~/.makepkg.conf``). Quotes
    are stripped and ``~``/``$VARS`` expanded. Returns ``None`` when unset
    everywhere so callers can fall back to their own default.
    """
    raw = os.environ.get(key, "")
    if not raw:
        try:
            from sysforge.primitives.config import parse_system_makepkg_conf
            raw = parse_system_makepkg_conf().get(key, "")
        except Exception:
            raw = ""
    raw = raw.strip().strip("\"'")
    if not raw:
        return None
    return Path(os.path.expandvars(raw)).expanduser()


def get_pkgdest() -> Path | None:
    """Return PKGDEST from the env or layered system makepkg.conf, or None."""
    return _resolve_makepkg_path("PKGDEST")


def get_builddir() -> Path | None:
    """Return BUILDDIR from the env or layered system makepkg.conf, or None.

    This is the directory makepkg builds under (``$BUILDDIR/<pkgbase>``); when
    unset, makepkg builds in-place in the PKGBUILD directory, so callers append
    their own fallback.
    """
    return _resolve_makepkg_path("BUILDDIR")


def get_srcdest() -> Path | None:
    """Return SRCDEST from the env or layered system makepkg.conf, or None."""
    return _resolve_makepkg_path("SRCDEST")


def get_logdest() -> Path | None:
    """Return LOGDEST from the env or layered system makepkg.conf, or None.

    With ``OPTIONS+=log``, makepkg writes per-package build logs here rather
    than in the build directory.
    """
    return _resolve_makepkg_path("LOGDEST")


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
        with _PACMAN_CONF.open(encoding="utf-8") as f:
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


def _read_pkginfo_names(path, keys: tuple) -> dict:
    """Return {key: set of bare names} for the given .PKGINFO keys.

    One bsdtar per package, not one per field. Beware the key spellings:
    makepkg's ``write_kv_pair`` emits ``replaces`` and ``provides`` plural but
    ``conflict`` and ``depend`` **singular** — the .PKGINFO names are not the
    PKGBUILD array names. Version constraints (``foo<1.0``, ``wayland=1.26``)
    are stripped to the bare name. Empty sets on any read failure.
    """
    out: dict = {k: set() for k in keys}
    try:
        result = subprocess.run(
            ["bsdtar", "-xOqf", str(path), ".PKGINFO"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return out
    if result.returncode != 0:
        return out
    for line in result.stdout.splitlines():
        for key in keys:
            prefix = f"{key} = "
            if line.startswith(prefix):
                val = line[len(prefix):].strip()
                name = re.split(r"[<>=]", val, maxsplit=1)[0].strip()
                if name:
                    out[key].add(name)
                break
    return out


def read_pkg_replaces_from_file(path) -> set:
    """Return the set of names a built package ``replaces`` (from its .PKGINFO).

    Used by :func:`filter_pkgs_to_installed` to keep a conflict-mode renamed
    artifact (``mesa-sysforge`` from a ``--pgo=use`` build) whose own pkgname is
    on neither the installed nor the requested list but which replaces a stock
    package that is. Empty set on any read failure.
    """
    return _read_pkginfo_names(path, ("replaces",))["replaces"]


def pkg_supersedes_installed(path, installed: set) -> set:
    """Return the installed names this built package deliberately displaces.

    The one home for "this is an intended drop-in replacement", which packages
    declare two different ways:

    * an explicit ``replaces`` naming the installed package — what a
      conflict-mode ``-sysforge`` rename emits; and
    * the AUR ``-git`` idiom: ``conflicts`` **and** ``provides`` naming the same
      installed package (``wayland-git`` declares ``conflict = wayland`` plus
      ``provides = wayland=<ver>`` and no ``replaces`` at all).

    Requiring *both* halves of the second form is what keeps this narrow: a
    package that conflicts with something it does not provide is an unexpected
    collision, not a substitution, and must still stop the transaction for
    review. Empty set on any read failure — unreadable metadata never widens
    what gets auto-confirmed.
    """
    fields = _read_pkginfo_names(path, ("replaces", "conflict", "provides"))
    dropin = fields["conflict"] & fields["provides"]
    return (fields["replaces"] | dropin) & installed


def filter_pkgs_to_installed(
    pkg_paths: list, installed: set,
) -> tuple[list, list]:
    """Split pkg files into (keep, dropped) by whether pkgname is in ``installed``.

    Built split-pkgbase runs emit .pkg.tar files for every sub-package, even
    ones the user never installed. Returns ``(keep, dropped)`` where
    ``dropped`` is ``[(path, pkgname)]``. Files whose pkgname can't be read
    fall through to ``keep`` so the caller can let pacman surface the error.

    A conflict-mode ``-sysforge`` rename (e.g. ``mesa --pgo=use`` → builds
    ``mesa-sysforge``) produces a pkgname on neither the installed nor the
    requested list, so the bare-pkgname test would wrongly drop it. Such a
    package declares ``replaces = <stock name>``; if any replaced name is in
    ``installed`` it is the drop-in the user asked for and is kept.
    """
    keep: list = []
    dropped: list = []
    for p in pkg_paths:
        pn = read_pkgname_from_file(p)
        if pn is None or pn in installed or read_pkg_replaces_from_file(p) & installed:
            keep.append(p)
        else:
            dropped.append((p, pn))
    return keep, dropped


def batch_install_pkgs(pkg_paths: list, *, interactive: bool = False) -> bool:
    """Install all built packages in one sudo pacman -U call. Returns True on success.

    With ``interactive=True`` the ``--noconfirm`` flag is dropped and the
    pacman streams stay inherited (not captured), so a package-conflict
    question (``X and Y are in conflict. Remove Y? [y/N]``) is put to the
    operator on the controlling TTY instead of being auto-answered ``N`` by
    ``--noconfirm`` and aborting the transaction (B6).

    Non-interactive runs (the default, and every ``sysforge update``) still
    auto-answer that prompt ``N`` — fatal for a deliberate drop-in replacement,
    of which there are two flavours: a conflict-mode ``-sysforge`` rename
    (``replaces = <stock name>``), and an AUR ``-git`` package that conflicts
    with and provides the stock name (``wayland-git`` over ``wayland``). pacman
    auto-processes neither on a local ``-U`` — ``replaces`` is honoured only
    during a sync upgrade — so it raises the conflict prompt regardless.
    When (and only when) a built package supersedes something currently
    installed per :func:`pkg_supersedes_installed`, pass ``--ask=4``
    (``ALPM_QUESTION_CONFLICT_PKG``) so that intended removal is auto-confirmed;
    absent that relationship the prompt keeps its safe default so an unexpected
    conflict still aborts the transaction.
    """
    missing = [p for p in pkg_paths if not Path(p).exists()]
    if missing:
        for p in missing:
            _log.warn(f"Package file gone before install (removed by hook?): {p}")
        pkg_paths = [p for p in pkg_paths if Path(p).exists()]
    if not pkg_paths:
        _log.error("No package files remain to install after filtering missing paths")
        return False
    _log.info(f"Batch-installing {len(pkg_paths)} built package file(s)")
    argv = privileged_argv(["pacman", "-U"])
    if not interactive:
        argv.append("--noconfirm")
        # Auto-confirm only the intended drop-in replacement; see the docstring
        # and `pkg_supersedes_installed` for what qualifies.
        installed = set(get_all_installed_packages().keys())
        superseded: set = set()
        for p in pkg_paths:
            superseded |= pkg_supersedes_installed(p, installed)
        if superseded:
            _log.info(
                "Auto-confirming the replacement of "
                f"{', '.join(sorted(superseded))} (declared by the built "
                "package as a drop-in)"
            )
            argv.append("--ask=4")
    argv += [str(p) for p in pkg_paths]
    # Interactive: inherit pacman's streams so the conflict prompt is visible
    # and stdin can answer it. Non-interactive: capture stderr to relay it.
    run_kwargs: dict = {} if interactive else {"stderr": subprocess.PIPE, "text": True}
    result = subprocess.run(argv, **run_kwargs)
    if result.returncode != 0:
        if not interactive and result.stderr:
            for line in result.stderr.splitlines():
                _log.error(line)
        return False
    # Record sysforge's own install targets so `sysforge update`'s reconcile can
    # tell them apart from an external `pacman -S` (which demotes a source-built
    # entry). Best-effort: a missing marker never fails the install.
    from sysforge.primitives.install_reconcile import record_self_install
    record_self_install(
        [n for n in (read_pkgname_from_file(p) for p in pkg_paths) if n]
    )
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
                if _alpm().find_satisfier(localdb.pkgcache, dep) is None
            ]
        except _alpm().error:
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
        privileged_argv(["pacman", "-S", "--needed", "--noconfirm"]) + deps
    )
    if result.returncode != 0:
        raise RuntimeError(f"makedep install failed (exit {result.returncode})")


def install_repo_pkgs(names: list) -> None:
    """Install repo packages via ``sudo pacman -S --needed --noconfirm``.

    Mutates the system; callers wrap this in an install-bearing sentinel scope.
    Raises RuntimeError on a non-zero pacman exit.
    """
    _log.info(f"Installing {len(names)} repo package(s): {names}")
    result = subprocess.run(
        privileged_argv(["pacman", "-S", "--needed", "--noconfirm"]) + list(names)
    )
    if result.returncode != 0:
        raise RuntimeError(f"repo install failed (exit {result.returncode})")


def remove_pkgs(names: list) -> None:
    """Remove packages via ``sudo pacman -R --noconfirm``.

    Used by ``revert-to-stock`` to drop a renamed optimized build
    (e.g. ``mesa-sysforge``) before reinstalling the stock package. No-op
    on an empty list so callers need not guard.
    """
    if not names:
        return
    subprocess.run(
        privileged_argv(["pacman", "-R", "--noconfirm", "--", *names]),
        check=True,
    )


def reinstall_repo_pkgs(names: list) -> None:
    """(Re)install repo packages via ``sudo pacman -S --noconfirm`` (no
    ``--needed``), so a source-built package at the repo version is replaced
    by the repo binary. Used by ``revert-to-stock``. No-op on empty list.
    """
    if not names:
        return
    subprocess.run(
        privileged_argv(["pacman", "-S", "--noconfirm", "--", *names]),
        check=True,
    )


def uninstall_pkgs(names: list, extra_flags: list | None = None) -> None:
    """Remove packages via ``sudo pacman -Rnsu`` (interactive confirmation).

    Distinct from :func:`remove_pkgs` (``-R --noconfirm``, used by
    revert-to-stock before an immediate reinstall). Here the removal is the
    whole point, so:

      ``-n`` skip ``.pacsave`` backups; ``-s`` recurse now-orphaned deps;
      ``-u`` restrict recursion to packages nothing else needs (won't strand a
      still-required dep).

    No ``--noconfirm`` -- pacman prints its own transaction + confirmation. No-op
    on an empty list. Raises ``subprocess.CalledProcessError`` on non-zero exit.
    """
    if not names:
        return
    argv = privileged_argv(["pacman", "-Rnsu", *(extra_flags or []), "--", *names])
    subprocess.run(argv, check=True)


def _search(flag: str, term: str) -> str:
    """Run ``pacman <flag> --color always <term>``; return captured stdout.

    Forced colour preserves pacman's native rendering while capture lets the
    caller omit an empty section. Empty string on no match (exit != 0).
    """
    result = subprocess.run(
        ["pacman", flag, "--color", "always", term],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def search_local(term: str) -> str:
    """Installed packages matching ``term`` (``pacman -Qs``). Empty if none."""
    return _search("-Qs", term)


def search_repo(term: str) -> str:
    """Sync-DB packages matching ``term`` (``pacman -Ss``). Empty if none."""
    return _search("-Ss", term)


# ---------------------------------------------------------------------------
# Package queries
# ---------------------------------------------------------------------------

def get_installed_version(pkgname: str) -> str | None:
    """Run `pacman -Q pkgname`, return version string or None if not installed."""
    if _use_pyalpm():
        try:
            pkg = _get_alpm_handle().get_localdb().get_pkg(pkgname)
            return pkg.version if pkg else None
        except _alpm().error:
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
        except _alpm().error:
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


def get_installed_facts(root=None) -> dict[str, tuple[str, int | None]]:
    """Return ``{pkgname: (version, installed_size)}`` for all installed packages.

    One pass over the local DB. Installed size rides along free from the same
    pyalpm record (``pkg.isize``), so the change summary's size column costs no
    extra query. Falls back to ``pacman -Qi`` when pyalpm is unavailable.

    ``root`` is accepted for the target-root case (2.6.1-F27) but only the live
    root is supported today; a non-None value raises so a caller can never
    silently receive live-root data for a target root.
    """
    if root is not None:
        raise NotImplementedError("get_installed_facts(root=...) is not implemented yet")

    if _use_pyalpm():
        try:
            localdb = _get_alpm_handle().get_localdb()
            return {pkg.name: (pkg.version, pkg.isize) for pkg in localdb.pkgcache}
        except _alpm().error:
            pass

    result = subprocess.run(["pacman", "-Qi"], capture_output=True, text=True)
    if result.returncode != 0:
        # Unlike get_all_installed_packages()'s {} fallback, this is a diff
        # input: an installed Arch system with zero packages does not exist,
        # so an empty result here is unambiguously a read failure, not a
        # defensible "assume nothing". Raise so every caller — not just
        # change_report.snapshot() — gets an honest failure signal instead of
        # a silently empty dict that reads as "nothing installed".
        raise RuntimeError(
            f"pacman -Qi failed (exit {result.returncode}): "
            f"{result.stderr.strip() or 'no output'}"
        )
    return _parse_qi_facts(result.stdout)


def _parse_qi_facts(text: str) -> dict[str, tuple[str, int | None]]:
    """Parse ``pacman -Qi`` output into ``{name: (version, isize_bytes)}``.

    ``Installed Size`` is locale-formatted (e.g. ``142.30 MiB``); an
    unparseable value yields ``None`` rather than a guess, and the renderer
    then drops the size column entirely.
    """
    units = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    facts: dict[str, tuple[str, int | None]] = {}
    name = version = None
    isize: int | None = None
    for line in text.splitlines():
        if not line.strip():
            if name and version:
                facts[name] = (version, isize)
            name = version = None
            isize = None
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "Name":
            name = value
        elif key == "Version":
            version = value
        elif key == "Installed Size":
            parts = value.split()
            if len(parts) == 2 and parts[1] in units:
                try:
                    isize = int(float(parts[0].replace(",", "")) * units[parts[1]])
                except ValueError:
                    isize = None
    if name and version:
        facts[name] = (version, isize)
    return facts


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
        except _alpm().error:
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
        except _alpm().error:
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


# ---------------------------------------------------------------------------
# Repo candidate version — sync DB cross-checked against a refreshed side copy
# ---------------------------------------------------------------------------

# Process-lifetime memo of one ``checkupdates`` run. ``_MISS`` distinguishes
# "not probed yet" from "probed, tool unavailable" (which caches ``None``).
_MISS = object()
_checkupdates_memo: object = _MISS


def reset_repo_candidate_cache() -> None:
    """Drop the memoized ``checkupdates`` result (tests; long-lived processes)."""
    global _checkupdates_memo
    _checkupdates_memo = _MISS


def _memoized_checkupdates() -> dict[str, str] | None:
    global _checkupdates_memo
    if _checkupdates_memo is _MISS:
        _checkupdates_memo = checkupdates_map()
    return _checkupdates_memo  # type: ignore[return-value]


def get_repo_candidate_version(pkgname: str) -> str | None:
    """Return the newest repo version of ``pkgname``, tolerating a stale sync DB.

    ``get_pacman_sync_version`` reads ``/var/lib/pacman/sync/*.db`` and never
    touches the network, so on a rolling repo it reports whatever the last
    ``pacman -Sy`` left on disk — a day-old DB pins a day-old release (3.1.0-B3).
    ``checkupdates`` refreshes a *side copy* of the sync DBs (no sudo, no
    partial-upgrade risk), so it sees the true candidate; this takes whichever
    of the two is newer by ``vercmp``.

    ``checkupdates`` only lists *installed* packages with a pending upgrade, so
    it is a strict enrichment: absent entry, unavailable tool, or an older value
    all fall back to the sync DB. Returns ``None`` only when the sync DB has no
    candidate at all (the package is not in any repo). The ``checkupdates`` run
    is memoized for the process — one probe, however many packages are pinned.
    """
    from sysforge.primitives.version import vercmp  # avoid import cycle at module load

    sync_version = get_pacman_sync_version(pkgname)
    if sync_version is None:
        # Not in any sync DB — checkupdates cannot rescue that, and a caller
        # distinguishing "no candidate" must keep seeing None.
        return None

    updates = _memoized_checkupdates()
    if not updates:
        return sync_version
    candidate = updates.get(pkgname)
    if candidate is None:
        return sync_version
    try:
        newer = vercmp(candidate, sync_version) > 0
    except (OSError, ValueError) as e:
        _log.warn(f"{pkgname}: vercmp failed ({e}) — using sync-DB version")
        return sync_version
    if newer:
        _log.debug(
            f"{pkgname}: sync DB has {sync_version}, checkupdates has "
            f"{candidate} — sync DB is stale, using {candidate}"
        )
        return candidate
    return sync_version


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


_QO_LINE_RE = re.compile(r"^(?P<path>.+) is owned by (?P<pkg>\S+) \S+$")
_QO_NOTFOUND_RE = re.compile(r"^error: No package owns (?P<path>.+)$")


def owners_of(candidates: list[Path]) -> dict[Path, str | None]:
    """Map each path in *candidates* to its owning package name, or None.

    Contract (distinct from :func:`owners_of_paths`, which just omits
    unowned paths): a path present with a package name is owned; present
    with ``None`` is **definitively unowned** (pacman said so explicitly);
    a path **absent from the dict entirely** means ownership could not be
    determined (the ``pacman -Qo`` command failed wholesale, e.g. a locked
    or missing DB). Callers doing artifact-inventory subtraction must not
    collapse absent into ``None`` — that would present system files as
    user-authored artifacts.

    Deliberately does **not** take the ``_use_pyalpm()`` fast path: pyalpm
    exposes no reverse index, so answering these lookups through it means
    iterating every installed package's file list (~960k entries, ~0.33s)
    versus one batched subprocess (~0.09s at 300 paths, near-flat in N).
    ``_use_pyalpm()`` exists to avoid subprocess cost on queries pyalpm can
    answer directly; this is the one query shape it cannot. Do not "restore"
    the pyalpm path here. Same rationale applies to :func:`owners_of_paths`,
    which is implemented in terms of this function.
    """
    paths_in = [Path(p) for p in candidates]
    if not paths_in:
        return {}
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, paths are not shell-interpreted
            ["pacman", "-Qo", *[str(p) for p in paths_in]],
            capture_output=True, text=True, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        # Lookup unavailable wholesale — return {} (absent), never a dict of
        # Nones that would read as "confirmed unowned".
        return {}

    out: dict[Path, str | None] = {}
    for line in (proc.stdout or "").splitlines():
        m = _QO_LINE_RE.match(line.strip())
        if m:
            out[Path(m.group("path").rstrip("/"))] = m.group("pkg")
    for line in (proc.stderr or "").splitlines():
        m = _QO_NOTFOUND_RE.match(line.strip())
        if m:
            out[Path(m.group("path").rstrip("/"))] = None
    return out


def owners_of_paths(paths: list[str]) -> dict[str, str]:
    """Map each path to its owning package name (the reverse of
    :func:`get_package_files`). Paths with no owning package are absent from
    the result rather than raising.

    Thin wrapper over :func:`owners_of` (see its docstring for the batched
    ``pacman -Qo`` rationale and the deliberate pyalpm skip): keeps only the
    owned entries, dropping the unowned/undetermined distinction that
    :func:`owners_of` exposes.
    """
    if not paths:
        return {}
    result = owners_of([Path(p) for p in paths])
    return {str(p): pkg for p, pkg in result.items() if pkg is not None}


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
    return _desc_array(desc_path.read_text(), "%DEPENDS%")


def _desc_array(text: str, section: str) -> list[str]:
    """Return the entries of a local-db ``desc`` array section (``%DEPENDS%``…).

    The array runs until the first blank line or the next ``%SECTION%`` header.
    """
    values: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line == section:
            in_section = True
            continue
        if not in_section:
            continue
        if not line or line.startswith("%"):
            break
        values.append(line)
    return values


def get_all_package_depends(root: Path | None = None) -> dict[str, list[str]]:
    """Return ``{pkgname: %DEPENDS%}`` for every installed package, in ONE pass
    over the local DB.

    Reverse-dependency walks must use this rather than calling
    :func:`get_package_depends` per package: that resolves
    ``<pkgname>-<pkgver>-<pkgrel>`` by enumerating the DB root every time, so a
    whole-system walk costs O(N^2) directory reads (~5.5M stats on a
    2,349-package host — 2.6.1-B22). Unreadable entries are skipped, matching
    the per-package reader's degrade-to-empty contract.
    """
    db_root = root or _LOCAL_DB_ROOT
    if not db_root.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for entry in db_root.iterdir():
        # <pkgname>-<pkgver>-<pkgrel>; non-package files (ALPM_DB_VERSION) and
        # non-directories fall out on the read below rather than costing a stat.
        parts = entry.name.rsplit("-", 2)
        if len(parts) != 3:
            continue
        try:
            text = (entry / "desc").read_text()
        except OSError:
            continue
        out[parts[0]] = _desc_array(text, "%DEPENDS%")
    return out
