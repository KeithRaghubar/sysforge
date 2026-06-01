"""
build_core.py — the shared source-build pipeline behind ``build`` and ``update``.

``sysforge build`` and ``sysforge update`` historically carried two separate
dependency-handling + build loops, which drifted: ``build`` left the profile's
``-s``/``-i`` makepkg flags in place (so makepkg ran ``pacman -S`` on deps,
breaking on AUR-only packages) and never pre-installed repo makedeps. This
module is the single entry point both verbs route through, so ``build`` is a
strict subset of ``update``:

    update  → prepare_deps → build loop → install_built   (build_and_install)
              + version check, source-sync scheduling, pacman -Syu, summaries
    build   → prepare_deps → build loop → install_built   (build_and_install)
              + --cleansrc, LLVM preflight

The dependency-prep, per-package makepkg invocation (with ``BATCH_STRIP_FLAGS``
so makepkg never resolves deps itself), and deferred bulk install all live here.
``update`` keeps everything that is genuinely update-specific (the
``--install-only`` artifact scan, toolchain pre-flight, ``pacman -Syu``, and the
result summary) around these calls.

Public API:
    BuildTarget, BuildOutcome
    prepare_deps(pkgbuild_paths, config, ...)
    build_and_install(targets, ...) -> BuildOutcome
    install_built(built_pkg_files) -> tuple[list[Path], bool]
    _find_existing_artifacts(...)        # also consumed by update's install_only scan
    _record_build_failure(state_dir, target, exc)
"""
import time
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.version import vercmp
from sysforge.primitives.pacman import (
    BATCH_STRIP_FLAGS,
    BATCH_EXTRA_FLAGS,
    snapshot_pkg_dir,
    batch_install_pkgs,
    filter_pkgs_to_installed,
    collect_makedeps,
    filter_missing_deps,
    batch_install_makedeps,
    get_all_installed_packages,
)

_log = log.get_logger("BUILD")


# ---------------------------------------------------------------------------
# Lightweight target + outcome structs
# ---------------------------------------------------------------------------

@dataclass
class BuildTarget:
    """A single package to source-build.

    ``update._UpdateResult`` already exposes all of these attributes, so it can
    be passed to :func:`build_and_install` directly (duck-typed); ``BuildVerb``
    constructs ``BuildTarget`` from a parsed PKGBUILD.
    """
    pkgbase: str
    pkgnames: list[str]
    pkgbuild_path: Path
    source: str | None = None
    pkgbuild_ver: str | None = None
    installed_ver: str | None = None


@dataclass
class BuildOutcome:
    built_pkgs: list[str] = field(default_factory=list)
    failed_pkgs: list[str] = field(default_factory=list)
    pgo_skipped_pkgs: list[str] = field(default_factory=list)
    built_pkg_files: list[Path] = field(default_factory=list)
    install_failed: bool = False


def target_from_pkgbuild(pkgbuild_path) -> BuildTarget:
    """Build a :class:`BuildTarget` from a PKGBUILD path.

    Derives ``pkgbase``/``pkgnames`` with the same static parse the build
    worker uses (``makepkg_wrapper`` records build state the same way), so a
    split package yields every ``pkgname``. ``source`` is left ``None`` — the
    ``build`` verb has no version-check classification, and ``BuildState.record``
    keeps the prior provenance sticky when a caller passes ``None``.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    globals_ = parse_pkgbuild(pkgbuild_path).get("globals", {})
    pkgnames = globals_.get("pkgname", [])
    if isinstance(pkgnames, str):
        pkgnames = [pkgnames]
    pkgbase = globals_.get("pkgbase") or (pkgnames[0] if pkgnames else "unknown")
    return BuildTarget(
        pkgbase=pkgbase,
        pkgnames=list(pkgnames),
        pkgbuild_path=Path(pkgbuild_path),
    )


# ---------------------------------------------------------------------------
# Built-artifact discovery + failure recording
# (moved from update.py — both modules and the install_only scan use them)
# ---------------------------------------------------------------------------

def _find_existing_artifacts(
    search_dir: Path,
    pkgnames: list[str],
    pkgbuild_ver: str | None,
    installed_ver: str | None = None,
) -> list[Path]:
    """Locate already-built .pkg.tar artifacts matching pkgnames.

    Two-stage lookup:
      1. Strict glob ``{pkgname}-{pkgbuild_ver}-*.pkg.tar*`` — matches
         non-VCS packages where the static PKGBUILD parse equals the
         filename version exactly.
      2. Fallback ``{pkgname}-*-*-*.pkg.tar*`` + filename parse + vercmp
         to pick the newest. Required for VCS (-git/-svn/...) packages,
         where ``pkgver()`` bumps the version at build time
         (PKGBUILD ``pkgver=0.1.0`` → artifact ``0.1.0.r45.g1234567``)
         so the static ``pkgbuild_ver`` never matches the filename.

    If ``installed_ver`` is provided, the fallback only returns artifacts
    strictly newer than installed — used by ``--install-only`` to avoid
    redundant reinstalls or downgrades.
    """
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename

    if not search_dir or not Path(search_dir).is_dir():
        return []

    found: list[Path] = []
    for pkgname in pkgnames:
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

        candidates: list[tuple[str, Path]] = []
        for p in Path(search_dir).glob(f"{pkgname}-*-*-*.pkg.tar*"):
            if p.name.endswith(".sig"):
                continue
            parsed = _parse_built_pkg_filename(pkgname, p.name)
            if parsed is None:
                continue
            epoch, ver, rel = parsed
            ver_string = f"{epoch}:{ver}-{rel}" if epoch != "0" else f"{ver}-{rel}"
            if installed_ver is not None:
                try:
                    if vercmp(ver_string, installed_ver) <= 0:
                        continue
                except RuntimeError:
                    continue
            candidates.append((ver_string, p))

        if not candidates:
            continue

        try:
            candidates.sort(key=cmp_to_key(lambda a, b: vercmp(a[0], b[0])))
        except RuntimeError:
            pass
        found.append(candidates[-1][1])

    return found


def _record_build_failure(state_dir, target, exc) -> None:
    """Persist a build failure to build_state.toml's ``[failures]`` table.

    Opens a fresh BuildState so loop-time success writes (recorded by the
    build worker on disk) aren't clobbered. Pulls the diagnosis signature/fix
    from the exception's ``.diagnosis`` (attached by makepkg_wrapper) when
    present. Best-effort: recording must never turn a build failure into a
    crash. The entry is auto-cleared on the next successful build of the same
    pkgbase (see BuildState.record).
    """
    try:
        diagnosis = getattr(exc, "diagnosis", None) or []
        first = diagnosis[0] if diagnosis else None
        bs_fail = BuildState(state_dir)
        bs_fail.record_failure(
            target.pkgbase,
            error=str(exc),
            pkgver=getattr(target, "pkgbuild_ver", None),
            signature=getattr(first, "signature", None),
            fix_cmd=getattr(first, "fix_cmd", None),
        )
        bs_fail.save()
    except Exception as e:  # pragma: no cover - defensive
        _log.warn(f"Failed to record build failure for {target.pkgbase!r}: {e}")


# ---------------------------------------------------------------------------
# Dependency preparation
# ---------------------------------------------------------------------------

def prepare_deps(
    pkgbuild_paths: list[Path],
    config: dict,
    *,
    building_names: "frozenset[str] | set[str]" = frozenset(),
    profile_conf: str | None = None,
    cc: str | None = None,
    cxx: str | None = None,
    ld: str | None = None,
    state_dir: Path | None = None,
) -> None:
    """Pre-install missing repo makedeps, then resolve + build AUR/local deps.

    This is what frees the per-package makepkg invocation from having to sync
    deps itself (``-s`` is stripped below): every repo makedep is installed in
    one ``pacman -S`` transaction, and every AUR/local dependency is built and
    installed via :func:`build_resolved_deps`. ``building_names`` are the
    pkgbases we are about to build ourselves — excluded so we never try to
    resolve a target as its own dependency.

    Both arms are best-effort: a failure warns and lets the build proceed (a
    genuinely missing dep surfaces as a per-package build failure with a
    diagnosis, rather than aborting the whole batch up front).
    """
    if not pkgbuild_paths:
        return

    # Repo makedeps — one sudo transaction.
    makedeps = collect_makedeps(pkgbuild_paths)
    missing_deps = filter_missing_deps(makedeps)
    if missing_deps:
        try:
            batch_install_makedeps(missing_deps)
        except RuntimeError as e:
            _log.error(str(e))
            _log.ui(
                "[SYSFORGE] Warning: makedep pre-install failed — "
                "some builds may fail"
            )

    # AUR/local deps — resolve transitively, build in topo order.
    from sysforge.primitives.aur_resolve import (
        resolve_aur_deps_batch,
        build_resolved_deps,
    )
    try:
        aur_deps = resolve_aur_deps_batch(pkgbuild_paths, config, fetch=True)
        aur_deps = [d for d in aur_deps if d.name not in building_names]
        if aur_deps:
            build_resolved_deps(
                aur_deps,
                profile_conf=profile_conf,
                cc_override=cc,
                cxx_override=cxx,
                ld_override=ld,
                state_dir=state_dir,
            )
    except RuntimeError as e:
        _log.error(f"AUR dep resolution failed: {e}")
        _log.ui(
            "[SYSFORGE] Warning: AUR dep resolution failed — "
            "some builds may fail"
        )


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_built(built_pkg_files: list[Path]) -> tuple[list[Path], bool]:
    """Dedupe, filter to currently-installed pkgnames, and bulk ``pacman -U``.

    Returns ``(deduped_files, install_failed)``. The currently-installed set is
    re-fetched here because makedep + AUR-dep pre-install may have expanded it
    since the caller last looked. Split-pkgbase safety: makepkg emits one
    .pkg.tar per pkgname in the PKGBUILD, but we only install the sub-packages
    the user already has on the system.
    """
    seen: set = set()
    deduped: list[Path] = []
    for p in built_pkg_files:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    built_pkg_files = deduped

    install_failed = False
    if built_pkg_files:
        currently_installed = set(get_all_installed_packages().keys())
        built_pkg_files, dropped = filter_pkgs_to_installed(
            built_pkg_files, currently_installed
        )
        if dropped:
            _log.info(
                f"Skipping install of {len(dropped)} split sub-package(s) "
                "not currently on the system:"
            )
            for path, pn in dropped:
                _log.info(f"  - {pn} ({path.name})")

    if built_pkg_files:
        if not batch_install_pkgs(built_pkg_files):
            _log.error("Batch package install failed")
            _log.error("packages were built but not installed")
            install_failed = True

    return built_pkg_files, install_failed


# ---------------------------------------------------------------------------
# Build + install
# ---------------------------------------------------------------------------

def build_and_install(
    targets,
    *,
    config: dict,
    sync_source: bool,
    interactive: bool = False,
    no_cleanbuild: bool = False,
    profile_conf: str | None = None,
    cc: str | None = None,
    cxx: str | None = None,
    ld: str | None = None,
    state_dir: Path | None = None,
    pkg_log: bool = True,
    persist_log: bool = False,
    log_dir: Path | None = None,
    cache_report: bool = False,
    abi_check: bool = False,
    extra_flags: list | None = None,
    active_variant: str | None = None,
    pkgdest: Path | None = None,
) -> BuildOutcome:
    """Resolve deps, build every target, then bulk-install — the shared core.

    Each target's makepkg call runs with ``strip_flags=BATCH_STRIP_FLAGS``
    (drops ``-s``/``--syncdeps``/``-i``/``--install``) and ``force_batch`` when
    non-interactive, so makepkg never touches pacman — deps were already
    handled by :func:`prepare_deps`, and the built artifacts are installed in
    one transaction at the end.

    ``sync_source`` is the one deliberate caller difference: ``update`` passes
    ``False`` (Phase 2 already synced sources via the scheduler); ``build``
    passes ``not --no-update`` to keep its inline per-package source sync, which
    already routes through ``source_sync.get_scheduler()`` inside ``run()``.
    """
    outcome = BuildOutcome()
    if not targets:
        return outcome

    # Imported here (not at module top) so tests patching
    # ``sysforge.primitives.makepkg_wrapper.run`` are observed.
    from sysforge.primitives.makepkg_wrapper import (
        BuildOptions,
        run as build_run,
        PGOBuildSkipped,
        AlreadyBuilt,
    )
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    reset_session()

    pkgbuild_paths = [t.pkgbuild_path for t in targets if t.pkgbuild_path]
    building_names = {t.pkgbase for t in targets}
    prepare_deps(
        pkgbuild_paths,
        config,
        building_names=building_names,
        profile_conf=profile_conf,
        cc=cc,
        cxx=cxx,
        ld=ld,
        state_dir=state_dir,
    )

    # Cleanbuild (-C) prevents a stale $srcdir from a prior failed run causing
    # patch-already-applied errors in prepare(). When the caller opts out we
    # also strip -C/--cleanbuild so a user-passed flag can't re-add it.
    cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
    batch_flags = cleanbuild_flags + (extra_flags or [])
    strip_flags = (
        BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild
        else BATCH_STRIP_FLAGS
    )

    from sysforge.ui import progress as _ui_progress
    with _ui_progress.tracker(len(targets), "building") as _tick:
        for target in targets:
            _tick(target.pkgbase)
            search_dir = pkgdest if pkgdest else target.pkgbuild_path.parent
            build_start = time.time()
            try:
                build_run(target.pkgbuild_path, options=BuildOptions(
                    pkg_log=pkg_log,
                    persist_log=persist_log,
                    log_dir=log_dir,
                    profile_conf=profile_conf,
                    cc_override=cc,
                    cxx_override=cxx,
                    ld_override=ld,
                    cache_report=False,
                    abi_check=abi_check,
                    init_session=(
                        not outcome.built_pkgs and not outcome.failed_pkgs
                    ),
                    update=sync_source,
                    state_dir=state_dir,
                    extra_flags=batch_flags,
                    strip_flags=strip_flags,
                    interactive=interactive,
                    force_batch=not interactive,
                    source=target.source,
                    toolchain_variant=active_variant,
                ))
                new_pkgs = sorted(
                    p for p in snapshot_pkg_dir(search_dir)
                    if p.stat().st_mtime >= build_start
                )
                outcome.built_pkg_files.extend(new_pkgs)
                outcome.built_pkgs.append(target.pkgbase)
            except PGOBuildSkipped as e:
                _log.warn(str(e))
                outcome.pgo_skipped_pkgs.append(target.pkgbase)
            except AlreadyBuilt:
                existing = _find_existing_artifacts(
                    search_dir, target.pkgnames, target.pkgbuild_ver,
                )
                if existing:
                    _log.info(
                        f"{target.pkgbase}: package already built — "
                        "installing existing artifact"
                    )
                    outcome.built_pkg_files.extend(existing)
                    outcome.built_pkgs.append(target.pkgbase)
                else:
                    msg = (
                        f"makepkg reported already built but no matching "
                        f".pkg.tar found in {search_dir}"
                    )
                    _log.error(f"{target.pkgbase}: {msg}")
                    outcome.failed_pkgs.append(target.pkgbase)
                    _record_build_failure(state_dir, target, msg)
            except (RuntimeError, SystemExit) as e:
                _log.error(f"Build failed for {target.pkgbase!r}: {e}")
                outcome.failed_pkgs.append(target.pkgbase)
                _record_build_failure(state_dir, target, e)

    outcome.built_pkg_files, outcome.install_failed = install_built(
        outcome.built_pkg_files
    )

    if cache_report:
        emit_session_report()

    return outcome
