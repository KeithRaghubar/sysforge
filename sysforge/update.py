"""
update.py — check for and rebuild outdated sysforge-managed packages

Compares installed package versions against the latest PKGBUILD versions in
pkgbuild_dir (after git pull --rebase), then rebuilds packages where the
PKGBUILD is newer than what is installed.

VCS packages (-git, -svn, -hg, -bzr) cannot be compared by version because
pkgver is generated dynamically during the build. They are flagged as DEVEL
and only rebuilt when --devel is passed.

Scope: only sysforge-managed packages recorded in build_state.toml.
Repo packages (installed via pacman -S) are out of scope — use pacman -Syu.

--all mode:
    Discovers foreign packages via `pacman -Qm` that are not yet tracked in
    build_state.toml or packages.toml. Each discovered package is classified
    (source, pkgbuild_patch), appended to packages.toml, and rebuilt if the
    PKGBUILD version is newer than what is installed. --dry-run shows what
    would be discovered and added without writing to packages.toml or building.

Public API:
    cmd_update(args)
"""
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
_log = log.get_logger("UPDATE")
from sysforge.primitives.build_state import BuildState, group_by_pkgbase
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.aur import git_pull_rebase, fetch_aur_name_cache
from sysforge.primitives.config import PACKAGES_PATH, load_config
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
    get_installed_version,
    get_all_installed_packages,
    get_foreign_packages,
    get_pacman_sync_version,
)
from sysforge.pipeline.state import resolve_state_dir


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")


@dataclass
class _UpdateResult:
    pkgbase: str
    pkgnames: list
    # Actions: UP_TO_DATE, NEEDS_REBUILD, NOT_INSTALLED, PULL_FAILED, DEVEL, DOWNGRADE
    action: str
    installed_ver: str | None
    pkgbuild_ver: str | None
    pkgbuild_path: Path | None


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# ---------------------------------------------------------------------------
# --all: foreign package discovery
# ---------------------------------------------------------------------------

@dataclass
class _DiscoveredResult:
    pkgname: str
    # ADDED, OUTDATED, DEVEL, NOT_FOUND, ALREADY_TRACKED
    action: str
    installed_ver: str | None = None
    pkgbuild_ver: str | None = None
    pkgbuild_path: Path | None = None


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


def _load_packages_toml_sources(path: Path) -> dict[str, str]:
    """Return {name: source} for all entries in packages.toml."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return {e["name"]: e.get("source", "aur") for e in data.get("package", []) if "name" in e}
    except Exception:
        return {}


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
            'pkgbuild_dir = "~/builds"\n'
        )
        path.write_text(header + blocks)


def _discover_and_add(args, bs: BuildState, config: dict) -> list[_DiscoveredResult]:
    """
    Discover foreign packages not yet tracked by sysforge, and pick up packages
    that are in packages.toml but have no build_state record (e.g. state was never
    written due to a missing state directory).

    1. Run pacman -Qm to find all foreign packages.
    2a. New packages (not in build_state or packages.toml): AUR-verify, add to
        packages.toml, compare versions, schedule rebuild if outdated.
    2b. Unrecorded packages (in packages.toml but not in build_state): find
        PKGBUILD, compare versions, schedule rebuild if outdated. Not re-added
        to packages.toml.

    Returns list of _DiscoveredResult for summary display.
    """
    from sysforge.primitives.aur import aur_info
    from sysforge.primitives.config import find_pkgbuild

    packages_path = Path(config.get("packages_file") or PACKAGES_PATH)

    foreign = get_foreign_packages()
    all_installed = get_all_installed_packages()

    if not foreign and not all_installed:
        _log.info("--all: no installed packages found")
        return []

    tracked = set(bs.all_packages().keys())
    in_manifest = _load_packages_toml_names(packages_path)

    # Phase 1: foreign packages not yet in packages.toml (discovery)
    new_foreign = {k: v for k, v in foreign.items() if k not in tracked and k not in in_manifest}
    # Phase 2: packages in packages.toml that are installed (AUR or repo) but have no build record
    unrecorded = {k: v for k, v in all_installed.items() if k in in_manifest and k not in tracked}

    if not new_foreign and not unrecorded:
        _log.info("--all: all manifest packages are already tracked")
        return []

    results: list[_DiscoveredResult] = []
    entries_to_add: list[dict] = []

    # --- Phase 1: truly new packages (not in packages.toml) ---
    if new_foreign:
        _log.info(f"--all: {len(new_foreign)} untracked foreign package(s) found")
        aur_results = aur_info(list(new_foreign.keys()))

        for pkgname in sorted(new_foreign):
            installed_ver = new_foreign[pkgname]

            if pkgname not in aur_results:
                _log.warn(f"--all: {pkgname!r} not found in AUR — skipping")
                results.append(_DiscoveredResult(
                    pkgname=pkgname, action="NOT_FOUND",
                    installed_ver=installed_ver,
                ))
                continue

            pkgbuild_path = None
            pkgbuild_ver = None
            pkgbuild_patch = False

            if not getattr(args, "dry_run", False):
                try:
                    pkgbuild_path = find_pkgbuild(pkgname, config)
                except (FileNotFoundError, RuntimeError) as e:
                    _log.warn(f"--all: {pkgname!r}: {e}")

                if pkgbuild_path and pkgbuild_path.exists():
                    try:
                        pkgmeta = parse_pkgbuild(pkgbuild_path)
                        pkgbuild_ver = format_version(pkgmeta.get("globals", {}))
                        from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                        pkgbuild_patch = bool(extract_pkgbuild_profile(pkgmeta, pkgbuild_path))
                    except Exception as e:
                        _log.warn(f"--all: {pkgname!r}: failed to parse PKGBUILD: {e}")

            entry: dict = {"name": pkgname, "source": "aur"}
            if pkgbuild_patch:
                entry["pkgbuild_patch"] = True
            entries_to_add.append(entry)

            if _is_vcs(pkgname):
                action = "DEVEL"
            elif pkgbuild_ver is not None:
                try:
                    cmp = vercmp(pkgbuild_ver, installed_ver)
                    action = "OUTDATED" if cmp > 0 else "ADDED"
                except RuntimeError:
                    action = "ADDED"
            else:
                action = "ADDED"

            results.append(_DiscoveredResult(
                pkgname=pkgname, action=action,
                installed_ver=installed_ver,
                pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path,
            ))

    if entries_to_add and not getattr(args, "dry_run", False):
        _append_to_packages_toml(packages_path, entries_to_add)
        _log.info(f"--all: appended {len(entries_to_add)} package(s) to {packages_path}")

    # --- Phase 2: packages in packages.toml with no build_state record ---
    if unrecorded:
        _log.info(
                  f"--all: {len(unrecorded)} package(s) in packages.toml with no build record — checking versions")

        pkgbuild_dir_raw = config.get("paths", {}).get("pkgbuild_dir")
        pkgbuild_dir = Path(pkgbuild_dir_raw).expanduser() if pkgbuild_dir_raw else None
        manifest_sources = _load_packages_toml_sources(packages_path)

        # Separate packages into those with a local PKGBUILD and those needing a DB lookup.
        # Don't auto-clone during update — pkgctl_checkout requires GITLAB_TOKEN and falls back
        # to SSH on failure, which prompts for credentials.
        has_local = {k: v for k, v in unrecorded.items()
                     if pkgbuild_dir and (pkgbuild_dir / k / "PKGBUILD").exists()}
        needs_db  = {k: v for k, v in unrecorded.items() if k not in has_local}

        # Batch AUR RPC for unrecorded non-VCS AUR packages without a local PKGBUILD.
        aur_to_check = [k for k in needs_db
                        if manifest_sources.get(k, "aur") == "aur" and not _is_vcs(k)]
        aur_db_versions: dict[str, str] = {}
        if aur_to_check:
            aur_results = aur_info(aur_to_check)
            aur_db_versions = {name: d["Version"] for name, d in aur_results.items() if d.get("Version")}

        # Process packages without a local PKGBUILD using package DB version.
        for pkgname in sorted(needs_db):
            installed_ver = needs_db[pkgname]

            if _is_vcs(pkgname):
                results.append(_DiscoveredResult(pkgname=pkgname, action="DEVEL",
                    installed_ver=installed_ver, pkgbuild_ver=None, pkgbuild_path=None))
                continue

            source = manifest_sources.get(pkgname, "aur")
            if source == "repo":
                db_ver = get_pacman_sync_version(pkgname)
            else:
                db_ver = aur_db_versions.get(pkgname)

            if not db_ver:
                results.append(_DiscoveredResult(pkgname=pkgname, action="NOT_FOUND",
                    installed_ver=installed_ver, pkgbuild_ver=None, pkgbuild_path=None))
                continue

            try:
                cmp = vercmp(db_ver, installed_ver)
                action = "OUTDATED" if cmp > 0 else "ADDED"
            except RuntimeError:
                action = "OUTDATED"

            results.append(_DiscoveredResult(pkgname=pkgname, action=action,
                installed_ver=installed_ver, pkgbuild_ver=db_ver, pkgbuild_path=None))

        # Process packages with local PKGBUILDs: git pull then compare PKGBUILD version.
        for pkgname in sorted(has_local):
            installed_ver = has_local[pkgname]
            pkgbuild_path = None
            pkgbuild_ver = None

            if not getattr(args, "dry_run", False):
                try:
                    pkgbuild_path = find_pkgbuild(pkgname, config)
                except (FileNotFoundError, RuntimeError) as e:
                    _log.warn(f"--all: {pkgname!r}: {e}")

                if pkgbuild_path and pkgbuild_path.exists():
                    # Pull before comparing so we see the latest AUR version,
                    # not the version from the last time this package was built.
                    if not getattr(args, "no_update", False):
                        try:
                            git_pull_rebase(pkgbuild_path.parent)
                        except RuntimeError as e:
                            _log.warn(f"--all: {pkgname!r}: git pull failed: {e}")
                    try:
                        pkgmeta = parse_pkgbuild(pkgbuild_path)
                        pkgbuild_ver = format_version(pkgmeta.get("globals", {}))
                    except Exception as e:
                        _log.warn(f"--all: {pkgname!r}: failed to parse PKGBUILD: {e}")

            # If pkgbuild_ver contains unexpanded bash (e.g. ${_ver/[a-z]/...}),
            # treat it as unparseable so we fall through to the "can't compare" case.
            if pkgbuild_ver and ('$' in pkgbuild_ver or '{' in pkgbuild_ver):
                _log.warn(f"--all: {pkgname!r}: pkgver contains unexpanded bash "
                          f"({pkgbuild_ver!r}), treating as unresolvable")
                pkgbuild_ver = None

            if _is_vcs(pkgname):
                action = "DEVEL"
            elif pkgbuild_ver is not None:
                try:
                    cmp = vercmp(pkgbuild_ver, installed_ver)
                    action = "OUTDATED" if cmp > 0 else "ADDED"
                except RuntimeError:
                    action = "OUTDATED"  # no record, treat as needing a build
            else:
                action = "OUTDATED"  # can't compare, schedule a build to record state

            results.append(_DiscoveredResult(
                pkgname=pkgname, action=action,
                installed_ver=installed_ver,
                pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path,
            ))

    return results


def _print_discovery_summary(results: list[_DiscoveredResult], args) -> None:
    if not results:
        return

    outdated  = [r for r in results if r.action == "OUTDATED"]
    devel     = [r for r in results if r.action == "DEVEL"]
    not_found = [r for r in results if r.action == "NOT_FOUND"]
    added     = [r for r in results if r.action == "ADDED"]

    print("\n  — --all discovery summary —")

    for r in outdated:
        ver_str = f"{r.installed_ver} → {r.pkgbuild_ver}" if r.pkgbuild_ver else r.installed_ver
        print(f"  [OUTDATED]       {r.pkgname}: {ver_str}")

    for r in not_found:
        print(f"  [NOT_FOUND]      {r.pkgname}: {r.installed_ver} (not in AUR — skipped)")

    if devel:
        flag = "will rebuild" if getattr(args, "devel", False) else "use --devel to rebuild"
        print(f"  [DEVEL]          {len(devel)} VCS package(s) skipped ({flag})")

    if added:
        print(f"  [UP_TO_DATE]     {len(added)} package(s) already current")

    print()


def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""
    # Refresh the AUR name cache as a side effect; failures are non-fatal
    fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # Open the unified log early so discovery messages are captured.
    unified_log_active = not getattr(args, "no_unified_log", False) and not getattr(args, "dry_run", False)
    unified_log_path = (Path(args.log_dir) if getattr(args, "log_dir", None) else state_dir) / "sysforge-update.log"
    if unified_log_active:
        try:
            log.open_unified_log(unified_log_path, purge=getattr(args, "purge_log", False))
            _log.info(f"Unified log: {unified_log_path}")
        except OSError as e:
            unified_log_active = False
            _log.warn(f"Cannot write unified log to {unified_log_path}: {e} — logging to terminal only")

    # --all: discover and classify foreign packages before the main update loop
    discover_config: dict = {}
    discovered: list[_DiscoveredResult] = []
    if getattr(args, "all", False):
        config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
        discover_config = load_config(config_paths=config_paths) or {}
        if getattr(args, "packages", None):
            discover_config["packages_file"] = args.packages
        discovered = _discover_and_add(args, bs, discover_config)
        _print_discovery_summary(discovered, args)

    packages = bs.all_packages()

    # Filter to specific packages when names are given on the command line
    filter_names: list[str] = getattr(args, "pkgnames", None) or []
    if filter_names:
        unknown = [n for n in filter_names if n not in packages]
        if unknown:
            for name in unknown:
                _log.warn(f"{name}: not found in build state — skipping")
        packages = {k: v for k, v in packages.items() if k in filter_names}

    if not packages and not discovered:
        print(
            "[SYSFORGE] No packages recorded in build state.\n"
            "Run `sysforge build <pkg>` first, or use --all to discover foreign packages.",
            file=sys.stderr,
        )
        return

    # Group by pkgbase to deduplicate split packages (one PKGBUILD per pkgbase)
    pkgbase_map, pkgbase_entry = group_by_pkgbase(packages)

    results: list[_UpdateResult] = []

    # --- Parallel git pulls ---
    # Pull all PKGBUILD dirs concurrently before the version-check loop.
    # git pull is network/IO-bound; ThreadPoolExecutor keeps the work parallel
    # without adding process overhead. Errors are captured and replayed below.
    pull_errors: dict[str, str] = {}
    if not getattr(args, "no_update", False):
        pull_candidates = [
            (pkgbase, Path(pkgbase_entry[pkgbase]["pkgbuild_dir"]))
            for pkgbase in sorted(pkgbase_map)
            if Path(pkgbase_entry[pkgbase]["pkgbuild_dir"]).is_dir()
            and (Path(pkgbase_entry[pkgbase]["pkgbuild_dir"]) / "PKGBUILD").exists()
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(git_pull_rebase, pkgbuild_dir): pkgbase
                for pkgbase, pkgbuild_dir in pull_candidates
            }
            for fut in as_completed(futures):
                pkgbase_key = futures[fut]
                try:
                    fut.result()
                except RuntimeError as e:
                    pull_errors[pkgbase_key] = str(e)

    for pkgbase, pkgnames in sorted(pkgbase_map.items()):
        entry = pkgbase_entry[pkgbase]
        pkgbuild_dir = Path(entry["pkgbuild_dir"])

        if not pkgbuild_dir.is_dir():
            _log.warn(f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
            continue

        pkgbuild_path = pkgbuild_dir / "PKGBUILD"
        if not pkgbuild_path.exists():
            _log.warn(f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
            continue

        # Use pre-computed pull result (parallel pulls ran above)
        if not getattr(args, "no_update", False) and pkgbase in pull_errors:
            _log.error(pull_errors[pkgbase])
            results.append(_UpdateResult(
                pkgbase=pkgbase,
                pkgnames=pkgnames,
                action="PULL_FAILED",
                installed_ver=None,
                pkgbuild_ver=None,
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        # Parse updated PKGBUILD
        try:
            pkgmeta = parse_pkgbuild(pkgbuild_path)
        except Exception as e:
            _log.warn(f"{pkgbase}: failed to parse PKGBUILD: {e} — skipping")
            continue

        globals_ = pkgmeta.get("globals", {})
        pkgbuild_ver = format_version(globals_)

        # VCS packages: version is only meaningful after running pkgver()
        if _is_vcs(pkgbase):
            results.append(_UpdateResult(
                pkgbase=pkgbase,
                pkgnames=pkgnames,
                action="DEVEL",
                installed_ver=None,
                pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        # Get installed version (check primary pkgname)
        installed_ver = get_installed_version(pkgnames[0])
        if installed_ver is None:
            results.append(_UpdateResult(
                pkgbase=pkgbase,
                pkgnames=pkgnames,
                action="NOT_INSTALLED",
                installed_ver=None,
                pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        try:
            cmp = vercmp(pkgbuild_ver, installed_ver)
        except RuntimeError as e:
            _log.warn(f"{pkgbase}: version comparison failed: {e} — skipping")
            continue

        if cmp > 0:
            action = "NEEDS_REBUILD"
        elif cmp == 0:
            action = "UP_TO_DATE"
        else:
            action = "DOWNGRADE"
            _log.warn(
                      f"{pkgbase}: PKGBUILD {pkgbuild_ver} is older than installed {installed_ver}")

        results.append(_UpdateResult(
            pkgbase=pkgbase,
            pkgnames=pkgnames,
            action=action,
            installed_ver=installed_ver,
            pkgbuild_ver=pkgbuild_ver,
            pkgbuild_path=pkgbuild_path,
        ))

    _print_summary(results, args)

    if getattr(args, "dry_run", False):
        return

    # Build packages that need rebuilding (git pull already done above)
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"]
    if getattr(args, "devel", False):
        to_build += [r for r in results if r.action == "DEVEL"]

    # Discovered packages to rebuild (--all mode); git pull already done in Phase 2
    discovered_to_build = [
        d for d in discovered
        if d.action == "OUTDATED"
        or (d.action == "DEVEL" and getattr(args, "devel", False))
    ]

    if not to_build and not discovered_to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    from sysforge.primitives.makepkg_wrapper import run as build_run, PGOBuildSkipped
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    reset_session()

    extra_flags = expand_makepkg_flags(args.makepkg) if getattr(args, "makepkg", None) else None

    # --- Phase 1: batch pre-install all makedepends (one sudo call) ---
    all_pkgbuild_paths = (
        [r.pkgbuild_path for r in to_build if r.pkgbuild_path]
        + [d.pkgbuild_path for d in discovered_to_build if d.pkgbuild_path]
    )
    makedeps = collect_makedeps(all_pkgbuild_paths)
    missing_deps = filter_missing_deps(makedeps)
    if missing_deps:
        try:
            batch_install_makedeps(missing_deps)
        except RuntimeError as e:
            _log.error(str(e))
            print(f"[SYSFORGE] Warning: makedep pre-install failed — some builds may fail", file=sys.stderr)

    # Where do built packages land? PKGDEST overrides the pkgbuild dir.
    pkgdest = get_pkgdest()
    interactive = getattr(args, "interactive", False)
    no_cleanbuild = getattr(args, "no_cleanbuild", False)
    # Prepend cleanbuild so stale $srcdir from a previous failed run never causes
    # patch-already-applied errors in prepare(). Suppressed by --no-cleanbuild.
    cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
    batch_flags = cleanbuild_flags + (extra_flags or [])
    # When --no-cleanbuild is set, also strip --cleanbuild/-C from profile makepkg_flags.
    strip_flags = BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild else BATCH_STRIP_FLAGS

    # --- Phase 2: build all packages (no syncdeps, no install per-package) ---
    built_pkg_files: list = []
    built_pkgs: list[str] = []
    failed_pkgs: list[str] = []
    pgo_skipped_pkgs: list[str] = []

    for result in to_build:
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
                update=False,  # git pull already done above
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

    for d in discovered_to_build:
        # Packages detected via DB version check have no local PKGBUILD yet — clone on demand.
        pkgbuild_path = d.pkgbuild_path
        if pkgbuild_path is None:
            from sysforge.primitives.config import find_pkgbuild
            try:
                pkgbuild_path = find_pkgbuild(d.pkgname, discover_config)
            except (FileNotFoundError, RuntimeError) as e:
                _log.error(f"Cannot find/clone PKGBUILD for {d.pkgname!r}: {e}")
                failed_pkgs.append(d.pkgname)
                continue

        search_dir = pkgdest if pkgdest else pkgbuild_path.parent
        build_start = time.time()
        try:
            build_run(pkgbuild_path, options=BuildOptions(
                pkg_log=not getattr(args, "no_pkg_log", False),
                persist_log=getattr(args, "persist_log", False),
                log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
                profile_conf=getattr(args, "profile_conf", None),
                cache_report=False,
                init_session=(not built_pkgs and not failed_pkgs),
                update=False,  # git pull already done in Phase 2 discovery
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
            built_pkgs.append(d.pkgname)
        except PGOBuildSkipped as e:
            _log.warn(str(e))
            pgo_skipped_pkgs.append(d.pkgname)
        except (RuntimeError, SystemExit) as e:
            _log.error(f"Build failed for {d.pkgname!r}: {e}")
            failed_pkgs.append(d.pkgname)

    # --- Phase 3: install all built packages in one sudo call ---
    # Deduplicate while preserving order (a package can appear in both loops if
    # it was discovered via --all and also has a build_state record).
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
    if pgo_skipped_pkgs:
        _log.ui(
            f"  PGO-skipped: {' '.join(pgo_skipped_pkgs)}"
            " (run 'sysforge run toolchain' to rebuild profdata)"
        )

    if unified_log_active:
        log.close_unified_log(success=(not failed_pkgs and not install_failed), persist=True)
        _log.ui(f"[SYSFORGE] Unified log: {unified_log_path}")


def _print_summary(results: list[_UpdateResult], args) -> None:
    if not results:
        print("[SYSFORGE] No packages to check.")
        return

    print()
    for r in results:
        if r.action == "NEEDS_REBUILD":
            print(f"  [NEEDS_REBUILD]  {r.pkgbase}: {r.installed_ver} → {r.pkgbuild_ver}")
        elif r.action == "UP_TO_DATE":
            print(f"  [UP_TO_DATE]     {r.pkgbase}: {r.pkgbuild_ver}")
        elif r.action == "DEVEL":
            if getattr(args, "devel", False):
                print(f"  [DEVEL]          {r.pkgbase}: will rebuild (--devel)")
            else:
                print(f"  [DEVEL]          {r.pkgbase}: skipped (use --devel to rebuild)")
        elif r.action == "NOT_INSTALLED":
            print(f"  [NOT_INSTALLED]  {r.pkgbase}: {r.pkgbuild_ver} (not currently installed)")
        elif r.action == "DOWNGRADE":
            print(f"  [DOWNGRADE]      {r.pkgbase}: installed {r.installed_ver} > pkgbuild {r.pkgbuild_ver} (skipped)")
        elif r.action == "PULL_FAILED":
            print(f"  [PULL_FAILED]    {r.pkgbase}: git pull failed (skipped)")
    print()
