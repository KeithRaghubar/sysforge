"""
converge.py — detect and repair profile flag drift

Compares the flags_string recorded in build_state.toml at build time against
the flags that would be applied if the package were built now (by re-resolving
the current profile). Packages whose stored flags differ from current are
flagged as DRIFTED.

Default (no --apply): show drift summary and per-key diffs.
--apply: rebuild all DRIFTED packages with the current profile.

Scope: only profiled packages (build_mode = "profiled") recorded in
build_state.toml. Packages recorded without flags_string (built before
this feature) are reported as NO_FLAGS and skipped.

Public API:
    cmd_converge(args)
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sysforge.log as _log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.config import load_config, load_conflict_groups
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.profile import match_rules, resolve_profile, serialize_flags
from sysforge.pipeline.state import resolve_state_dir


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class _ConvergeResult:
    pkgbase: str
    pkgnames: list
    # IN_SYNC, DRIFTED, NO_FLAGS, NO_PKGBUILD, PARSE_ERROR, PACMAN_ONLY
    status: str
    pkgbuild_path: Path | None = None
    stored_flags: str | None = None
    current_flags: str | None = None
    diffs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Flags diff
# ---------------------------------------------------------------------------

def _diff_flags(stored: str, current: str) -> list[str]:
    """
    Return human-readable diff lines between two flags strings.
    Format per changed key: "  KEY: <old> → <new>" or "  +KEY: <new>" / "  -KEY: <old>".
    """
    def _parse(s: str) -> dict[str, str]:
        result = {}
        for line in s.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v
        return result

    old = _parse(stored)
    new = _parse(current)
    all_keys = sorted(set(old) | set(new))
    diffs = []
    for key in all_keys:
        if key not in old:
            diffs.append(f"  +{key}: {new[key]!r}")
        elif key not in new:
            diffs.append(f"  -{key}: {old[key]!r}")
        elif old[key] != new[key]:
            diffs.append(f"  {key}: {old[key]!r} → {new[key]!r}")
    return diffs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def cmd_converge(args) -> None:
    """Entry point for `sysforge converge`."""
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

    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths)
    conflict_groups = load_conflict_groups()

    # Group by pkgbase (one PKGBUILD per pkgbase)
    pkgbase_map: dict[str, list[str]] = {}
    pkgbase_entry: dict[str, dict] = {}
    for pkgname, entry in packages.items():
        base = entry.get("pkgbase", pkgname)
        pkgbase_map.setdefault(base, []).append(pkgname)
        if base not in pkgbase_entry:
            pkgbase_entry[base] = entry

    results: list[_ConvergeResult] = []

    for pkgbase, pkgnames in sorted(pkgbase_map.items()):
        entry = pkgbase_entry[pkgbase]

        if entry.get("build_mode") != "profiled":
            results.append(_ConvergeResult(
                pkgbase=pkgbase, pkgnames=pkgnames, status="PACMAN_ONLY",
            ))
            continue

        pkgbuild_path = Path(entry["pkgbuild_dir"]) / "PKGBUILD"
        if not pkgbuild_path.exists():
            results.append(_ConvergeResult(
                pkgbase=pkgbase, pkgnames=pkgnames, status="NO_PKGBUILD",
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        stored_flags = entry.get("flags_string")
        if not stored_flags:
            results.append(_ConvergeResult(
                pkgbase=pkgbase, pkgnames=pkgnames, status="NO_FLAGS",
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        try:
            pkgmeta = parse_pkgbuild(pkgbuild_path)
        except Exception as e:
            _log.warn("[CONVERGE]", f"{pkgbase}: failed to parse PKGBUILD: {e}")
            results.append(_ConvergeResult(
                pkgbase=pkgbase, pkgnames=pkgnames, status="PARSE_ERROR",
                pkgbuild_path=pkgbuild_path,
            ))
            continue

        matched = match_rules(pkgmeta, config.get("rules", []))
        resolved = resolve_profile(pkgmeta, matched, config, conflict_groups)
        current_flags = serialize_flags(resolved)

        diffs = _diff_flags(stored_flags, current_flags)
        status = "DRIFTED" if diffs else "IN_SYNC"

        results.append(_ConvergeResult(
            pkgbase=pkgbase,
            pkgnames=pkgnames,
            status=status,
            pkgbuild_path=pkgbuild_path,
            stored_flags=stored_flags,
            current_flags=current_flags,
            diffs=diffs,
        ))

    _print_summary(results)

    if getattr(args, "apply", False):
        _apply(results, args)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_summary(results: list[_ConvergeResult]) -> None:
    if not results:
        print("[SYSFORGE] No packages to check.")
        return

    print()
    for r in results:
        if r.status == "IN_SYNC":
            print(f"  [IN_SYNC]      {r.pkgbase}")
        elif r.status == "DRIFTED":
            print(f"  [DRIFTED]      {r.pkgbase}")
            for line in r.diffs:
                print(line)
        elif r.status == "NO_FLAGS":
            print(f"  [NO_FLAGS]     {r.pkgbase}  (built before flag tracking; rebuild to record)")
        elif r.status == "NO_PKGBUILD":
            print(f"  [NO_PKGBUILD]  {r.pkgbase}  ({r.pkgbuild_path})")
        elif r.status == "PARSE_ERROR":
            print(f"  [PARSE_ERROR]  {r.pkgbase}  ({r.pkgbuild_path})")
        elif r.status == "PACMAN_ONLY":
            pass  # pacman-installed packages are not in scope; omit from output

    drifted  = [r for r in results if r.status == "DRIFTED"]
    in_sync  = [r for r in results if r.status == "IN_SYNC"]
    no_flags = [r for r in results if r.status == "NO_FLAGS"]
    skipped  = [r for r in results if r.status in ("NO_PKGBUILD", "PARSE_ERROR")]

    print()
    parts = [f"In sync: {len(in_sync)}", f"Drifted: {len(drifted)}"]
    if no_flags:
        parts.append(f"No flags: {len(no_flags)}")
    if skipped:
        parts.append(f"Skipped: {len(skipped)}")
    print("[SYSFORGE] " + "  |  ".join(parts))

    if drifted:
        print("[SYSFORGE] Run with --apply to rebuild drifted packages.")
    print()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _apply(results: list[_ConvergeResult], args) -> None:
    import time

    to_build = [r for r in results if r.status == "DRIFTED"]
    if not to_build:
        print("[SYSFORGE] Nothing to rebuild.")
        return

    from sysforge.primitives.makepkg_wrapper import run as build_run
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    from sysforge.update import _snapshot_pkg_dir, _batch_install_pkgs, _get_pkgdest, _BATCH_STRIP_FLAGS
    reset_session()

    pkgdest = _get_pkgdest()

    # -f is always required: the package artifact already exists from the prior
    # build, so makepkg refuses to rebuild without --force.
    # Strip --install/-i and --syncdeps/-s: packages are installed in one batch
    # at the end; makedeps are already present from the prior build.
    user_flags = getattr(args, "extra_flags", [])
    extra_flags = ["-f"] + user_flags

    built_pkg_files: list = []
    built = failed = 0
    for result in to_build:
        search_dir = pkgdest if pkgdest else (result.pkgbuild_path.parent if result.pkgbuild_path else Path("."))
        build_start = time.time()
        print(f"[SYSFORGE] Rebuilding {result.pkgbase!r}...")
        try:
            build_run(
                result.pkgbuild_path,
                extra_flags=extra_flags,
                strip_flags=_BATCH_STRIP_FLAGS,
                pkg_log=not getattr(args, "no_pkg_log", False),
                persist_log=getattr(args, "persist_log", False),
                log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
                profile_conf=getattr(args, "profile_conf", None),
                cache_report=False,
                init_session=(built + failed == 0),
                update=False,   # converge is about flag drift, not version updates
                state_dir=Path(args.state_dir) if getattr(args, "state_dir", None) else None,
            )
            new_pkgs = sorted(
                p for p in _snapshot_pkg_dir(search_dir)
                if p.stat().st_mtime >= build_start
            )
            built_pkg_files.extend(new_pkgs)
            built += 1
        except (RuntimeError, SystemExit) as e:
            _log.error("[CONVERGE]", f"Build failed for {result.pkgbase!r}: {e}")
            failed += 1

    if built_pkg_files:
        if not _batch_install_pkgs(built_pkg_files):
            _log.error("[CONVERGE]", "Batch install failed")
            print(
                "[SYSFORGE] Error: batch install failed — packages were built but not installed.",
                file=sys.stderr,
            )
            failed += 1
    elif built > 0:
        _log.warn("[CONVERGE]", "No .pkg.tar.* files found after builds — nothing to install")

    if getattr(args, "cache_report", False):
        emit_session_report()

    print(f"\n[SYSFORGE] Converge complete: {built} rebuilt, {failed} failed.")
