"""
cli.py — SysForge command-line interface

Implemented commands:
    sysforge build <PKGBUILD>   Build a single package using its matched profile

Stubbed commands (pipeline not yet implemented):
    sysforge install            Full pipeline: partition → base install → toolchain → packages → kernel → configure
    sysforge converge           Rebuild packages whose profile, flags, or version have drifted
    sysforge resolve <pkg>      Show which profile would be applied to a package and why
"""
import argparse
import sys

from sysforge.primitives.makepkg_wrapper import run


def _cmd_build(args):
    try:
        run(args.pkgbuild)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_install(args):
    # TODO: full pipeline — partition, base_install, hardware_detection,
    # toolchain, packages, kernel, configure stages.
    # Requires: pipeline DAG, hardware detection, packages.toml manifest.
    print("[SYSFORGE] 'install' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_converge(args):
    # TODO: compare installed state in /var/lib/sysforge/build_state.toml
    # against current manifest and flag profiles; rebuild drifted packages.
    # --dry-run should show what would be rebuilt without doing it.
    # Requires: pipeline DAG, build state tracking.
    print("[SYSFORGE] 'converge' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_resolve(args):
    # TODO: parse the named package's PKGBUILD (fetched from AUR or a local path),
    # run rule matching and profile resolution, and print the resolved profile.
    # --show-flags prints the full resolved flag set.
    # Requires: AUR fetch or local PKGBUILD path resolution.
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
    p_build.set_defaults(func=_cmd_build)

    # install
    p_install = sub.add_parser(
        "install",
        help="[planned] Run full install pipeline from Arch ISO.",
    )
    p_install.add_argument(
        "--config",
        metavar="DIR",
        help="Config directory (default: ~/.config/sysforge/).",
    )
    p_install.set_defaults(func=_cmd_install)

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
