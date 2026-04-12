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

Public API:
    cmd_update(args)
"""
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
from sysforge.primitives.aur import git_pull_rebase, fetch_aur_name_cache, purge_src, aur_clone, aur_info
from sysforge.primitives.config import load_config
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
# --all: foreign package discovery (Phase 1 only — truly new packages)
# ---------------------------------------------------------------------------

@dataclass
class _DiscoveredResult:
    pkgname: str
    # ADDED, OUTDATED, DEVEL, NOT_FOUND, ALREADY_TRACKED
    action: str
    installed_ver: str | None = None
    pkgbuild_ver: str | None = None
    pkgbuild_path: Path | None = None


def _discover_and_add(args, bs: BuildState, config: dict,
                      packages_path: Path) -> list[_DiscoveredResult]:
    """
    Discover foreign packages not yet in packages.toml and add them.

    Only handles truly new packages (not in packages.toml or build_state).
    Packages already in packages.toml but missing from build_state are now
    handled by the main update loop.

    Returns list of _DiscoveredResult for summary display.
    """
    from sysforge.primitives.aur import aur_info
    from sysforge.primitives.config import find_pkgbuild

    foreign = get_foreign_packages()

    if not foreign:
        _log.info("--all: no foreign packages found")
        return []

    tracked = set(bs.all_packages().keys())
    in_manifest = _load_packages_toml_names(packages_path)

    new_foreign = {k: v for k, v in foreign.items() if k not in tracked and k not in in_manifest}

    if not new_foreign:
        _log.info("--all: no new foreign packages to discover")
        return []

    results: list[_DiscoveredResult] = []
    entries_to_add: list[dict] = []

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


# ---------------------------------------------------------------------------
# Per-pkgbase version check (called from ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _check_one_pkgbase(
    pkgbase: str,
    pkgnames: list[str],
    entry: dict,
    pull_errors: dict[str, str],
    all_installed: dict[str, str],
    unrecorded_names: set[str],
    skip_pull_check: bool,
    fetch_missing: bool = False,
) -> _UpdateResult | None:
    """Check a single pkgbase and return an _UpdateResult, or None on skip."""
    pkgbuild_dir = Path(entry["pkgbuild_dir"])
    has_record = not any(pn in unrecorded_names for pn in pkgnames)
    is_repo = entry.get("source") == "repo"

    if not pkgbuild_dir.is_dir():
        if fetch_missing and not is_repo:
            try:
                aur_clone(pkgbase, pkgbuild_dir)
            except RuntimeError as e:
                _log.error(f"{pkgbase}: --fetch-missing clone failed: {e}")
                return None
        else:
            _log.warn(f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
            return None

    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        if fetch_missing and not is_repo:
            # Dir exists but is empty / partial — wipe and re-clone.
            try:
                purge_src(pkgbuild_dir)
                aur_clone(pkgbase, pkgbuild_dir)
            except RuntimeError as e:
                _log.error(f"{pkgbase}: --fetch-missing recovery failed: {e}")
                return None
        else:
            _log.warn(f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
            return None

    if not skip_pull_check and pkgbase in pull_errors:
        _log.error(pull_errors[pkgbase])
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

    # VCS packages: version is only meaningful after running pkgver()
    if _is_vcs(pkgbase):
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL",
            installed_ver=None, pkgbuild_ver=pkgbuild_ver,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
        )

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
# Main entry point
# ---------------------------------------------------------------------------

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

    # --- Load config and packages.toml early (needed for all paths) ---
    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths) or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages

    packages_path = resolve_packages_path(config)
    build_cfg, manifest_entries = _load_full_packages_toml(packages_path)
    manifest_by_name = {e["name"]: e for e in manifest_entries}

    # --- --all: discover truly new foreign packages ---
    discovered: list[_DiscoveredResult] = []
    if getattr(args, "all", False):
        discovered = _discover_and_add(args, bs, config, packages_path)
        _print_discovery_summary(discovered, args)

    # --- Build unified package set from packages.toml + build_state ---
    build_state_pkgs = bs.all_packages()
    all_installed = get_all_installed_packages()

    # Resolve base pkgbuild_src_dir for packages without a build_state record
    pkgbuild_src_dir_raw = (
        build_cfg.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )
    pkgbuild_src_dir_base = Path(pkgbuild_src_dir_raw).expanduser() if pkgbuild_src_dir_raw else None

    packages: dict[str, dict] = {}
    unrecorded_names: set[str] = set()

    for name in manifest_by_name:
        if name in build_state_pkgs:
            pkg = build_state_pkgs[name]
            manifest_source = manifest_by_name[name].get("source")
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
            manifest_source = manifest_by_name[name].get("source")
            if manifest_source:
                entry["source"] = manifest_source
            packages[name] = entry

    # Resolve pkgbase for unrecorded AUR packages via AUR RPC so split packages
    # get the correct pkgbase and pkgbuild_dir (e.g. ob-xd-common → pkgbase ob-xd).
    if unrecorded_names and pkgbuild_src_dir_base:
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
                _log.warn(f"{name}: not found in packages.toml — skipping")
        packages = {k: v for k, v in packages.items() if k in filter_names}

    if not packages and not discovered:
        print(
            "[SYSFORGE] No packages found in packages.toml or build state.\n"
            "Add packages with `sysforge packages add`, or use --all to discover foreign packages.",
            file=sys.stderr,
        )
        return

    # Group by pkgbase to deduplicate split packages (one PKGBUILD per pkgbase)
    pkgbase_map, pkgbase_entry = group_by_pkgbase(packages)

    results: list[_UpdateResult] = []

    # --- Optional --cleansrc: purge each src dir before pulling ---
    # Per-package fatal: a dirty repo (uncommitted, unpushed, or no upstream)
    # is reported via cleansrc_failures and excluded from this run; everything
    # else proceeds normally so the unattended path doesn't abort.
    cleansrc_failures: dict[str, str] = {}
    if getattr(args, "cleansrc", False) and not getattr(args, "dry_run", False):
        seen_purge: set[str] = set()
        for pkgbase in sorted(pkgbase_map):
            d = Path(pkgbase_entry[pkgbase]["pkgbuild_dir"])
            key = str(d)
            if key in seen_purge:
                continue
            seen_purge.add(key)
            try:
                purge_src(d)
            except RuntimeError as e:
                cleansrc_failures[pkgbase] = str(e)
                _log.error(f"--cleansrc {pkgbase}: {e}")

        if cleansrc_failures:
            # Drop fatal pkgbases from this run so they don't get pulled,
            # version-checked, or built against a stale dir we couldn't purge.
            for failed in cleansrc_failures:
                pkgbase_map.pop(failed, None)
                pkgbase_entry.pop(failed, None)

    # --- Parallel git pulls ---
    # Pull all PKGBUILD dirs concurrently before the version-check loop.
    # Deduplicate by resolved path to avoid pulling the same dir twice
    # (can happen with unrecorded packages sharing a pkgbase).
    pull_errors: dict[str, str] = {}
    skip_pulls = getattr(args, "no_update", False) or getattr(args, "dry_run", False)
    if not skip_pulls:
        seen_dirs: set[str] = set()
        pull_candidates: list[tuple[str, Path]] = []
        for pkgbase in sorted(pkgbase_map):
            d = Path(pkgbase_entry[pkgbase]["pkgbuild_dir"])
            resolved = str(d)
            if resolved in seen_dirs or not d.is_dir():
                continue
            if not (d / "PKGBUILD").exists():
                continue
            seen_dirs.add(resolved)
            pull_candidates.append((pkgbase, d))

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

    # --- Parallel version checks ---
    fetch_missing = getattr(args, "fetch_missing", False) and not getattr(args, "dry_run", False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _check_one_pkgbase, pkgbase, pkgnames,
                pkgbase_entry[pkgbase], pull_errors, all_installed,
                unrecorded_names, skip_pulls, fetch_missing,
            ): pkgbase
            for pkgbase, pkgnames in sorted(pkgbase_map.items())
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                results.append(result)

    # Sort results for stable output
    results.sort(key=lambda r: r.pkgbase)

    _print_summary(results, args)

    if getattr(args, "dry_run", False):
        return

    # Build packages that need rebuilding (git pull already done above)
    # Without --all, only packages with a build_state record are built.
    build_all = getattr(args, "all", False)
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"
                and (r.has_build_record or build_all)]
    if getattr(args, "devel", False):
        to_build += [r for r in results if r.action == "DEVEL"
                     and (r.has_build_record or build_all)]

    # Discovered packages to rebuild (--all mode)
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

    # --- Phase 1b: resolve and build AUR-only deps ---
    from sysforge.primitives.aur_resolve import resolve_aur_deps_batch, build_resolved_deps
    if all_pkgbuild_paths:
        try:
            aur_deps = resolve_aur_deps_batch(all_pkgbuild_paths, config, fetch=True)
            # Filter out packages we're already about to build
            building_names = {r.pkgbase for r in to_build} | {d.pkgname for d in discovered_to_build}
            aur_deps = [d for d in aur_deps if d.name not in building_names]
            if aur_deps:
                build_resolved_deps(aur_deps)
        except RuntimeError as e:
            _log.error(f"AUR dep resolution failed: {e}")
            print("[SYSFORGE] Warning: AUR dep resolution failed — some builds may fail", file=sys.stderr)

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
                pkgbuild_path = find_pkgbuild(d.pkgname, config)
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
                update=False,  # git pull already done in discovery
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

    # Cleansrc-fatal packages count as failures (build never attempted).
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

        if r.action == "NEEDS_REBUILD":
            print(f"  [NEEDS_REBUILD]  {r.pkgbase}: {r.installed_ver} → {r.pkgbuild_ver}{star}")
        elif r.action == "UP_TO_DATE":
            print(f"  [UP_TO_DATE]     {r.pkgbase}: {r.pkgbuild_ver}{star}")
        elif r.action == "DEVEL":
            if getattr(args, "devel", False):
                print(f"  [DEVEL]          {r.pkgbase}: will rebuild (--devel){star}")
            else:
                print(f"  [DEVEL]          {r.pkgbase}: skipped (use --devel to rebuild){star}")
        elif r.action == "NOT_INSTALLED":
            print(f"  [NOT_INSTALLED]  {r.pkgbase}: {r.pkgbuild_ver} (not currently installed){star}")
        elif r.action == "DOWNGRADE":
            print(f"  [DOWNGRADE]      {r.pkgbase}: installed {r.installed_ver} > pkgbuild {r.pkgbuild_ver} (skipped){star}")
        elif r.action == "PULL_FAILED":
            print(f"  [PULL_FAILED]    {r.pkgbase}: git pull failed (skipped){star}")

    if has_unrecorded:
        print(f"\n  * = no build record (use --all to include in rebuild)")
    print()
