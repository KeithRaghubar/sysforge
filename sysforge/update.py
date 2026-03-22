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
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import sysforge.log as _log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.aur import git_pull_rebase, fetch_aur_name_cache
from sysforge.primitives.config import PACKAGES_PATH, load_config
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


def _get_installed_version(pkgname: str) -> str | None:
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


def _get_foreign_packages() -> dict[str, str]:
    """
    Run `pacman -Qm` and return {pkgname: installed_version} for all
    foreign (non-repo) packages currently installed.
    """
    result = subprocess.run(
        ["pacman", "-Qm"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    packages = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            packages[parts[0]] = parts[1]
    return packages


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


def _append_to_packages_toml(path: Path, entries: list[dict]) -> None:
    """Append [[package]] blocks to packages.toml, creating the file if needed."""
    from sysforge.packages_cmd import _entry_toml_block
    blocks = "".join("\n" + _entry_toml_block(e) + "\n" for e in entries)
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

    foreign = _get_foreign_packages()
    if not foreign:
        _log.info("[UPDATE]", "--all: no foreign packages found via pacman -Qm")
        return []

    tracked = set(bs.all_packages().keys())
    in_manifest = _load_packages_toml_names(packages_path)

    new_foreign = {k: v for k, v in foreign.items() if k not in tracked and k not in in_manifest}
    unrecorded = {k: v for k, v in foreign.items() if k in in_manifest and k not in tracked}

    if not new_foreign and not unrecorded:
        _log.info("[UPDATE]", "--all: all foreign packages are already tracked")
        return []

    results: list[_DiscoveredResult] = []
    entries_to_add: list[dict] = []

    # --- Phase 1: truly new packages (not in packages.toml) ---
    if new_foreign:
        _log.info("[UPDATE]", f"--all: {len(new_foreign)} untracked foreign package(s) found")
        aur_results = aur_info(list(new_foreign.keys()))

        for pkgname in sorted(new_foreign):
            installed_ver = new_foreign[pkgname]

            if pkgname not in aur_results:
                _log.warn("[UPDATE]", f"--all: {pkgname!r} not found in AUR — skipping")
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
                    _log.warn("[UPDATE]", f"--all: {pkgname!r}: {e}")

                if pkgbuild_path and pkgbuild_path.exists():
                    try:
                        pkgmeta = parse_pkgbuild(pkgbuild_path)
                        pkgbuild_ver = format_version(pkgmeta.get("globals", {}))
                        from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                        pkgbuild_patch = bool(extract_pkgbuild_profile(pkgmeta, pkgbuild_path))
                    except Exception as e:
                        _log.warn("[UPDATE]", f"--all: {pkgname!r}: failed to parse PKGBUILD: {e}")

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
        _log.info("[UPDATE]", f"--all: appended {len(entries_to_add)} package(s) to {packages_path}")

    # --- Phase 2: packages in packages.toml with no build_state record ---
    if unrecorded:
        _log.info("[UPDATE]",
                  f"--all: {len(unrecorded)} package(s) in packages.toml with no build record — checking versions")

        for pkgname in sorted(unrecorded):
            installed_ver = unrecorded[pkgname]
            pkgbuild_path = None
            pkgbuild_ver = None

            if not getattr(args, "dry_run", False):
                try:
                    pkgbuild_path = find_pkgbuild(pkgname, config)
                except (FileNotFoundError, RuntimeError) as e:
                    _log.warn("[UPDATE]", f"--all: {pkgname!r}: {e}")

                if pkgbuild_path and pkgbuild_path.exists():
                    try:
                        pkgmeta = parse_pkgbuild(pkgbuild_path)
                        pkgbuild_ver = format_version(pkgmeta.get("globals", {}))
                    except Exception as e:
                        _log.warn("[UPDATE]", f"--all: {pkgname!r}: failed to parse PKGBUILD: {e}")

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
    print("\n  — Discovered foreign packages —")
    for r in results:
        ver = f": {r.installed_ver}" if r.installed_ver else ""
        if r.action == "ADDED":
            print(f"  [ADDED]          {r.pkgname}{ver} (added to packages.toml)")
        elif r.action == "OUTDATED":
            print(f"  [OUTDATED]       {r.pkgname}: {r.installed_ver} → {r.pkgbuild_ver} (added, will rebuild)")
        elif r.action == "DEVEL":
            flag = "(--devel to rebuild)" if not getattr(args, "devel", False) else "(will rebuild)"
            print(f"  [DEVEL]          {r.pkgname}{ver} (added, {flag})")
        elif r.action == "NOT_FOUND":
            print(f"  [NOT_FOUND]      {r.pkgname}{ver} (not in AUR — skipped)")


# Flags stripped from each per-package makepkg call during batch update.
# Deps are pre-installed in one shot; packages are installed in one shot at the end.
_BATCH_STRIP_FLAGS = frozenset({"--syncdeps", "-s", "--install", "-i"})


def _get_pkgdest() -> Path | None:
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


def _collect_makedeps(pkgbuild_paths: list) -> list:
    """Parse PKGBUILDs and return a sorted unique list of their makedepends."""
    deps: set = set()
    for path in pkgbuild_paths:
        try:
            pkgmeta = parse_pkgbuild(path)
            raw = pkgmeta.get("globals", {}).get("makedepends", [])
            if isinstance(raw, str):
                raw = [raw]
            # Strip version constraints (e.g. "cmake>=3.16" → "cmake")
            for dep in raw:
                deps.add(dep.split(">=")[0].split("<=")[0].split("=")[0].split(">")[0].split("<")[0])
        except Exception as e:
            _log.warn("[UPDATE]", f"makedeps parse error ({Path(path).parent.name}): {e}")
    return sorted(deps)


def _filter_missing(deps: list) -> list:
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


def _batch_install_makedeps(deps: list) -> None:
    _log.info("[UPDATE]", f"Batch-installing {len(deps)} missing makedep(s): {deps}")
    result = subprocess.run(
        ["sudo", "pacman", "-S", "--needed", "--noconfirm"] + deps
    )
    if result.returncode != 0:
        raise RuntimeError(f"makedep install failed (exit {result.returncode})")


def _snapshot_pkg_dir(directory: Path) -> frozenset:
    """Return frozenset of .pkg.tar.* paths (not .sig) in directory."""
    if not directory.exists():
        return frozenset()
    return frozenset(
        p for p in directory.glob("*.pkg.tar.*")
        if not p.name.endswith(".sig")
    )


def _batch_install_pkgs(pkg_paths: list) -> bool:
    """Install all built packages in one sudo pacman -U call. Returns True on success."""
    _log.info("[UPDATE]", f"Batch-installing {len(pkg_paths)} built package file(s)")
    result = subprocess.run(
        ["sudo", "pacman", "-U", "--noconfirm"] + [str(p) for p in pkg_paths]
    )
    return result.returncode == 0


def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""
    # Refresh the AUR name cache as a side effect; failures are non-fatal
    fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # --all: discover and classify foreign packages before the main update loop
    discovered: list[_DiscoveredResult] = []
    if getattr(args, "all", False):
        config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
        discover_config = load_config(config_paths=config_paths) or {}
        if getattr(args, "packages", None):
            discover_config["packages_file"] = args.packages
        discovered = _discover_and_add(args, bs, discover_config)
        _print_discovery_summary(discovered, args)

    packages = bs.all_packages()

    if not packages and not discovered:
        print(
            "[SYSFORGE] No packages recorded in build state.\n"
            "Run `sysforge build <pkg>` first, or use --all to discover foreign packages.",
            file=sys.stderr,
        )
        return

    # Group by pkgbase to deduplicate split packages (one PKGBUILD per pkgbase)
    pkgbase_map: dict[str, list[str]] = {}   # pkgbase -> [pkgnames]
    pkgbase_entry: dict[str, dict] = {}       # pkgbase -> representative entry
    for pkgname, entry in packages.items():
        base = entry.get("pkgbase", pkgname)
        pkgbase_map.setdefault(base, []).append(pkgname)
        if base not in pkgbase_entry:
            pkgbase_entry[base] = entry

    results: list[_UpdateResult] = []

    for pkgbase, pkgnames in sorted(pkgbase_map.items()):
        entry = pkgbase_entry[pkgbase]
        pkgbuild_dir = Path(entry["pkgbuild_dir"])

        if not pkgbuild_dir.is_dir():
            _log.warn("[UPDATE]", f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
            continue

        pkgbuild_path = pkgbuild_dir / "PKGBUILD"
        if not pkgbuild_path.exists():
            _log.warn("[UPDATE]", f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
            continue

        # Pull latest PKGBUILD unless --no-update
        if not getattr(args, "no_update", False):
            try:
                git_pull_rebase(pkgbuild_dir)
            except RuntimeError as e:
                _log.error("[UPDATE]", str(e))
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
            _log.warn("[UPDATE]", f"{pkgbase}: failed to parse PKGBUILD: {e} — skipping")
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
        installed_ver = _get_installed_version(pkgnames[0])
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
            _log.warn("[UPDATE]", f"{pkgbase}: version comparison failed: {e} — skipping")
            continue

        if cmp > 0:
            action = "NEEDS_REBUILD"
        elif cmp == 0:
            action = "UP_TO_DATE"
        else:
            action = "DOWNGRADE"
            _log.warn("[UPDATE]",
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

    # Discovered packages to rebuild (--all mode); git pull not yet done
    discovered_to_build = [
        d for d in discovered
        if d.pkgbuild_path is not None
        and (d.action == "OUTDATED"
             or (d.action == "DEVEL" and getattr(args, "devel", False)))
    ]

    if not to_build and not discovered_to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    from sysforge.primitives.makepkg_wrapper import run as build_run
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    from sysforge.cli import _expand_makepkg_flags
    reset_session()

    extra_flags = _expand_makepkg_flags(args.makepkg) if getattr(args, "makepkg", None) else None

    # Unified log: always persisted (update runs are long; failures need post-mortem).
    unified_log_active = not getattr(args, "no_unified_log", False) and not getattr(args, "dry_run", False)
    unified_log_path = (Path(args.log_dir) if getattr(args, "log_dir", None) else state_dir) / "sysforge-update.log"
    if unified_log_active:
        try:
            _log.open_unified_log(unified_log_path, purge=getattr(args, "purge_log", False))
            _log.info("[UPDATE]", f"Unified log: {unified_log_path}")
        except OSError as e:
            unified_log_active = False
            _log.warn("[UPDATE]", f"Cannot write unified log to {unified_log_path}: {e} — logging to terminal only")

    # --- Phase 1: batch pre-install all makedepends (one sudo call) ---
    all_pkgbuild_paths = (
        [r.pkgbuild_path for r in to_build if r.pkgbuild_path]
        + [d.pkgbuild_path for d in discovered_to_build if d.pkgbuild_path]
    )
    makedeps = _collect_makedeps(all_pkgbuild_paths)
    missing_deps = _filter_missing(makedeps)
    if missing_deps:
        try:
            _batch_install_makedeps(missing_deps)
        except RuntimeError as e:
            _log.error("[UPDATE]", str(e))
            print(f"[SYSFORGE] Warning: makedep pre-install failed — some builds may fail", file=sys.stderr)

    # Where do built packages land? PKGDEST overrides the pkgbuild dir.
    pkgdest = _get_pkgdest()

    # --- Phase 2: build all packages (no syncdeps, no install per-package) ---
    built_pkg_files: list = []
    built = failed = 0

    for result in to_build:
        search_dir = pkgdest if pkgdest else result.pkgbuild_path.parent
        before = _snapshot_pkg_dir(search_dir)
        try:
            build_run(
                result.pkgbuild_path,
                pkg_log=not getattr(args, "no_pkg_log", False),
                persist_log=getattr(args, "persist_log", False),
                log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
                profile_conf=getattr(args, "profile_conf", None),
                cache_report=False,
                init_session=(built + failed == 0),
                update=False,  # git pull already done above
                state_dir=Path(args.state_dir) if getattr(args, "state_dir", None) else None,
                extra_flags=extra_flags,
                strip_flags=_BATCH_STRIP_FLAGS,
            )
            new_pkgs = sorted(_snapshot_pkg_dir(search_dir) - before)
            built_pkg_files.extend(new_pkgs)
            built += 1
        except (RuntimeError, SystemExit) as e:
            _log.error("[UPDATE]", f"Build failed for {result.pkgbase!r}: {e}")
            failed += 1

    for d in discovered_to_build:
        search_dir = pkgdest if pkgdest else d.pkgbuild_path.parent
        before = _snapshot_pkg_dir(search_dir)
        try:
            build_run(
                d.pkgbuild_path,
                pkg_log=not getattr(args, "no_pkg_log", False),
                persist_log=getattr(args, "persist_log", False),
                log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
                profile_conf=getattr(args, "profile_conf", None),
                cache_report=False,
                init_session=(built + failed == 0),
                update=not getattr(args, "no_update", False),
                state_dir=Path(args.state_dir) if getattr(args, "state_dir", None) else None,
                extra_flags=extra_flags,
                strip_flags=_BATCH_STRIP_FLAGS,
            )
            new_pkgs = sorted(_snapshot_pkg_dir(search_dir) - before)
            built_pkg_files.extend(new_pkgs)
            built += 1
        except (RuntimeError, SystemExit) as e:
            _log.error("[UPDATE]", f"Build failed for {d.pkgname!r}: {e}")
            failed += 1

    # --- Phase 3: install all built packages in one sudo call ---
    if built_pkg_files:
        if not _batch_install_pkgs(built_pkg_files):
            _log.error("[UPDATE]", "Batch package install failed")
            print("[SYSFORGE] Error: batch install failed — packages were built but not installed.", file=sys.stderr)
            failed += 1
    elif built > 0:
        _log.warn("[UPDATE]", "No .pkg.tar.* files found after builds — nothing to install")

    if unified_log_active:
        _log.close_unified_log(success=(failed == 0), persist=True)
        print(f"[SYSFORGE] Unified log: {unified_log_path}")

    if getattr(args, "cache_report", False):
        emit_session_report()

    skipped = len(results) - len(to_build)
    print(f"\n[SYSFORGE] Update complete: {built} built, {failed} failed, {skipped} skipped.")


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
