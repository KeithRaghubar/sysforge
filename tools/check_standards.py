#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
tools/check_standards.py - pre-release validator for standards compliance.

Guards the mechanically-checkable subset of the committed standards documented
in docs/design/21-standards.md. The behavioural subset (NO_COLOR, --version,
stdout/stderr discipline, RFC 3339 timestamps, reproducibility) is covered by
tests/test_standards_compliance.py instead.

Run before cutting a release. tools/release.sh invokes it from preflight, and
`make check-standards` / `make pre-release` run it locally.

Usage:
    python tools/check_standards.py [--warn] [--check=GROUP] [--list] [--repo=PATH]

Groups:

    paths       XDG/FHS discipline: no source module other than paths.py may
                *construct* a sysforge user dir (Path.home()/expanduser/Path
                with a "...sysforge..." home-relative literal). Docstrings and
                log messages that merely mention a path are not flagged.
    spdx        Every first-party .py under sysforge/ and tools/ carries an
                SPDX-License-Identifier header. If the `reuse` CLI is on PATH,
                the full-tree `reuse lint` is run instead.
    changelog   Every docs/release-notes/v*.md — and the running accumulator
                unreleased.md — has a `#` title and uses only Keep a Changelog
                `##` category headings.
    encoding    UTF-8 discipline: ruff's PLW1514 (preview) reports no text-mode
                open/read_text/write_text without an explicit encoding. Skipped
                with a warning if ruff is not available.

Drift detection cases (verify these still fire after editing this script):
    - Add `Path.home() / ".cache/sysforge"` to a module other than paths.py.
    - Drop the SPDX header from a sysforge/*.py file.
    - Add a `## Notes` heading to a docs/release-notes/*.md file.
    - Add `open(p, "w")` without encoding= to a sysforge/*.py file.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Finding:
    group: str
    severity: str  # "error" or "warn"
    location: str
    message: str


# ===========================================================================
# Group: paths  (XDG / FHS discipline)
# ===========================================================================

# Path *construction* idioms that build a sysforge user dir. These must route
# through the paths.py constants (USER_CONFIG_DIR / USER_CACHE_DIR /
# USER_STATE_DIR). Matching requires the literal "sysforge" so that legitimate
# non-sysforge home paths (e.g. makepkg.conf under ~/.config, Steam config) and
# plain docstring/log mentions of a path are not flagged.
_PATH_CONSTRUCT_RES = (
    re.compile(r"Path\.home\(\)\s*/[^#\n]*sysforge"),
    re.compile(r"""expanduser\(\s*['"]~[^'"]*sysforge"""),
    re.compile(r"""\bPath\(\s*['"]~[^'"]*sysforge"""),
)

# paths.py is the one home allowed to construct these dirs.
_PATHS_EXEMPT = {"sysforge/primitives/paths.py"}


def check_paths(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for py in sorted((repo / "sysforge").rglob("*.py")):
        rel = py.relative_to(repo).as_posix()
        if rel in _PATHS_EXEMPT:
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if any(rx.search(line) for rx in _PATH_CONSTRUCT_RES):
                findings.append(Finding(
                    "paths", "error", f"{rel}:{lineno}",
                    "constructs a sysforge user dir directly — import "
                    "USER_CONFIG_DIR/USER_CACHE_DIR/USER_STATE_DIR from paths.py",
                ))
    return findings


# ===========================================================================
# Group: spdx  (REUSE / SPDX licensing)
# ===========================================================================

# The header tag we grep for in the fallback path. Isolated here behind
# REUSE-Ignore guards so `reuse` does not mistake this literal for a license
# declaration *on this file* (the file's real header is at the top).
# REUSE-IgnoreStart
_SPDX_TAG = "SPDX-License-Identifier:"
# REUSE-IgnoreEnd


def check_spdx(repo: Path) -> list[Finding]:
    # Authoritative path: full-tree REUSE compliance when the CLI is available.
    if shutil.which("reuse"):
        proc = subprocess.run(
            ["reuse", "--root", str(repo), "lint"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return [Finding("spdx", "error", "reuse lint",
                            "tree is not REUSE-compliant (run `reuse lint` for detail)")]
        return []

    # Fallback: require an SPDX license header in every first-party .py source.
    findings: list[Finding] = []
    for base in ("sysforge", "tools"):
        for py in sorted((repo / base).rglob("*.py")):
            rel = py.relative_to(repo).as_posix()
            head = py.read_text(encoding="utf-8")[:512]
            if _SPDX_TAG not in head:
                findings.append(Finding("spdx", "error", rel,
                                        f"missing {_SPDX_TAG} header"))
    return findings


# ===========================================================================
# Group: changelog  (Keep a Changelog)
# ===========================================================================

_KAC_HEADINGS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}


def check_changelog(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    notes_dir = repo / "docs" / "release-notes"
    if not notes_dir.is_dir():
        return findings
    # Lint published notes (v*.md) plus the running accumulator (unreleased.md),
    # since per-task entries are authored in the accumulator all cycle and must
    # obey the same Keep a Changelog vocabulary before they are renamed.
    notes_files = sorted(notes_dir.glob("v*.md"))
    unreleased = notes_dir / "unreleased.md"
    if unreleased.is_file():
        notes_files.append(unreleased)
    for md in notes_files:
        rel = md.relative_to(repo).as_posix()
        text = md.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not any(ln.startswith("# ") for ln in lines):
            findings.append(Finding("changelog", "error", rel,
                                    "missing a `# ` title heading"))
        for lineno, ln in enumerate(lines, 1):
            if ln.startswith("## "):
                heading = ln[3:].strip()
                if heading not in _KAC_HEADINGS:
                    findings.append(Finding(
                        "changelog", "error", f"{rel}:{lineno}",
                        f"`## {heading}` is not a Keep a Changelog category "
                        f"({', '.join(sorted(_KAC_HEADINGS))})",
                    ))
    return findings


# ===========================================================================
# Group: encoding  (UTF-8 discipline via ruff PLW1514)
# ===========================================================================

def check_encoding(repo: Path) -> list[Finding]:
    if not shutil.which("ruff"):
        return [Finding("encoding", "warn", "ruff",
                        "ruff not found — skipping PLW1514 encoding check")]
    proc = subprocess.run(
        ["ruff", "check", "sysforge", "tools",
         "--select", "PLW1514", "--preview", "--output-format", "concise"],
        cwd=repo, capture_output=True, text=True,
    )
    findings: list[Finding] = []
    for line in proc.stdout.splitlines():
        if "PLW1514" in line:
            loc = line.split(":", 1)[0] if ":" in line else "?"
            findings.append(Finding("encoding", "error", loc,
                                    "text-mode I/O without explicit encoding= (UTF-8)"))
    return findings


# ===========================================================================
# Driver
# ===========================================================================

GROUPS = {
    "paths":     check_paths,
    "spdx":      check_spdx,
    "changelog": check_changelog,
    "encoding":  check_encoding,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pre-release validator for sysforge standards compliance.",
    )
    p.add_argument("--warn", action="store_true",
                   help="downgrade errors to warnings (exit 0 on findings)")
    p.add_argument("--check", action="append", default=[],
                   help="run only this group (repeatable)")
    p.add_argument("--list", action="store_true", help="list groups and exit")
    p.add_argument("--repo", type=Path, default=REPO,
                   help="repo root to validate (default: this script's repo)")
    args = p.parse_args(argv)

    if args.list:
        for name in GROUPS:
            print(name)
        return 0

    selected = args.check or list(GROUPS.keys())
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        print(f"unknown group(s): {unknown}", file=sys.stderr)
        return 2

    repo = args.repo.resolve()
    all_findings: list[Finding] = []
    for name in selected:
        try:
            all_findings.extend(GROUPS[name](repo))
        except Exception as e:
            all_findings.append(Finding(name, "error", "checker",
                                        f"check group crashed: {e!r}"))

    by_group: dict[str, list[Finding]] = {}
    for f in all_findings:
        by_group.setdefault(f.group, []).append(f)

    for group_name in selected:
        group_findings = by_group.get(group_name, [])
        if not group_findings:
            print(f"[OK]   {group_name}")
            continue
        worst = "FAIL" if any(f.severity == "error" for f in group_findings) else "WARN"
        print(f"[{worst}] {group_name}")
        for f in group_findings:
            print(f"  {f.severity.upper():5} {f.location}: {f.message}")

    error_count = sum(1 for f in all_findings if f.severity == "error")
    warn_count = sum(1 for f in all_findings if f.severity == "warn")
    print()
    print(f"summary: {error_count} error(s), {warn_count} warning(s)")
    if error_count and not args.warn:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
