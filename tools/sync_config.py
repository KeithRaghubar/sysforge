#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
tools/sync_config.py - adopt new shipped config defaults into a live config dir.

The shipped defaults under etc/sysforge/*.toml are kept up to date, but a live
config dir does not auto-update (by design). On an installed system pacman's
backup=()/.pacnew handles this; in a from-repo dev setup (SYSFORGE_CONFIG_DIR
pointed at a working tree) nothing does. This tool fills that gap.

It performs an **add-only**, comment-preserving merge: for each shipped TOML it
injects keys, tables, and their leading comment blocks that the live file is
missing, and never touches a value the live file already sets (even if the
shipped default changed). Arrays-of-tables ([[package]]) are user content and
are left entirely alone.

tomlkit is a **dev-only** dependency, pulled into an ephemeral uv overlay by the
`make sync-config` target - it is intentionally not in pyproject.toml, so the
shipped runtime stays dependency-light.

Usage:
    python tools/sync_config.py [--dry-run] [--target DIR]

    --target DIR  live config dir to update (the dir that *contains* the
                  TOML files). Default: $SYSFORGE_CONFIG_DIR/etc/sysforge,
                  mirroring sysforge.primitives.paths.CONFIG_BASE resolution
                  (falls back to /etc/sysforge when SYSFORGE_CONFIG_DIR unset).
    --dry-run     report what would change; write nothing.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Table, Whitespace

REPO = Path(__file__).resolve().parent.parent
SHIPPED_DIR = REPO / "etc/sysforge"
# bootstrap.toml ships as /usr/share/sysforge/bootstrap.toml.example (per-host);
# it has no live counterpart - same exclusion as tools/check_shipped.py.
SKIP = {"bootstrap.toml"}


def _default_target() -> Path:
    base = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))
    return base / "etc/sysforge"


def _is_section(item) -> bool:
    return isinstance(item, (Table, AoT))


def _first_section_index(body) -> int:
    """Index of the first table/AoT header in a container body (or len)."""
    for i, (key, item) in enumerate(body):
        if key is not None and _is_section(item):
            return i
    return len(body)


def merge_container(src, dst, prefix: str = "") -> list[str]:
    """Add-only merge of src into dst (both tomlkit Containers).

    Returns the dotted paths of everything injected. Existing dst keys are
    never overwritten; tables present in both are recursed into. Bare keys are
    spliced *before* the first table (TOML requires bare keys to precede table
    headers, else they would be absorbed into the preceding table on reparse);
    new tables are appended at the end. Leading comment/whitespace runs travel
    with the item they precede.
    """
    added: list[str] = []
    dst_keys = {key.key for key, _ in dst.body if key is not None}
    pending: list = []      # contiguous (None, Comment/Whitespace) run
    bare_block: list = []   # trivia+item runs to splice before first table
    sect_block: list = []   # trivia+item runs to append at end

    for key, item in src.body:
        if key is None:
            pending.append((key, item))
            continue
        name = key.key
        if name not in dst_keys:
            run = list(pending) + [(key, item)]
            (sect_block if _is_section(item) else bare_block).extend(run)
            added.append(prefix + name)
        elif isinstance(item, Table) and not isinstance(item, AoT):
            dst_item = dst[name]
            if isinstance(dst_item, Table):
                added += merge_container(item.value, dst_item.value,
                                         prefix + name + ".")
        pending = []

    if bare_block:
        idx = _first_section_index(dst.body)
        block = list(bare_block)
        if idx > 0 and not isinstance(dst.body[idx - 1][1], Whitespace):
            block = [(None, Whitespace("\n"))] + block
        if idx < len(dst.body) and not isinstance(dst.body[idx][1], Whitespace):
            block = block + [(None, Whitespace("\n"))]
        dst.body[idx:idx] = block
    if sect_block:
        if dst.body and not isinstance(dst.body[-1][1], Whitespace):
            sect_block = [(None, Whitespace("\n"))] + sect_block
        dst.body.extend(sect_block)
    return added


def sync_file(shipped: Path, target: Path, dry_run: bool) -> tuple[str, list[str]]:
    """Sync one shipped file into target. Returns (status, added-paths)."""
    if not target.exists():
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shipped, target)
        return ("created", [])

    src = tomlkit.parse(shipped.read_text(encoding="utf-8"))
    dst = tomlkit.parse(target.read_text(encoding="utf-8"))
    added = merge_container(src, dst)
    if added and not dry_run:
        target.write_text(tomlkit.dumps(dst), encoding="utf-8")
    return ("updated" if added else "up to date", added)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, default=None,
                    help="live config dir (default: $SYSFORGE_CONFIG_DIR/etc/sysforge)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes without writing")
    args = ap.parse_args(argv)

    target_dir = (args.target or _default_target()).expanduser()
    if not SHIPPED_DIR.is_dir():
        print(f"error: shipped dir not found: {SHIPPED_DIR}", file=sys.stderr)
        return 2

    tag = " (dry-run)" if args.dry_run else ""
    print(f"sync-config{tag}: {SHIPPED_DIR}  ->  {target_dir}")

    changed = 0
    for shipped in sorted(SHIPPED_DIR.glob("*.toml")):
        if shipped.name in SKIP:
            continue
        target = target_dir / shipped.name
        status, added = sync_file(shipped, target, args.dry_run)
        if status == "up to date":
            print(f"  {shipped.name}: up to date")
            continue
        changed += 1
        if status == "created":
            print(f"  {shipped.name}: created (copied wholesale)")
            continue
        print(f"  {shipped.name}: +{len(added)} added")
        for path in added:
            print(f"      + {path}")

    verb = "would update" if args.dry_run else "updated"
    print(f"-> {changed} file(s) {verb}, your values preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
