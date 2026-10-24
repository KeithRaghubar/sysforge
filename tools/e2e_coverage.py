# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""e2e_coverage.py — which commands the end-to-end tier runs (3.5.0-DEV23).

Reads the commands from tests/data/surface.json and the `sysforge <command>`
lines of tools/e2e/scenarios/*.sh. Every command must be run by a scenario or
listed, with its reason, in tools/e2e/uncovered.toml; a listed command that a
scenario now runs is an error too, so the list names only real gaps. A new
command therefore shows up here instead of going untested unnoticed.

Usage:
    python tools/e2e_coverage.py           # report; exit 1 on an unlisted gap or a stale listing
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "tools" / "e2e" / "scenarios"
UNCOVERED = REPO / "tools" / "e2e" / "uncovered.toml"
SURFACE = REPO / "tests" / "data" / "surface.json"

# `sysforge` as a command word, not a path component (/etc/sysforge/...).
_INVOCATION = re.compile(r"(?<![\w/.-])sysforge((?:\s+[\w-]+)+)")


def commands() -> set[str]:
    return set(json.loads(SURFACE.read_text())["commands"])


def uncovered() -> dict[str, str]:
    with UNCOVERED.open("rb") as f:
        return tomllib.load(f)["uncovered"]


def covered(known: set[str], scenarios: Path = SCENARIOS) -> dict[str, list[str]]:
    """Each command a scenario runs, with the scenarios that run it."""
    found: dict[str, list[str]] = {}
    for path in sorted(scenarios.glob("*.sh")):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in _INVOCATION.finditer(line):
                words = [w for w in m.group(1).split() if not w.startswith("-")]
                for n in (2, 1):
                    cmd = " ".join(words[:n])
                    if len(words) >= n and cmd in known:
                        found.setdefault(cmd, [])
                        if path.stem not in found[cmd]:
                            found[cmd].append(path.stem)
                        break
    return found


def problems(known: set[str], runs: dict[str, list[str]], listed: dict[str, str]) -> list[str]:
    out = []
    for cmd in sorted(known - set(runs) - set(listed)):
        out.append(f"`sysforge {cmd}` is run by no scenario and not listed in {UNCOVERED.name}")
    for cmd in sorted(set(listed) & set(runs)):
        by = ", ".join(runs[cmd])
        out.append(f"`sysforge {cmd}` is listed in {UNCOVERED.name} but {by} runs it")
    for cmd in sorted(set(listed) - known):
        out.append(f"{UNCOVERED.name} lists `{cmd}`, which is not a command")
    for cmd, reason in sorted(listed.items()):
        if not str(reason).strip():
            out.append(f"{UNCOVERED.name} gives no reason for `{cmd}`")
    return out


def main() -> int:
    known, listed = commands(), uncovered()
    runs = covered(known)
    width = max(len(c) for c in known) + 2
    print(f"Run by a scenario ({len(runs)} of {len(known)}):")
    for cmd in sorted(runs):
        print(f"  {cmd:<{width}}{', '.join(runs[cmd])}")
    print(f"Not run, listed in {UNCOVERED.name} ({len(listed)}):")
    for cmd in sorted(listed):
        print(f"  {cmd or '(bare)':<{width}}{listed[cmd]}")
    errors = problems(known, runs, listed)
    for e in errors:
        print(f"ERROR {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
