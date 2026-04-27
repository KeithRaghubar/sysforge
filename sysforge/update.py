"""
update.py — check for and rebuild outdated sysforge-managed packages

Iteration scope is the live install set: every installed AUR package
(`pacman -Qm`) plus repo packages selected by an override in packages.toml.
packages.toml entries apply as overrides where present (see
DESIGN.md §Package Manifest); installed packages with no entry use defaults.

Compares installed package versions against the latest PKGBUILD versions in
pkgbuild_src_dir (after source sync), then rebuilds packages where the
PKGBUILD is newer than what is installed.

VCS packages (-git, -svn, -hg, -bzr) cannot be compared by version because
pkgver is generated dynamically during the build. They are flagged as DEVEL
and only rebuilt when --devel is passed.

Phases:
    0. Init — load state, config, packages.toml overrides, open unified log
    1. Package set assembly — walk pacman -Qm + override-tagged repo
    2. Source sync — batched RPC + shallow fetch via SourceSyncScheduler
    3. Version check — parallel PKGBUILD parsing + vercmp
    4. Summary + dry-run gate
    5. Build — makedeps, AUR deps, single build loop
    6. Install + finalize

Public API:
    cmd_update(args)
"""
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
_log = log.get_logger("UPDATE")
from sysforge.primitives.build_state import BuildState, group_by_pkgbase
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.aur import fetch_aur_name_cache, aur_info
from sysforge.primitives.source_sync import (
    SyncRequest, SyncResult,
    STATUS_DIVERGED, STATUS_FAILED, STATUS_PURGE_REFUSED, STATUS_RATE_LIMITED,
    get_scheduler,
)
from sysforge.primitives.config import load_config, load_sysforge_toml
from sysforge.primitives.paths import resolve_packages_path
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags, BuildOptions
from sysforge.primitives.pacman import (
    BATCH_STRIP_FLAGS,
    BATCH_EXTRA_FLAGS,
    get_pkgdest,
    snapshot_pkg_dir,
    batch_install_pkgs,
    filter_pkgs_to_installed,
    collect_makedeps,
    filter_missing_deps,
    batch_install_makedeps,
    get_all_installed_packages,
    get_foreign_packages,
)
from sysforge.pipeline.state import resolve_state_dir


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")

# Sync statuses that block the package from proceeding to build.
_SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED,
})


@dataclass
class _UpdateResult:
    pkgbase: str
    pkgnames: list
    # Actions: UP_TO_DATE, NEEDS_REBUILD, PULL_FAILED, DEVEL, DOWNGRADE
    action: str
    installed_ver: str | None
    pkgbuild_ver: str | None
    pkgbuild_path: Path | None
    has_build_record: bool = True


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# ---------------------------------------------------------------------------
# packages.toml loader
# ---------------------------------------------------------------------------

def _load_overrides(path: Path) -> tuple[dict, dict[str, dict]]:
    """Load packages.toml and return (build_cfg, overrides_by_name).

    `overrides_by_name` is keyed by package name; each value is the raw
    [[package]] entry dict. Entries are *overrides* applied to the live
    install set — they do not declare what should be installed.

    Returns ({}, {}) if the file does not exist or cannot be parsed.
    """
    if not path.exists():
        return {}, {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        build_cfg = data.get("build", {})
        overrides = {e["name"]: e for e in data.get("package", []) if "name" in e}
        return build_cfg, overrides
    except Exception:
        return {}, {}


# ---------------------------------------------------------------------------
# Phase 1: Package set assembly
# ---------------------------------------------------------------------------

def _assemble_package_set(
    args, bs: BuildState, config: dict,
    build_cfg: dict, overrides_by_name: dict[str, dict],
) -> tuple[dict[str, dict], set[str]]:
    """Phase 1: build the unified {pkgname: entry} dict from the live install set.

    Iteration scope:
      - Every installed foreign package (`pacman -Qm`) — always.
      - Installed repo packages whose name appears in `overrides_by_name` —
        the override is what asks sysforge to manage them.

    `overrides_by_name` is applied as an overlay (`source`, `pkgbuild_patch`,
    `cache`, `reason`); installed packages with no override use defaults.
    Override entries whose package is not currently installed are inert
    rules and are not iterated.

    Returns (packages, unrecorded_names).
    """
    build_state_pkgs = bs.all_packages()

    pkgbuild_src_dir_raw = (
        build_cfg.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )
    pkgbuild_src_dir_base = Path(pkgbuild_src_dir_raw).expanduser() if pkgbuild_src_dir_raw else None

    foreign = set(get_foreign_packages().keys())
    repo_overridden = {
        name for name, ov in overrides_by_name.items()
        if ov.get("source") == "repo"
    }

    # Live install set: every installed foreign + every repo package with an
    # override that pulls it into sysforge's scope.
    all_installed = get_all_installed_packages()
    target_names = {n for n in all_installed if n in foreign or n in repo_overridden}

    packages: dict[str, dict] = {}
    unrecorded_names: set[str] = set()

    for name in target_names:
        override = overrides_by_name.get(name, {})
        bs_entry = build_state_pkgs.get(name)

        if bs_entry is not None and bs_entry.get("build_mode", "profiled") != "pacman":
            pkg = dict(bs_entry)
            override_source = override.get("source")
            if override_source and "source" not in pkg:
                pkg["source"] = override_source
            packages[name] = pkg
        else:
            unrecorded_names.add(name)
            pkgdir = str(pkgbuild_src_dir_base / name) if pkgbuild_src_dir_base else ""
            entry: dict = {
                "pkgbase": name,
                "pkgbuild_dir": pkgdir,
            }
            override_source = override.get("source")
            if override_source:
                entry["source"] = override_source
            packages[name] = entry

    # Resolve pkgbase for unrecorded AUR packages via AUR RPC so split packages
    # get the correct pkgbase and pkgbuild_dir (e.g. ob-xd-common → pkgbase ob-xd).
    offline = getattr(args, "offline", False)
    if unrecorded_names and pkgbuild_src_dir_base and not offline:
        aur_unrecorded = [n for n in unrecorded_names
                          if packages[n].get("source") != "repo"]
        if aur_unrecorded:
            aur_results = aur_info(aur_unrecorded)
            for name in aur_unrecorded:
                info = aur_results.get(name)
                if info and info.get("PackageBase") and info["PackageBase"] != name:
                    real_base = info["PackageBase"]
                    packages[name]["pkgbase"] = real_base
                    packages[name]["pkgbuild_dir"] = str(pkgbuild_src_dir_base / real_base)

    # Filter to specific packages when names are given on the command line
    filter_names: list[str] = getattr(args, "pkgnames", None) or []
    if filter_names:
        unknown = [n for n in filter_names if n not in packages]
        if unknown:
            for name in unknown:
                _log.warn(f"{name}: not in update scope (not installed, or repo package without an override) — skipping")
        filter_set = set(filter_names)
        packages = {k: v for k, v in packages.items() if k in filter_set}

    return packages, unrecorded_names


# ---------------------------------------------------------------------------
# Phase 2: Source sync (pull + clone + cleansrc + recovery)
# ---------------------------------------------------------------------------

def _sync_sources(
    pkgbase_map: dict[str, list],
    pkgbase_entry: dict[str, dict],
    args,
) -> dict[str, str]:
    """Ensure every package has an up-to-date local PKGBUILD.

    Delegates to ``SourceSyncScheduler``: one batched AUR RPC call, then
    sequential shallow fetches for pkgbases whose Version/LastModified/HEAD
    have drifted from ``source_meta.toml``. Returns
    ``{pkgbase: error_message}`` for packages that blocked on sync.
    """
    offline = getattr(args, "offline", False)
    dry_run = getattr(args, "dry_run", False)
    cleansrc = getattr(args, "cleansrc", False) and not dry_run
    force_devel = getattr(args, "devel", False)

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    sysforge_toml = load_sysforge_toml()
    git_cfg = sysforge_toml.get("git", {})
    aur_cfg = sysforge_toml.get("aur", {})
    # Accept legacy pull_timeout as an alias for fetch_timeout.
    fetch_timeout = git_cfg.get("fetch_timeout", git_cfg.get("pull_timeout", 30))
    clone_timeout = git_cfg.get("clone_timeout", 60)

    scheduler = get_scheduler(
        state_dir=state_dir,
        offline=offline,
        cleansrc=cleansrc,
        force_devel=force_devel,
        min_fetch_interval_ms=aur_cfg.get("min_fetch_interval_ms", 500),
        rate_limit_abort_s=aur_cfg.get("rate_limit_abort_s", 120.0),
        fetch_timeout=fetch_timeout,
        clone_timeout=clone_timeout,
    )

    if offline and not cleansrc:
        return {}

    reqs: list[SyncRequest] = []
    seen_dirs: set[str] = set()
    for pkgbase in sorted(pkgbase_map):
        entry = pkgbase_entry[pkgbase]
        source = entry.get("source", "aur")
        if source == "repo":
            continue
        pkgbuild_dir = Path(entry["pkgbuild_dir"])
        resolved = str(pkgbuild_dir)
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)
        reqs.append(SyncRequest(
            pkgbase=pkgbase, pkgbuild_dir=pkgbuild_dir, source=source,
        ))

    # Prime the RPC batch once so every subsequent request() can hit the
    # short-circuit path. Without this the scheduler only runs _ensure_rpc
    # inside sync_many(), and the per-request loop below would fetch every
    # package on every run.
    aur_bases = [r.pkgbase for r in reqs if r.source in ("aur", "git")]
    if aur_bases:
        scheduler._ensure_rpc(aur_bases)

    from sysforge.ui import progress as _ui_progress
    results: dict[str, SyncResult] = {}
    with _ui_progress.tracker(len(reqs), "source sync") as _tick:
        for req in reqs:
            _tick(req.pkgbase)
            results[req.pkgbase] = scheduler.request(req)

    # Summarise per-status counts once at INFO for operator visibility.
    by_status: dict[str, int] = {}
    for r in results.values():
        by_status[r.status] = by_status.get(r.status, 0) + 1
    if by_status:
        _log.info("source sync: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    scheduler.close()

    sync_failures: dict[str, str] = {}
    for pkgbase, result in results.items():
        if result.status in _SYNC_BLOCKING_STATUSES:
            sync_failures[pkgbase] = result.error or result.status
        elif result.status == STATUS_DIVERGED:
            # Divergence is not a hard failure: local PKGBUILD is kept; build
            # proceeds against it. Surface as a warning, not a blocker.
            _log.warn(
                f"{pkgbase}: {result.error or 'divergent upstream'} — "
                "build will use the local PKGBUILD; rerun with --cleansrc "
                "to discard local edits and re-clone"
            )
    return sync_failures


# ---------------------------------------------------------------------------
# Phase 3: Version check (called from ThreadPoolExecutor)
# ---------------------------------------------------------------------------

_UNRESOLVED_EXPANSION = re.compile(r"[${}]")


def _check_one_pkgbase(
    pkgbase: str,
    pkgnames: list[str],
    entry: dict,
    sync_failures: dict[str, str],
    all_installed: dict[str, str],
    unrecorded_names: set[str],
    skip_sync_check: bool,
    rpc_version_by_base: dict[str, str],
) -> _UpdateResult | None:
    """Check a single pkgbase and return an _UpdateResult, or None on skip."""
    pkgbuild_dir = Path(entry["pkgbuild_dir"])
    has_record = not any(pn in unrecorded_names for pn in pkgnames)

    if not pkgbuild_dir.is_dir():
        _log.warn(f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
        return None

    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        _log.warn(f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
        return None

    if not skip_sync_check and pkgbase in sync_failures:
        _log.error(sync_failures[pkgbase])
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="PULL_FAILED",
            installed_ver=None, pkgbuild_ver=None, pkgbuild_path=pkgbuild_path,
            has_build_record=has_record,
        )

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        _log.warn(f"{pkgbase}: failed to parse PKGBUILD: {e} — skipping")
        return None

    globals_ = pkgmeta.get("globals", {})
    pkgbuild_ver = format_version(globals_)

    # Static PKGBUILD parser can't evaluate bash parameter expansion
    # (${var//-/_}, ${var/[a-z]/.sfx}, etc.). When pkgver still contains
    # shell metacharacters, fall back to the AUR RPC version we already
    # cached in source_meta.toml — it's the authoritative released version
    # and is vercmp-ready (already includes pkgrel and any epoch prefix).
    if _UNRESOLVED_EXPANSION.search(pkgbuild_ver):
        rpc_ver = rpc_version_by_base.get(pkgbase)
        if rpc_ver:
            pkgbuild_ver = rpc_ver
        else:
            _log.warn(
                f"{pkgbase}: pkgver '{pkgbuild_ver}' has unresolved shell "
                "expansion and no cached RPC version — skipping"
            )
            return None

    # Live-install-set iteration guarantees every pkgbase reaching here has
    # at least one installed sub-package; pick that version for vercmp.
    installed_ver: str | None = None
    for pn in pkgnames:
        ver = all_installed.get(pn)
        if ver is not None:
            installed_ver = ver
            break
    assert installed_ver is not None, f"{pkgbase}: no installed pkgname in {pkgnames}"

    # VCS packages: version is only meaningful after running pkgver()
    if _is_vcs(pkgbase):
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL",
            installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
        )

    try:
        cmp = vercmp(pkgbuild_ver, installed_ver)
    except RuntimeError as e:
        _log.warn(f"{pkgbase}: version comparison failed: {e} — skipping")
        return None

    if cmp > 0:
        action = "NEEDS_REBUILD"
    elif cmp == 0:
        action = "UP_TO_DATE"
    else:
        action = "DOWNGRADE"
        _log.warn(f"{pkgbase}: PKGBUILD {pkgbuild_ver} is older than installed {installed_ver}")

    return _UpdateResult(
        pkgbase=pkgbase, pkgnames=pkgnames, action=action,
        installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
        pkgbuild_path=pkgbuild_path, has_build_record=has_record,
    )


# ---------------------------------------------------------------------------
# Phase 5 helpers
# ---------------------------------------------------------------------------

def _find_existing_artifacts(
    search_dir: Path,
    pkgnames: list[str],
    pkgbuild_ver: str | None,
    installed_ver: str | None = None,
) -> list[Path]:
    """Locate already-built .pkg.tar artifacts matching pkgnames.

    Two-stage lookup:
      1. Strict glob ``{pkgname}-{pkgbuild_ver}-*.pkg.tar.*`` — matches
         non-VCS packages where the static PKGBUILD parse equals the
         filename version exactly.
      2. Fallback ``{pkgname}-*-*-*.pkg.tar.*`` + filename parse + vercmp
         to pick the newest. Required for VCS (-git/-svn/...) packages,
         where ``pkgver()`` bumps the version at build time
         (PKGBUILD ``pkgver=0.1.0`` → artifact ``0.1.0.r45.g1234567``)
         so the static ``pkgbuild_ver`` never matches the filename.

    If ``installed_ver`` is provided, the fallback only returns artifacts
    strictly newer than installed — used by ``--install-only`` to avoid
    redundant reinstalls or downgrades.
    """
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename
    from functools import cmp_to_key

    if not search_dir or not Path(search_dir).is_dir():
        return []

    found: list[Path] = []
    for pkgname in pkgnames:
        if pkgbuild_ver:
            strict = [
                p for p in Path(search_dir).glob(
                    f"{pkgname}-{pkgbuild_ver}-*.pkg.tar.*"
                )
                if not p.name.endswith(".sig")
            ]
            if strict:
                found.extend(strict)
                continue

        candidates: list[tuple[str, Path]] = []
        for p in Path(search_dir).glob(f"{pkgname}-*-*-*.pkg.tar.*"):
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""

    # ── Phase 0: Init ─────────────────────────────────────────────────────
    install_only = getattr(args, "install_only", False)
    offline = getattr(args, "offline", False) or install_only
    if install_only:
        args.offline = True
    if not offline:
        fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # Superset sync: build_state.toml carries an entry for every installed
    # package (pacman-mode marker for those sysforge didn't build), so that
    # every `pacman -Q` name has a known state and zombie entries left by
    # prior parser runs (e.g. literal ``$_pkgname`` keys) are pruned.
    all_installed = get_all_installed_packages()
    try:
        sync_result = bs.sync_with_installed(all_installed)
    except OSError as e:
        _log.warn(f"build_state sync failed: {e}")
    else:
        if isinstance(sync_result, tuple) and len(sync_result) == 2:
            added, removed = sync_result
            if added or removed:
                bs.save()
                _log.info(f"build_state sync: +{added} pacman-mode, -{removed} stale")

    # Unified log — always on, always truncate.
    unified_log_active = not getattr(args, "dry_run", False)
    unified_log_path = (Path(args.log_dir) if getattr(args, "log_dir", None) else state_dir) / "sysforge-update.log"
    if unified_log_active:
        try:
            log.open_unified_log(unified_log_path, purge=True)
            _log.info(f"Unified log: {unified_log_path}")
        except OSError as e:
            unified_log_active = False
            _log.warn(f"Cannot write unified log to {unified_log_path}: {e} — logging to terminal only")

    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths) or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages

    packages_path = resolve_packages_path(config)
    build_cfg, overrides_by_name = _load_overrides(packages_path)

    # ── Phase 1: Package set assembly ─────────────────────────────────────
    packages, unrecorded_names = _assemble_package_set(
        args, bs, config, build_cfg, overrides_by_name,
    )

    if not packages:
        print(
            "[SYSFORGE] No installed packages in scope (no foreign packages, "
            "and no repo packages with overrides in packages.toml).",
            file=sys.stderr,
        )
        return

    pkgbase_map, pkgbase_entry = group_by_pkgbase(packages)

    # Authoritative pkgbuild versions for packages whose PKGBUILDs use bash
    # parameter expansion the static parser can't evaluate. The scheduler's
    # SourceMetaCache holds the latest AUR RPC Version per pkgbase.
    rpc_version_by_base = {
        pb: meta["rpc_version"]
        for pb, meta in get_scheduler().cache.all().items()
        if meta.get("rpc_version")
    }

    # ── Phase 2: Source sync ──────────────────────────────────────────────
    sync_failures = _sync_sources(pkgbase_map, pkgbase_entry, args)

    # ── Phase 3: Version check ────────────────────────────────────────────
    skip_sync_check = offline
    results: list[_UpdateResult] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _check_one_pkgbase, pkgbase, pkgnames,
                pkgbase_entry[pkgbase], sync_failures, all_installed,
                unrecorded_names, skip_sync_check, rpc_version_by_base,
            ): pkgbase
            for pkgbase, pkgnames in sorted(pkgbase_map.items())
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                results.append(result)

    results.sort(key=lambda r: r.pkgbase)

    # ── Phase 4: Summary + dry-run gate ───────────────────────────────────
    _print_summary(results, args)

    if getattr(args, "dry_run", False):
        return

    # ── Phase 5: Build ────────────────────────────────────────────────────
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"]
    if getattr(args, "devel", False):
        to_build += [r for r in results if r.action == "DEVEL"]

    # Exclude packages that failed source sync (cleansrc refusal, etc.)
    cleansrc_failures = {k: v for k, v in sync_failures.items()
                         if "refusing to purge" in v}
    if sync_failures:
        to_build = [r for r in to_build if r.pkgbase not in sync_failures]

    if not to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    pkgdest = get_pkgdest()
    built_pkg_files: list = []
    built_pkgs: list[str] = []
    failed_pkgs: list[str] = []
    pgo_skipped_pkgs: list[str] = []

    if install_only:
        # Skip the whole build loop: no makedep batching, no AUR-dep resolution,
        # no makepkg invocation. For each result the version-check filter has
        # already proved is newer than installed, look for a matching artifact
        # at exactly that pkgbuild_ver in PKGDEST and queue it for install.
        from sysforge.ui import progress as _ui_progress
        with _ui_progress.tracker(len(to_build), "scanning") as _tick:
            for result in to_build:
                _tick(result.pkgbase)
                search_dir = pkgdest if pkgdest else (
                    result.pkgbuild_path.parent if result.pkgbuild_path else None
                )
                existing = _find_existing_artifacts(
                    search_dir, result.pkgnames, result.pkgbuild_ver,
                    installed_ver=result.installed_ver,
                ) if search_dir else []
                if existing:
                    _log.info(
                        f"{result.pkgbase}: queuing pre-built artifact "
                        f"({existing[0].name})"
                    )
                    built_pkg_files.extend(existing)
                    built_pkgs.append(result.pkgbase)
                else:
                    _log.info(
                        f"{result.pkgbase}: [SKIP] no built artifact for "
                        f"{result.pkgbuild_ver} in {search_dir}"
                    )
        # Phase 6 (install) handles the rest.
    else:
        from sysforge.primitives.makepkg_wrapper import (
            run as build_run, PGOBuildSkipped, AlreadyBuilt,
        )
        from sysforge.primitives.cache_probe import reset_session, emit_session_report
        reset_session()

        extra_flags = expand_makepkg_flags(args.makepkg) if getattr(args, "makepkg", None) else None

        # Batch pre-install all makedepends (one sudo call)
        all_pkgbuild_paths = [r.pkgbuild_path for r in to_build if r.pkgbuild_path]
        makedeps = collect_makedeps(all_pkgbuild_paths)
        missing_deps = filter_missing_deps(makedeps)
        if missing_deps:
            try:
                batch_install_makedeps(missing_deps)
            except RuntimeError as e:
                _log.error(str(e))
                print("[SYSFORGE] Warning: makedep pre-install failed — some builds may fail", file=sys.stderr)

        # Resolve and build AUR-only deps
        from sysforge.primitives.aur_resolve import resolve_aur_deps_batch, build_resolved_deps
        if all_pkgbuild_paths:
            try:
                aur_deps = resolve_aur_deps_batch(all_pkgbuild_paths, config, fetch=True)
                building_names = {r.pkgbase for r in to_build}
                aur_deps = [d for d in aur_deps if d.name not in building_names]
                if aur_deps:
                    build_resolved_deps(aur_deps)
            except RuntimeError as e:
                _log.error(f"AUR dep resolution failed: {e}")
                print("[SYSFORGE] Warning: AUR dep resolution failed — some builds may fail", file=sys.stderr)

        # Build all packages
        interactive = getattr(args, "interactive", False)
        no_cleanbuild = getattr(args, "no_cleanbuild", False)
        cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
        batch_flags = cleanbuild_flags + (extra_flags or [])
        strip_flags = BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild else BATCH_STRIP_FLAGS

        from sysforge.ui import progress as _ui_progress
        with _ui_progress.tracker(len(to_build), "building") as _tick:
            for result in to_build:
                _tick(result.pkgbase)
                search_dir = pkgdest if pkgdest else result.pkgbuild_path.parent
                build_start = time.time()
                try:
                    build_run(result.pkgbuild_path, options=BuildOptions(
                        pkg_log=not getattr(args, "no_pkg_log", False),
                        persist_log=getattr(args, "persist_log", False),
                        log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
                        profile_conf=getattr(args, "profile_conf", None),
                        cache_report=False,
                        init_session=(not built_pkgs and not failed_pkgs),
                        update=False,  # source sync already done
                        state_dir=Path(args.state_dir) if getattr(args, "state_dir", None) else None,
                        extra_flags=batch_flags,
                        strip_flags=strip_flags,
                        interactive=interactive,
                        force_batch=not interactive,
                    ))
                    new_pkgs = sorted(
                        p for p in snapshot_pkg_dir(search_dir)
                        if p.stat().st_mtime >= build_start
                    )
                    built_pkg_files.extend(new_pkgs)
                    built_pkgs.append(result.pkgbase)
                except PGOBuildSkipped as e:
                    _log.warn(str(e))
                    pgo_skipped_pkgs.append(result.pkgbase)
                except AlreadyBuilt:
                    existing = _find_existing_artifacts(
                        search_dir, result.pkgnames, result.pkgbuild_ver,
                    )
                    if existing:
                        _log.info(
                            f"{result.pkgbase}: package already built — "
                            "installing existing artifact"
                        )
                        built_pkg_files.extend(existing)
                        built_pkgs.append(result.pkgbase)
                    else:
                        _log.error(
                            f"{result.pkgbase}: makepkg reported already built "
                            f"but no matching .pkg.tar found in {search_dir}"
                        )
                        failed_pkgs.append(result.pkgbase)
                except (RuntimeError, SystemExit) as e:
                    _log.error(f"Build failed for {result.pkgbase!r}: {e}")
                    failed_pkgs.append(result.pkgbase)

    # ── Phase 6: Install + finalize ───────────────────────────────────────

    # Deduplicate while preserving order
    seen: set = set()
    deduped: list = []
    for p in built_pkg_files:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    built_pkg_files = deduped

    # Split-pkgbase safety: makepkg emits one .pkg.tar for every pkgname in
    # the PKGBUILD, not just the ones the user has installed. Filter to
    # installed pkgnames so rebuilding e.g. pipewire-full-git doesn't pull
    # in 14 split sub-packages the user never chose. Refetch now — makedep
    # and AUR-dep pre-install above may have expanded the installed set.
    install_failed = False
    if built_pkg_files:
        currently_installed = set(get_all_installed_packages().keys())
        built_pkg_files, dropped = filter_pkgs_to_installed(built_pkg_files, currently_installed)
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
    elif built_pkgs:
        _log.warn("No .pkg.tar.* files eligible to install — nothing to do")

    if not install_only and getattr(args, "cache_report", False):
        from sysforge.primitives.cache_probe import emit_session_report
        emit_session_report()

    # Sync failures from cleansrc refusals count as build failures.
    failed_pkgs.extend(sorted(cleansrc_failures))

    skipped = len(results) - len(to_build)
    if install_only:
        skipped += len(to_build) - len(built_pkgs) - len(failed_pkgs)
    built_label = "installed" if install_only else "built"
    _log.ui((
        f"\n[SYSFORGE] Update complete: "
        f"{len(built_pkgs)} {built_label}, {len(failed_pkgs)} failed, {skipped} skipped"
        + (f", {len(pgo_skipped_pkgs)} pgo-skipped" if pgo_skipped_pkgs else "")
        + "."
    ))
    if built_pkgs:
        _label = "Installed:" if install_only else "Built:"
        _log.ui(f"  {_label:<13}{' '.join(built_pkgs)}")
    if failed_pkgs:
        _log.ui(f"  Failed:      {' '.join(failed_pkgs)}")
    if cleansrc_failures:
        _log.ui(
            f"  --cleansrc refused {len(cleansrc_failures)} package(s) with local work; "
            "commit/push or resolve manually before retrying."
        )
    if pgo_skipped_pkgs:
        _log.ui(
            f"  PGO-skipped: {' '.join(pgo_skipped_pkgs)}"
            " (run 'sysforge run toolchain' to rebuild profdata)"
        )

    if unified_log_active:
        log.close_unified_log(success=(not failed_pkgs and not install_failed), persist=True)
        _log.ui(f"[SYSFORGE] Unified log: {unified_log_path}")


# ---------------------------------------------------------------------------
# Summary display
# ---------------------------------------------------------------------------

def _print_summary(results: list[_UpdateResult], args) -> None:
    if not results:
        print("[SYSFORGE] No packages to check.")
        return

    # Totals header
    counts: dict[str, int] = {}
    no_record_count = 0
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
        if not r.has_build_record:
            no_record_count += 1

    parts = [f"{len(results)} packages"]
    if counts.get("UP_TO_DATE"):
        parts.append(f"{counts['UP_TO_DATE']} up to date")
    if counts.get("NEEDS_REBUILD"):
        parts.append(f"{counts['NEEDS_REBUILD']} need rebuild")
    if counts.get("DEVEL"):
        parts.append(f"{counts['DEVEL']} devel")
    if counts.get("DOWNGRADE"):
        parts.append(f"{counts['DOWNGRADE']} downgrade")
    if counts.get("PULL_FAILED"):
        parts.append(f"{counts['PULL_FAILED']} pull failed")
    if no_record_count:
        parts.append(f"{no_record_count} no build record")

    print(f"\n  Checking {', '.join(parts)}")
    print()

    for r in results:
        star = " *" if not r.has_build_record else ""

        if r.action == "NEEDS_REBUILD":
            print(f"  [NEEDS_REBUILD]  {r.pkgbase}: {r.installed_ver} → {r.pkgbuild_ver}{star}")
        elif r.action == "UP_TO_DATE":
            print(f"  [UP_TO_DATE]     {r.pkgbase}: {r.pkgbuild_ver}{star}")
        elif r.action == "DEVEL":
            if getattr(args, "devel", False):
                print(f"  [DEVEL]          {r.pkgbase}: will rebuild (--devel){star}")
            else:
                print(f"  [DEVEL]          {r.pkgbase}: skipped (use --devel to rebuild){star}")
        elif r.action == "DOWNGRADE":
            print(f"  [DOWNGRADE]      {r.pkgbase}: installed {r.installed_ver} > pkgbuild {r.pkgbuild_ver} (skipped){star}")
        elif r.action == "PULL_FAILED":
            print(f"  [PULL_FAILED]    {r.pkgbase}: git pull failed (skipped){star}")

    if no_record_count:
        print(f"\n  * = no build record")
    print()
