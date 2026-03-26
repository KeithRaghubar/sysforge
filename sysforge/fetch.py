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
from sysforge.primitives.aur import git_pull_rebase


def cmd_fetch(args) -> None:
    """Entry point for sysforge fetch."""
    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths)

    failed = 0
    for pkg in args.pkgs:
        try:
            pkgbuild_path = find_pkgbuild(pkg, config)
        except (FileNotFoundError, RuntimeError) as e:
            _log.error(str(e))
            failed += 1
            continue

        pkgbuild_dir = pkgbuild_path.parent

        if not args.no_update:
            try:
                git_pull_rebase(pkgbuild_dir)
            except RuntimeError as e:
                _log.warn(str(e))

        print(str(pkgbuild_dir))

    if failed:
        sys.exit(1)
