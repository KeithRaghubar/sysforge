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

Public API:
    cmd_update(args)
"""
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import sysforge.log as _log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.aur import git_pull_rebase
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


def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""
    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    packages = bs.all_packages()

    if not packages:
        print(
            "[SYSFORGE] No packages recorded in build state.\n"
            "Run `sysforge build <pkg>` first to populate the build state.",
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

    # Build packages that need rebuilding
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"]
    if getattr(args, "devel", False):
        to_build += [r for r in results if r.action == "DEVEL"]

    if not to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    from sysforge.primitives.makepkg_wrapper import run as build_run
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    reset_session()

    built = failed = 0
    for result in to_build:
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
            )
            built += 1
        except (RuntimeError, SystemExit) as e:
            _log.error("[UPDATE]", f"Build failed for {result.pkgbase!r}: {e}")
            failed += 1

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
