"""
fetch.py — sysforge fetch subcommand

Download one or more PKGBUILDs into pkgbuild_dir without building them.
For packages already cloned, runs git pull --rebase (skipped with --no-update).
Prints the resulting PKGBUILD directory for each package.

Public API:
    cmd_fetch(args)
"""
import sys
from pathlib import Path

from sysforge import log
_log = log.get_logger("FETCH")
from sysforge.primitives.config import find_pkgbuild, load_config
from sysforge.primitives.llvm_state import collect_llvm_state, render_preflight
from sysforge.primitives.source_sync import SyncRequest, get_scheduler


def cmd_fetch(args) -> None:
    """Entry point for sysforge fetch."""
    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths)
    scheduler = get_scheduler(
        force_devel=getattr(args, "devel", False),
    )

    if not getattr(args, "no_llvm_preflight", False):
        report = collect_llvm_state(args.pkgs, config)
        if report.states:
            print(render_preflight(report))

    from sysforge.ui import progress as _ui_progress
    failed = 0
    with _ui_progress.tracker(len(args.pkgs), "fetching") as _tick:
        for pkg in args.pkgs:
            _tick(pkg)
            try:
                pkgbuild_path = find_pkgbuild(pkg, config)
            except (FileNotFoundError, RuntimeError) as e:
                _log.error(str(e))
                failed += 1
                continue

            pkgbuild_dir = pkgbuild_path.parent

            if not args.no_update:
                result = scheduler.request(SyncRequest(
                    pkgbase=pkgbuild_dir.name,
                    pkgbuild_dir=pkgbuild_dir,
                    force_fetch=getattr(args, "force_fetch", False),
                ))
                if result.error and result.status != "up_to_date":
                    _log.warn(f"{pkgbuild_dir.name}: {result.error}")

            print(str(pkgbuild_dir))

    if failed:
        sys.exit(1)
