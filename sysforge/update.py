"""
update.py — check for and rebuild outdated sysforge-managed packages

Compares installed package versions against the latest PKGBUILD versions in
pkgbuild_src_dir (after git pull --rebase), then rebuilds packages where the
PKGBUILD is newer than what is installed.

VCS packages (-git, -svn, -hg, -bzr) cannot be compared by version because
pkgver is generated dynamically during the build. They are flagged as DEVEL
and only rebuilt when --devel is passed.

Scope: all packages listed in packages.toml. Packages with a build_state
record are eligible for automatic rebuild; packages without one are checked
and reported but only rebuilt when --all is passed.

--all mode:
    1. Discovers foreign packages via `pacman -Qm` that are not yet tracked
       in packages.toml. Each discovered package is classified (source,
       pkgbuild_patch), appended to packages.toml, and rebuilt if the
       PKGBUILD version is newer than what is installed. --dry-run shows what
       would be discovered and added without writing to packages.toml or
       building.
    2. Allows packages in packages.toml with no build_state record to be
       rebuilt (without --all they are checked but not built).

Phases:
    0. Init — load state, config, manifest, open unified log
    1. Package set assembly — merge manifest + build_state + discovery
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
    # Actions: UP_TO_DATE, NEEDS_REBUILD, NOT_INSTALLED, PULL_FAILED, DEVEL, DOWNGRADE
    action: str
    installed_ver: str | None
    pkgbuild_ver: str | None
    pkgbuild_path: Path | None
    has_build_record: bool = True
    discovered: bool = False


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# ---------------------------------------------------------------------------
# packages.toml loaders
# ---------------------------------------------------------------------------

def _load_packages_toml_names(path: Path) -> set[str]:
    """Return the set of package names already in packages.toml, or empty set."""
    if not path.exists():
        return set()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return {e.get("name", "") for e in data.get("package", [])}
    except Exception:
        return set()



def _load_full_packages_toml(path: Path) -> tuple[dict, list[dict]]:
    """Load packages.toml and return (build_config, package_entries).

    Returns ({}, []) if the file does not exist or cannot be parsed.
    """
    if not path.exists():
        return {}, []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        build_cfg = data.get("build", {})
        entries = [e for e in data.get("package", []) if "name" in e]
        return build_cfg, entries
    except Exception:
        return {}, []


def _append_to_packages_toml(path: Path, entries: list[dict]) -> None:
    """Append [[package]] blocks to packages.toml, creating the file if needed."""
    from sysforge.packages_cmd import entry_toml_block
    blocks = "".join("\n" + entry_toml_block(e) + "\n" for e in entries)
    if path.exists():
        with open(path, "a") as f:
            f.write(blocks)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# packages.toml — managed by sysforge packages\n"
            "\n[build]\n"
            'pkgbuild_src_dir = "~/src"\n'
        )
        path.write_text(header + blocks)


# ---------------------------------------------------------------------------
# Phase 1: Package set assembly
# ---------------------------------------------------------------------------

def _discover_new_packages(
    args, bs: BuildState, packages_path: Path,
) -> tuple[list[dict], list[str]]:
    """Discover foreign packages not yet tracked and append to packages.toml.

    Returns (entries_added, not_found_names) where entries_added are dicts
    with at least {name, source} suitable for merging into the unified
    packages dict.
    """
    foreign = get_foreign_packages()

    if not foreign:
        _log.info("--all: no foreign packages found")
        return [], []

    # Pacman-mode entries are superset bookkeeping, not build records, and
    # must not hide foreign packages from --all discovery. Treat missing
    # build_mode as legacy-profiled so older records aren't rediscovered.
    tracked = {
        name
        for name, entry in bs.all_packages().items()
        if entry.get("build_mode", "profiled") != "pacman"
    }
    in_manifest = _load_packages_toml_names(packages_path)

    new_foreign = {k: v for k, v in foreign.items() if k not in tracked and k not in in_manifest}

    if not new_foreign:
        _log.info("--all: no new foreign packages to discover")
        return [], []

    entries_to_add: list[dict] = []
    not_found: list[str] = []

    _log.info(f"--all: {len(new_foreign)} untracked foreign package(s) found")
    aur_results = aur_info(list(new_foreign.keys()))

    for pkgname in sorted(new_foreign):
        if pkgname not in aur_results:
            _log.warn(f"--all: {pkgname!r} not found in AUR — skipping")
            not_found.append(pkgname)
            continue

        pkgbuild_patch = False
        if not getattr(args, "dry_run", False):
            from sysforge.primitives.config import find_pkgbuild
            try:
                pkgbuild_path = find_pkgbuild(pkgname, {})
                if pkgbuild_path and pkgbuild_path.exists():
                    pkgmeta = parse_pkgbuild(pkgbuild_path)
                    from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                    pkgbuild_patch = bool(extract_pkgbuild_profile(pkgmeta, pkgbuild_path))
            except Exception as e:
                _log.warn(f"--all: {pkgname!r}: {e}")

        entry: dict = {"name": pkgname, "source": "aur"}
        if pkgbuild_patch:
            entry["pkgbuild_patch"] = True

        # Carry AUR PackageBase for split-package resolution
        aur_base = aur_results[pkgname].get("PackageBase")
        if aur_base and aur_base != pkgname:
            entry["pkgbase"] = aur_base

        entries_to_add.append(entry)

    if entries_to_add and not getattr(args, "dry_run", False):
        _append_to_packages_toml(packages_path, entries_to_add)
        _log.info(f"--all: appended {len(entries_to_add)} package(s) to {packages_path}")

    return entries_to_add, not_found


def _assemble_package_set(
    args, bs: BuildState, config: dict, packages_path: Path,
    build_cfg: dict, manifest_entries: list[dict],
) -> tuple[dict[str, dict], set[str], list[str]]:
    """Phase 1: build the unified {pkgname: entry} dict.

    Returns (packages, unrecorded_names, discovery_not_found).
    """
    manifest_by_name = {e["name"]: e for e in manifest_entries}
    build_state_pkgs = bs.all_packages()

    pkgbuild_src_dir_raw = (
        build_cfg.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )
    pkgbuild_src_dir_base = Path(pkgbuild_src_dir_raw).expanduser() if pkgbuild_src_dir_raw else None

    packages: dict[str, dict] = {}
    unrecorded_names: set[str] = set()

    # --- --all: discover truly new foreign packages ---
    discovery_not_found: list[str] = []
    if getattr(args, "all", False):
        discovered_entries, discovery_not_found = _discover_new_packages(args, bs, packages_path)
        for entry in discovered_entries:
            name = entry["name"]
            pkgbase = entry.get("pkgbase", name)
            pkgdir = str(pkgbuild_src_dir_base / pkgbase) if pkgbuild_src_dir_base else ""
            packages[name] = {
                "pkgbase": pkgbase,
                "pkgbuild_dir": pkgdir,
                "source": entry.get("source", "aur"),
                "discovered": True,
            }
            unrecorded_names.add(name)

    # Merge manifest entries with build_state. Pacman-mode markers are
    # superset bookkeeping only — they carry no pkgbuild_dir, so treat them
    # as unrecorded and synthesise a pkgbuild_src_dir_base / name path.
    # Missing build_mode defaults to profiled (legacy records pre-superset).
    for name, manifest_entry in manifest_by_name.items():
        if name in packages:
            continue  # Already added by discovery
        bs_entry = build_state_pkgs.get(name)
        if bs_entry is not None and bs_entry.get("build_mode", "profiled") != "pacman":
            pkg = bs_entry
            manifest_source = manifest_entry.get("source")
            if manifest_source and "source" not in pkg:
                pkg["source"] = manifest_source
            packages[name] = pkg
        else:
            unrecorded_names.add(name)
            pkgdir = str(pkgbuild_src_dir_base / name) if pkgbuild_src_dir_base else ""
            entry = {
                "pkgbase": name,
                "pkgbuild_dir": pkgdir,
            }
            manifest_source = manifest_entry.get("source")
            if manifest_source:
                entry["source"] = manifest_source
            packages[name] = entry

    # Resolve pkgbase for unrecorded AUR packages via AUR RPC so split packages
    # get the correct pkgbase and pkgbuild_dir (e.g. ob-xd-common → pkgbase ob-xd).
    offline = getattr(args, "offline", False)
    if unrecorded_names and pkgbuild_src_dir_base and not offline:
        aur_unrecorded = [n for n in unrecorded_names
                          if packages[n].get("source") != "repo"
                          and not packages[n].get("discovered")]  # already resolved
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
                _log.warn(f"{name}: not found in packages.toml — skipping")
        filter_set = set(filter_names)
        packages = {k: v for k, v in packages.items() if k in filter_set}

    return packages, unrecorded_names, discovery_not_found


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
    discovered = entry.get("discovered", False)

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
            has_build_record=has_record, discovered=discovered,
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

    # Check all pkgnames for split packages — installed if ANY sub-package is
    installed_ver = None
    for pn in pkgnames:
        ver = all_installed.get(pn)
        if ver is not None:
            installed_ver = ver
            break

    if installed_ver is None:
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="NOT_INSTALLED",
            installed_ver=None, pkgbuild_ver=pkgbuild_ver,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
            discovered=discovered,
        )

    # VCS packages: version is only meaningful after running pkgver()
    if _is_vcs(pkgbase):
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL",
            installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
            discovered=discovered,
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
        discovered=discovered,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""

    # ── Phase 0: Init ─────────────────────────────────────────────────────
    offline = getattr(args, "offline", False)
    if not offline:
        fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # Superset sync: build_state.toml carries an entry for every installed
    # package (pacman-mode marker for those sysforge didn't build), so that
    # every `pacman -Q` name has a known state and zombie entries left by
    # prior parser runs (e.g. literal ``$_pkgname`` keys) are pruned.
    try:
        sync_result = bs.sync_with_installed(get_all_installed_packages())
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
    build_cfg, manifest_entries = _load_full_packages_toml(packages_path)

    # ── Phase 1: Package set assembly ─────────────────────────────────────
    packages, unrecorded_names, discovery_not_found = _assemble_package_set(
        args, bs, config, packages_path, build_cfg, manifest_entries,
    )

    if not packages:
        print(
            "[SYSFORGE] No packages found in packages.toml or build state.\n"
            "Add packages with `sysforge packages add`, or use --all to discover foreign packages.",
            file=sys.stderr,
        )
        return

    pkgbase_map, pkgbase_entry = group_by_pkgbase(packages)

    # Print discovery summary (before source sync, so user sees what was found)
    if getattr(args, "all", False):
        _print_discovery_summary(packages, discovery_not_found)

    # ── Phase 2: Source sync ──────────────────────────────────────────────
    sync_failures = _sync_sources(pkgbase_map, pkgbase_entry, args)

    # ── Phase 3: Version check ──��─────────────────────────────────────────
    all_installed = get_all_installed_packages()
    skip_sync_check = offline
    results: list[_UpdateResult] = []

    # Authoritative pkgbuild versions for packages whose PKGBUILDs use bash
    # parameter expansion the static parser can't evaluate. The scheduler's
    # SourceMetaCache holds the latest AUR RPC Version per pkgbase.
    rpc_version_by_base = {
        pb: meta["rpc_version"]
        for pb, meta in get_scheduler().cache.all().items()
        if meta.get("rpc_version")
    }

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
    build_all = getattr(args, "all", False)
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"
                and (r.has_build_record or build_all or r.discovered)]
    if getattr(args, "devel", False):
        to_build += [r for r in results if r.action == "DEVEL"
                     and (r.has_build_record or build_all or r.discovered)]

    # Exclude packages that failed source sync (cleansrc refusal, etc.)
    cleansrc_failures = {k: v for k, v in sync_failures.items()
                         if "refusing to purge" in v}
    if sync_failures:
        to_build = [r for r in to_build if r.pkgbase not in sync_failures]

    if not to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    from sysforge.primitives.makepkg_wrapper import run as build_run, PGOBuildSkipped
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
    pkgdest = get_pkgdest()
    interactive = getattr(args, "interactive", False)
    no_cleanbuild = getattr(args, "no_cleanbuild", False)
    cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
    batch_flags = cleanbuild_flags + (extra_flags or [])
    strip_flags = BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild else BATCH_STRIP_FLAGS

    built_pkg_files: list = []
    built_pkgs: list[str] = []
    failed_pkgs: list[str] = []
    pgo_skipped_pkgs: list[str] = []

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

    install_failed = False
    if built_pkg_files:
        if not batch_install_pkgs(built_pkg_files):
            _log.error("Batch package install failed")
            _log.error("packages were built but not installed")
            install_failed = True
    elif built_pkgs:
        _log.warn("No .pkg.tar.* files found after builds — nothing to install")

    if getattr(args, "cache_report", False):
        emit_session_report()

    # Sync failures from cleansrc refusals count as build failures.
    failed_pkgs.extend(sorted(cleansrc_failures))

    skipped = len(results) - len(to_build)
    _log.ui((
        f"\n[SYSFORGE] Update complete: "
        f"{len(built_pkgs)} built, {len(failed_pkgs)} failed, {skipped} skipped"
        + (f", {len(pgo_skipped_pkgs)} pgo-skipped" if pgo_skipped_pkgs else "")
        + "."
    ))
    if built_pkgs:
        _log.ui(f"  Built:       {' '.join(built_pkgs)}")
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

def _print_discovery_summary(
    packages: dict[str, dict], not_found: list[str],
) -> None:
    """Print summary of --all discovery results."""
    discovered_pkgs = {k: v for k, v in packages.items() if v.get("discovered")}
    if not discovered_pkgs and not not_found:
        return

    print("\n  — --all discovery ��")

    if discovered_pkgs:
        devel_count = sum(1 for name in discovered_pkgs if _is_vcs(name))
        print(f"  [DISCOVERED]     {len(discovered_pkgs)} new package(s) added to packages.toml"
              + (f" ({devel_count} VCS)" if devel_count else ""))

    for name in not_found:
        print(f"  [NOT_FOUND]      {name} (not in AUR — skipped)")

    print()


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
    if counts.get("NOT_INSTALLED"):
        parts.append(f"{counts['NOT_INSTALLED']} not installed")
    if counts.get("DOWNGRADE"):
        parts.append(f"{counts['DOWNGRADE']} downgrade")
    if counts.get("PULL_FAILED"):
        parts.append(f"{counts['PULL_FAILED']} pull failed")
    if no_record_count:
        parts.append(f"{no_record_count} no build record")

    print(f"\n  Checking {', '.join(parts)}")
    print()

    has_unrecorded = False
    for r in results:
        star = ""
        if not r.has_build_record:
            star = " *"
            has_unrecorded = True
        disc = " (discovered)" if r.discovered else ""

        if r.action == "NEEDS_REBUILD":
            print(f"  [NEEDS_REBUILD]  {r.pkgbase}: {r.installed_ver} → {r.pkgbuild_ver}{star}{disc}")
        elif r.action == "UP_TO_DATE":
            print(f"  [UP_TO_DATE]     {r.pkgbase}: {r.pkgbuild_ver}{star}{disc}")
        elif r.action == "DEVEL":
            if getattr(args, "devel", False):
                print(f"  [DEVEL]          {r.pkgbase}: will rebuild (--devel){star}{disc}")
            else:
                print(f"  [DEVEL]          {r.pkgbase}: skipped (use --devel to rebuild){star}{disc}")
        elif r.action == "NOT_INSTALLED":
            print(f"  [NOT_INSTALLED]  {r.pkgbase}: {r.pkgbuild_ver} (not currently installed){star}{disc}")
        elif r.action == "DOWNGRADE":
            print(f"  [DOWNGRADE]      {r.pkgbase}: installed {r.installed_ver} > pkgbuild {r.pkgbuild_ver} (skipped){star}{disc}")
        elif r.action == "PULL_FAILED":
            print(f"  [PULL_FAILED]    {r.pkgbase}: git pull failed (skipped){star}{disc}")

    if has_unrecorded:
        print(f"\n  * = no build record (use --all to include in rebuild)")
    print()
