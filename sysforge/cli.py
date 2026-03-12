import argparse
import sys
from sysforge.primitives.makepkg_wrapper import run


def main():
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Build a package from a PKGBUILD using resolved flag profiles.",
    )
    parser.add_argument(
        "pkgbuild",
        metavar="PKGBUILD",
        help="Path to the PKGBUILD file to build.",
    )
    args = parser.parse_args()

    try:
        run(args.pkgbuild)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)
