#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
tools/surface.py — the committed snapshot of every user-facing surface (3.5.0-DEV18).

tests/data/surface.json lists what a user can type, set or parse: every command
with its flags and their allowed values, the environment variables and journal
fields, each config file's sections and keys, the fields of each `--json`
document, and the run-log names. It is generated, like DESIGN.md and the man
page, so a change to any surface shows up as a diff of that one file, which is
where a reviewer looks.

    --write          regenerate the snapshot (`make surface`)
    --check          the gate (`make check-surface`):
                       1. the committed snapshot matches the code;
                       2. against the base (the merge-base with main, or the
                          previous commit on main itself), every added or
                          removed entry is named in text this change adds to
                          docs/release-notes/unreleased.md;
                       3. a flag, command, environment variable or config key
                          that leaves the product entirely is registered as
                          retired (tools/retired_names.toml, or for a config
                          key config_schema.RETIRED_KEYS), so it cannot creep
                          back unnoticed.
                     With no snapshot at the base (its first introduction) or
                     no base at all, step 1 runs alone.
    --base REF       compare against REF instead.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SNAPSHOT = "tests/data/surface.json"
NOTES = "docs/release-notes/unreleased.md"


# ---------------------------------------------------------------------------
# Collecting
# ---------------------------------------------------------------------------

def _commands() -> dict:
    import argparse as ap

    from sysforge.verbs.registry import build_parser

    out: dict = {}

    def walk(parser, path):
        flags, positionals = {}, {}
        subs = None
        for a in parser._actions:
            if isinstance(a, ap._SubParsersAction):
                subs = a
                continue
            if a.dest == "help":
                continue
            values = sorted(map(str, a.choices)) if a.choices else None
            if a.option_strings:
                flags[max(a.option_strings, key=len)] = values
            else:
                positionals[a.metavar or a.dest] = values
        # A group parent takes nothing itself; its subcommands carry it all.
        if not (path and subs is not None and not flags and not positionals):
            out[" ".join(path)] = {"flags": dict(sorted(flags.items())),
                                   "positionals": positionals}
        if subs is not None:
            for name, sub in subs.choices.items():
                if (path + [name])[0] == "completions":
                    continue  # the completion scripts' own plumbing
                walk(sub, path + [name])

    walk(build_parser(), [])
    return dict(sorted(out.items()))


def _json_fields(schema: dict, prefix: str = "") -> list[str]:
    fields = []
    for name, sub in sorted(schema.get("properties", {}).items()):
        path = f"{prefix}.{name}" if prefix else name
        fields.append(path)
        if sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            fields += _json_fields(sub["items"], path + "[]")
        elif "properties" in sub:
            fields += _json_fields(sub, path)
    return fields


def collect() -> dict:
    from sysforge.primitives import config_schema as cs
    from sysforge.primitives.env_vars import ENV_VARS
    from sysforge.primitives.journal import JOURNAL_FIELDS
    from sysforge.verbs.registry import verb_log_names

    config = {}
    for name in cs.CONFIG_FILES:
        documented = cs._documented(cs.shipped_text(name) or "")
        sections = {}
        for section in sorted(cs.KNOWN_SECTIONS[name]):
            sections[section] = (None if section in cs.OPEN_SECTIONS.get(name, set())
                                 else sorted(documented.get(section, set())))
        config[name] = {"top_level": sorted(cs.KNOWN_TOP_KEYS[name]), "sections": sections}
    schemas = {}
    for path in sorted((REPO / "tests/data/json-schema").glob("*.schema.json")):
        schemas[path.name.removesuffix(".schema.json")] = _json_fields(
            json.loads(path.read_text(encoding="utf-8")))
    return {
        "commands": _commands(),
        "environment": sorted(v.name for v in ENV_VARS),
        "journal_fields": sorted(f for f, _ in JOURNAL_FIELDS),
        "config": config,
        "json": schemas,
        "run_logs": verb_log_names(),
    }


def render(surface: dict) -> str:
    return json.dumps(surface, indent=1, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def entries(surface: dict) -> dict[str, str]:
    """Flatten a snapshot to {entry id: the name a release note would use}."""
    out: dict[str, str] = {}
    for cmd, spec in surface.get("commands", {}).items():
        if cmd:
            out[f"command: {cmd}"] = cmd
        for flag, values in spec.get("flags", {}).items():
            out[f"flag: {cmd} {flag}".replace("  ", " ")] = flag
            for v in values or []:
                out[f"value: {cmd} {flag}={v}".replace("  ", " ")] = v
        for pos, values in spec.get("positionals", {}).items():
            for v in values or []:
                out[f"value: {cmd} {pos}={v}"] = v
    for name in surface.get("environment", []):
        out[f"environment: {name}"] = name
    for name in surface.get("journal_fields", []):
        out[f"journal field: {name}"] = name
    for file, spec in surface.get("config", {}).items():
        for key in spec.get("top_level", []):
            out[f"config: {file} {key}"] = key
        for section, keys in spec.get("sections", {}).items():
            out[f"config: {file} [{section}]"] = section
            for key in keys or []:
                out[f"config: {file} [{section}] {key}"] = key
    for kind, fields in surface.get("json", {}).items():
        out[f"json: {kind}"] = kind
        for field in fields:
            out[f"json: {kind}.{field}"] = field.rsplit(".", 1)[-1].removesuffix("[]")
    for name in surface.get("run_logs", []):
        out[f"run log: {name}"] = name
    return out


def _all_names(surface: dict, kinds: tuple[str, ...]) -> set[str]:
    return {name for eid, name in entries(surface).items() if eid.split(":")[0] in kinds}


def _retired(repo: Path) -> set[str]:
    names = set()
    reg = repo / "tools/retired_names.toml"
    if reg.is_file():
        names |= {e["name"] for e in tomllib.loads(reg.read_text(encoding="utf-8"))
                  .get("retired", [])}
    from sysforge.primitives import config_schema as cs
    names |= {r.key for r in cs.RETIRED_KEYS}
    return names


def judge(base: dict, head: dict, added_notes: str, retired: set[str]) -> list[str]:
    """Problems with going from ``base`` to ``head``; [] when it is fine."""
    old, new = entries(base), entries(head)
    problems = []
    for eid in sorted(set(new) - set(old)):
        if not re.search(rf"(?<![\w-]){re.escape(new[eid])}(?![\w-])", added_notes):
            problems.append(f"added {eid}: name `{new[eid]}` in a release-note entry")
    gone_everywhere = {
        "flag": _all_names(base, ("flag",)) - _all_names(head, ("flag",)),
        "command": _all_names(base, ("command",)) - _all_names(head, ("command",)),
        "environment": _all_names(base, ("environment",)) - _all_names(head, ("environment",)),
        "config": _all_names(base, ("config",)) - _all_names(head, ("config",)),
    }
    for eid in sorted(set(old) - set(new)):
        name = old[eid]
        if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", added_notes):
            problems.append(f"removed {eid}: name `{name}` in a release-note entry")
        kind = eid.split(":")[0]
        if kind in gone_everywhere and name in gone_everywhere[kind] and name not in retired:
            problems.append(f"removed {eid}: `{name}` is gone entirely; add it to "
                            "tools/retired_names.toml (a config key: "
                            "config_schema.RETIRED_KEYS)")
    return problems


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def default_base() -> str | None:
    """The merge-base with main (origin/main first, as in CI); on main itself,
    the previous commit, since a squash merge lands as one."""
    head = (_git("rev-parse", "HEAD") or "").strip()
    for ref in ("origin/main", "main"):
        base = (_git("merge-base", "HEAD", ref) or "").strip()
        if base and base != head:
            return base
        if base == head:
            prev = (_git("rev-parse", "HEAD^") or "").strip()
            return prev or None
    return None


def _added_text(base: str) -> str:
    """Lines this change adds to the release notes (the whole file when the base
    has none)."""
    old = _git("show", f"{base}:{NOTES}") or ""
    new = (REPO / NOTES).read_text(encoding="utf-8") if (REPO / NOTES).is_file() else ""
    old_lines = set(old.splitlines())
    return "\n".join(line for line in new.splitlines() if line not in old_lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    p.add_argument("--base", metavar="REF")
    args = p.parse_args(argv)
    current = collect()
    path = REPO / SNAPSHOT
    if args.write:
        path.write_text(render(current), encoding="utf-8")
        print(f"wrote {SNAPSHOT}")
        return 0
    if not path.is_file() or path.read_text(encoding="utf-8") != render(current):
        print(f"[FAIL] {SNAPSHOT} is out of date with the code: run `make surface` and "
              "commit it (its diff is the surface change to review)")
        return 1
    base = args.base or default_base()
    base_snapshot = _git("show", f"{base}:{SNAPSHOT}") if base else None
    if base is None or base_snapshot is None:
        print(f"[OK]   {SNAPSHOT} is current (no snapshot at the base to compare with)")
        return 0
    problems = judge(json.loads(base_snapshot), current, _added_text(base), _retired(REPO))
    if problems:
        print(f"[FAIL] surface changes since {base[:12]} need a release note or a "
              "retirement record:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"[OK]   {SNAPSHOT} is current, and every change since {base[:12]} is recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
