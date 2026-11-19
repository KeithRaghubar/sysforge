#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""build_design.py -- generate DESIGN.md from the modular docs/design/ sources.

DESIGN.md is a GENERATED aggregate: the source of truth is the set of focused
files under docs/design/, concatenated in the order listed in
docs/design/_manifest, under a generated banner. This mirrors the `make man`
generate-and-check pattern -- edit the sources, run `make design`, and
`make check-design` guards against the committed DESIGN.md drifting from the
sources (wired into the release preflight alongside the manpage drift check).
Two markers are expanded at build time: `<!--TOC-->` (the section index) and
`<!--MODULE-TREE-->` (the sysforge/ package tree, from module docstrings).

Usage:
    python tools/build_design.py            # regenerate DESIGN.md in place
    python tools/build_design.py --check     # exit 1 if DESIGN.md is stale
    python tools/build_design.py --repo DIR  # operate on a different tree (tests)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Placeholder line (in docs/design/00-header.md) replaced by the generated
# Table of Contents, derived from the `##` section headings in the assembled
# document. Keeps the TOC in sync automatically as sections move/rename.
_TOC_MARKER = "<!--TOC-->"

# Placeholder line (in docs/design/04-directory-structure.md) replaced by the
# sysforge/ package tree, one line per module, each described by the first
# paragraph of its docstring (4.0.0-DEV7). The hand-kept tree it replaces had
# fallen 80 modules behind; generated, a new or re-described module makes
# `make check-design` fail until `make design` picks it up.
_TREE_MARKER = "<!--MODULE-TREE-->"
# `name.py — `, `stages/base.py -- `, `sysforge.ui — `, `stages/kernel/ — `: the
# docstring repeating its own name (a `.` or `/` keeps a plain first word).
_SELF_NAME_RE = re.compile(r"^[\w]+[./][\w/.]*\s+(?:—|--|-)\s+")

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


def _slugify(title: str) -> str:
    """GitHub-style heading anchor: lowercase, drop punctuation, spaces->hyphens."""
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)   # drop everything but word chars, space, hyphen
    s = re.sub(r"\s+", "-", s)
    return s


def _generate_toc(body: str) -> str:
    """Build a numbered TOC from the `## ` section headings in body.

    Skips fenced code blocks (``` ... ```), the TOC heading itself, and any
    deeper headings (### and below) -- the TOC indexes top-level sections only.
    """
    headings: list[str] = []
    in_code = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^## (.+)$", line)   # exactly two hashes (### won't match)
        if not m:
            continue
        title = m.group(1).strip()
        if title.lower() == "table of contents":
            continue
        headings.append(title)
    return "\n".join(
        f"{i}. [{t}](#{_slugify(t)})" for i, t in enumerate(headings, 1)
    )


def _module_summary(path: Path) -> str | None:
    """The first paragraph of a module's docstring, on one line, without the
    file name it often opens with; None when it has no docstring."""
    import ast
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    if not doc:
        return None
    para = " ".join(doc.strip().split("\n\n")[0].split())
    return _SELF_NAME_RE.sub("", para).replace("``", "`").rstrip(".")


def _tree_lines(pkg: Path, prefix: str, missing: list[str], repo: Path) -> list[str]:
    entries = sorted(
        (p for p in pkg.iterdir()
         if p.name != "__pycache__" and not p.name.startswith("_data")
         # primitives/ is a namespace package: a directory counts by its modules.
         and (p.suffix == ".py" or (p.is_dir() and any(p.glob("*.py"))))
         and p.name != "__init__.py"),
        key=lambda p: (p.is_dir(), p.name))
    lines: list[str] = []
    for i, p in enumerate(entries):
        last = i == len(entries) - 1
        branch, pad = ("└── ", "    ") if last else ("├── ", "│   ")
        if p.is_dir():
            init = p / "__init__.py"
            summary = _module_summary(init) if init.is_file() else None
            lines.append(f"{prefix}{branch}{p.name}/" + (f"  # {summary}" if summary else ""))
            lines += _tree_lines(p, prefix + pad, missing, repo)
        else:
            summary = _module_summary(p)
            if summary is None:
                missing.append(p.relative_to(repo).as_posix())
            lines.append(f"{prefix}{branch}{p.name}  # {summary or '(no docstring)'}")
    return lines


def generate_module_tree(repo: Path) -> str:
    """The sysforge/ package as a fenced tree. Raises SystemExit naming any
    module without a docstring: the tree is only as good as its descriptions."""
    pkg = repo / "sysforge"
    if not (pkg / "__init__.py").is_file():
        return "```\nsysforge/  (package not present in this tree)\n```"
    missing: list[str] = []
    lines = ["sysforge/"] + _tree_lines(pkg, "", missing, repo)
    if missing:
        raise SystemExit("[FAIL] modules without a docstring (the DESIGN.md module tree "
                         "describes each by its first paragraph):\n  " + "\n  ".join(missing))
    return "```\n" + "\n".join(lines) + "\n```"


def render(repo: Path) -> str:
    """Return the full generated DESIGN.md text (banner + sources + injected
    TOC and module tree)."""
    body = _BANNER + "".join(p.read_text(encoding="utf-8") for p in _manifest_files(repo))
    # Only a marker alone on its line: prose that names it (design 17) stays prose.
    tree_line = re.compile(rf"^{re.escape(_TREE_MARKER)}$", re.MULTILINE)
    if tree_line.search(body):
        body = tree_line.sub(lambda _: generate_module_tree(repo), body)
    return body.replace(_TOC_MARKER, _generate_toc(body))


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
