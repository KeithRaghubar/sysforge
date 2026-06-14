#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""check_personal.py -- de-personalization lint gate (refactor Phase 4).

Fails if personal *identity* or *personal-path* tokens leak into the surface
sysforge publishes (docs, source comments/docstrings, shipped configs). The goal
is that everything shipped reads impersonally. Concrete hardware/toolchain facts
are deliberately KEPT -- they are useful supported-hardware examples -- so this
gate targets identity and private home paths only, never hardware strings.

Deny tokens (a line containing any of these is a violation):
  - the personal possessive            "Keith"+"'s"
  - the personal name in prose         "Keith Raghubar"
  - an absolute personal home path      /home/keith
  - the personal state directory        ~/sf-state
  - a personal git namespace            github.com[:/]keith/

Legitimate attribution is allowed: any copyright / maintainer / manpage
--author line is exempt (those are *supposed* to carry the name), and the
functional repository URL (github.com/KeithRaghubar/sysforge) is never matched
by the deny patterns -- they match a lowercase "/keith/" namespace, not the
"KeithRaghubar" org slug.

Scanned: *.md (README, DESIGN, CLAUDE, docs/**), *.py (sysforge/, tools/),
shipped etc/sysforge/*.toml, *.sh and *.hook -- including the committed subset
of .claude/ (hooks, agents, skills, hookify rules), which travels with the repo
and must read impersonally. Excluded: .git, .venv, .remember/ (rolling
handoffs), .claude/worktrees/ (local git-worktree checkouts), tests/ (fixtures
genericized separately), LICENSE (copyright), the generated man/sysforge.1 (its
inputs are scanned instead), and this checker itself (it holds the deny tokens
as literals). Note .json (e.g. .claude/settings.local.json) is not a scanned
suffix and is gitignored, so the per-machine allowlist never reaches the gate.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# (compiled pattern, human-readable reason). The possessive / home-path tokens
# are inherently personal and never occur in legitimate attribution, so they
# need no allowance; the bare name is handled by the _ALLOW exemption below.
_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Keith['‘’]s"), "personal possessive"),
    (re.compile(r"\bKeith\s+Raghubar\b"), "personal name in prose"),
    (re.compile(r"/home/keith\b"), "absolute personal home path"),
    (re.compile(r"~/sf-state\b"), "personal state directory"),
    (re.compile(r"github\.com[:/]keith/"), "personal git namespace"),
]

# A line that trips a deny pattern is EXEMPT if it is an attribution line --
# copyright notices, PKGBUILD Maintainer fields, the manpage --author arg, and
# SPDX-FileCopyrightText/SPDX-FileContributor headers are supposed to carry the
# real name. (SPDX-FileCopyrightText is not matched by \bcopyright\b because the
# word boundary fails inside "FileCopyrightText".)
_ALLOW = re.compile(
    r"(?i)\bcopyright\b|\bmaintainer\b|--author|SPDX-File(?:CopyrightText|Contributor)"
)

# Generated/transient trees and never-published workflow state are out of scope.
# `.claude/` IS scanned (its shared subset -- hooks, agents, skills, hookify
# rules -- is committed and must stay impersonal), but `worktrees` skips the
# local git-worktree checkouts under `.claude/worktrees/` (full repo copies),
# and `.remember/` (rolling handoffs) stays excluded by convention.
_EXCLUDE_DIRS = {
    ".git", ".venv", "venv", ".remember", "worktrees", "node_modules",
    "__pycache__", ".pytest_cache", ".ruff_cache", "htmlcov", "builds", "tests",
}
# Generated artifacts / legal text / this checker -- excluded outright.
_EXCLUDE_FILES = {
    "LICENSE",
    "man/sysforge.1",           # generated from --author (scanned at its source)
    "tools/check_personal.py",  # holds the deny tokens as literals
}
_INCLUDE_SUFFIXES = {".md", ".py", ".toml", ".sh", ".hook"}


@dataclass
class Violation:
    path: str
    lineno: int
    reason: str
    text: str


def _iter_files(repo: Path):
    for p in sorted(repo.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(repo)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in _EXCLUDE_FILES:
            continue
        if p.suffix not in _INCLUDE_SUFFIXES:
            continue
        yield p, rel


def scan(repo: Path) -> list[Violation]:
    out: list[Violation] = []
    for path, rel in _iter_files(repo):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _ALLOW.search(line):
                continue
            for pat, reason in _DENY:
                if pat.search(line):
                    out.append(Violation(rel.as_posix(), lineno, reason, line.strip()))
                    break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="De-personalization lint gate.")
    ap.add_argument(
        "--repo", type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root to scan (default: the parent of tools/)",
    )
    args = ap.parse_args()
    repo = args.repo.resolve()

    violations = scan(repo)
    if not violations:
        print("[OK]   check-personal: no personal identity/path tokens in the published surface")
        return 0

    print(f"[FAIL] check-personal: {len(violations)} personal reference(s) found\n")
    for v in violations:
        print(f"  {v.path}:{v.lineno}: {v.reason}")
        print(f"      {v.text}")
    print("\nFix: rephrase impersonally (keep hardware facts). Legitimate")
    print("attribution must read as a copyright / maintainer / --author line.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
