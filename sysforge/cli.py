"""
cli.py — SysForge command-line interface

Implemented commands:
    sysforge build <PKGBUILD>   Build a single package using its matched profile
    sysforge install            Run the pipeline (stages 5-7 usable now;
                                stages 1-4 are stubs, use --start-from packages)
    sysforge manifest           Generate a packages.toml stub from a list of names

Stubbed commands (not yet implemented):
    sysforge converge           Rebuild packages whose profile, flags, or version have drifted
    sysforge resolve <pkg>      Show which profile would be applied to a package and why
"""
import argparse
import sys
from pathlib import Path

from sysforge.manifest import cmd_manifest

from sysforge.primitives.makepkg_wrapper import run
from sysforge.primitives.config import load_config
from sysforge.pipeline.runner import run_pipeline
from sysforge.pipeline.stages.base import RunOptions


def _expand_makepkg_flags(flags_str):
    """
    Split a makepkg flags string into a list of individual flags,
    expanding combined short flags like '-sfci' into ['-s', '-f', '-c', '-i'].
    Long flags (--noconfirm) and flags with values are passed through as-is.
    """
    if not flags_str:
        return []
    result = []
    for token in flags_str.split():
        if token.startswith("--"):
            result.append(token)
        elif token.startswith("-") and len(token) > 2:
            # Combined short flags e.g. -sfci → -s -f -c -i
            result.extend(f"-{ch}" for ch in token[1:])
        else:
            result.append(token)
    return result


def _cmd_build(args):
    extra_flags = _expand_makepkg_flags(args.makepkg) if args.makepkg else None
    try:
        run(args.pkgbuild, extra_flags=extra_flags)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_install(args):
    config = load_config()

    # Inject packages_file into config if passed on CLI
    if args.packages:
        config["packages_file"] = args.packages

    options = RunOptions(
        resume=args.resume,
        start_from=args.start_from,
        force_retry=args.force_retry,
        dry_run=args.dry_run,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )

    run_pipeline(config, options)


def _cmd_converge(args):
    # TODO: compare installed state in /var/lib/sysforge/build_state.toml
    # against current manifest and flag profiles; rebuild drifted packages.
    # --dry-run should show what would be rebuilt without doing it.
    print("[SYSFORGE] 'converge' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_resolve(args):
    # TODO: parse the named package's PKGBUILD (fetched from AUR or a local path),
    # run rule matching and profile resolution, and print the resolved profile.
    # --show-flags prints the full resolved flag set.
    print("[SYSFORGE] 'resolve' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Reproducible, performance-tuned Arch Linux installer.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # build
    p_build = sub.add_parser("build", help="Build a package from a PKGBUILD.")
    p_build.add_argument(
        "pkgbuild",
        metavar="PKGBUILD",
        help="Path to the PKGBUILD file to build.",
    )
    p_build.add_argument(
        "--makepkg", "-m",
        metavar="FLAGS",
        help=(
            "Additional makepkg flags, appended after profile makepkg_flags. "
            "Combined short flags are expanded: -sfci becomes -s -f -c -i. "
            "Example: sysforge build PKGBUILD -m '-sfci'"
        ),
    )
    p_build.set_defaults(func=_cmd_build)

    # install
    p_install = sub.add_parser(
        "install",
        help="Run the install pipeline (stages 5-7 active; 1-4 are stubs).",
    )
    p_install.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last checkpoint.",
    )
    p_install.add_argument(
        "--start-from",
        metavar="STAGE",
        dest="start_from",
        help=(
            "Start from this stage, marking all prior stages as skipped. "
            "Useful for running stages 5-7 on a live system: "
            "--start-from packages"
        ),
    )
    p_install.add_argument(
        "--force-retry",
        action="store_true",
        dest="force_retry",
        help="Retry all failed packages without prompting.",
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would run without executing anything.",
    )
    p_install.add_argument(
        "--packages",
        metavar="FILE",
        help="Path to packages.toml (overrides config default).",
    )
    p_install.add_argument(
        "--state-dir",
        metavar="DIR",
        dest="state_dir",
        help=(
            "Override state directory (default: /var/lib/sysforge or "
            "SYSFORGE_STATE_DIR env var)."
        ),
    )
    p_install.set_defaults(func=_cmd_install)

    # manifest
    p_manifest = sub.add_parser(
        "manifest",
        help="Generate a packages.toml stub from a list of package names.",
    )
    p_manifest.add_argument(
        "packages",
        nargs="*",
        metavar="PKG",
        help="Package names to include.",
    )
    p_manifest.add_argument(
        "--file", "-f",
        metavar="FILE",
        help="Text file with one package name per line (can be combined with inline names).",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    # converge
    p_converge = sub.add_parser(
        "converge",
        help="[planned] Rebuild packages that have drifted from their profile.",
    )
    p_converge.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be rebuilt without doing it.",
    )
    p_converge.set_defaults(func=_cmd_converge)

    # resolve
    p_resolve = sub.add_parser(
        "resolve",
        help="[planned] Show which profile would be applied to a package.",
    )
    p_resolve.add_argument(
        "pkg",
        metavar="PKG",
        help="Package name or path to PKGBUILD.",
    )
    p_resolve.add_argument(
        "--show-flags",
        action="store_true",
        help="Print the full resolved flag set.",
    )
    p_resolve.set_defaults(func=_cmd_resolve)

    args = parser.parse_args()
    args.func(args)
