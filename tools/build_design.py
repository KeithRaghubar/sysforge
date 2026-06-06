#!/usr/bin/env python3
"""build_design.py -- generate DESIGN.md from the modular docs/design/ sources.

DESIGN.md is a GENERATED aggregate: the source of truth is the set of focused
files under docs/design/, concatenated in the order listed in
docs/design/_manifest, under a generated banner. This mirrors the `make man`
generate-and-check pattern -- edit the sources, run `make design`, and
`make check-design` guards against the committed DESIGN.md drifting from the
sources (wired into the release preflight alongside the manpage drift check).

Usage:
    python tools/build_design.py            # regenerate DESIGN.md in place
    python tools/build_design.py --check     # exit 1 if DESIGN.md is stale
    python tools/build_design.py --repo DIR  # operate on a different tree (tests)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BANNER = (
    "<!-- GENERATED FILE -- do not edit directly.\n"
    "     Source of truth: the modular files under docs/design/ (see\n"
    "     docs/design/index.md and docs/design/_manifest). Edit those, then run\n"
    "     `make design`; `make check-design` guards against drift. -->\n\n"
)


def _manifest_files(repo: Path) -> list[Path]:
    """Resolve the ordered source files listed in docs/design/_manifest."""
    design = repo / "docs" / "design"
    manifest = design / "_manifest"
    files: list[Path] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path = design / line
        if not path.is_file():
            raise FileNotFoundError(f"_manifest lists a missing source: {line}")
        files.append(path)
    return files


def render(repo: Path) -> str:
    """Return the full generated DESIGN.md text (banner + concatenated sources)."""
    parts = [_BANNER]
    for path in _manifest_files(repo):
        parts.append(path.read_text(encoding="utf-8"))
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate DESIGN.md from docs/design/.")
    ap.add_argument(
        "--repo", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the parent of tools/)",
    )
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if DESIGN.md is stale instead of writing it")
    args = ap.parse_args()
    repo = args.repo.resolve()

    generated = render(repo)
    target = repo / "DESIGN.md"

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != generated:
            print("[FAIL] DESIGN.md is out of date with docs/design/ -- "
                  "run `make design` and commit the result.", file=sys.stderr)
            return 1
        print("[OK]   DESIGN.md matches its docs/design/ sources")
        return 0

    target.write_text(generated, encoding="utf-8")
    print(f"wrote {target.name} ({len(generated.splitlines())} lines) "
          f"from {len(_manifest_files(repo))} docs/design/ sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
