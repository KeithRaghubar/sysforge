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

The merge is **key-anchored** — it can only carry comments that lead an active
key it injects. Pure documentation comments and *commented-out example*
settings (``# interactive = true``) have no key to anchor to, so when a shipped
file gains such content the live file lacks, the shipped file is written verbatim
beside the target as ``<name>.sfnew`` (pacnew-style) for the operator to diff and
adopt manually. A stale ``.sfnew`` is removed once the drift is resolved.

tomlkit is a **dev-only** dependency, pulled into an ephemeral uv overlay by the
`make sync-config` target - it is intentionally not in pyproject.toml, so the
shipped runtime stays dependency-light.

Usage:
    python tools/sync_config.py [--dry-run] [--target DIR]

    --target DIR  live config dir to update (the dir that *contains* the
                  TOML files). Default: $SYSFORGE_CONFIG_DIR itself,
                  mirroring sysforge.primitives.paths.CONFIG_DIR resolution
                  (the env var is the config dir directly; falls back to
                  /etc/sysforge when SYSFORGE_CONFIG_DIR unset).
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
    # SYSFORGE_CONFIG_DIR is the config dir itself (mirrors paths.CONFIG_DIR),
    # not an FHS root prefix; fall back to the FHS system path when unset.
    val = os.environ.get("SYSFORGE_CONFIG_DIR")
    return Path(val) if val else Path("/etc/sysforge")


def _is_section(item) -> bool:
    return isinstance(item, (Table, AoT))


def _comment_signature(text: str) -> set[str]:
    """Normalized set of full-line comments in a TOML text.

    Captures both documentation comments and *commented-out example* settings
    (``# interactive = true``) — the content the key-anchored merge can't see
    because it isn't an active key. Leading ``#`` and surrounding whitespace are
    stripped so cosmetic spacing differences don't register as drift.
    """
    sig: set[str] = set()
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            sig.add(stripped.lstrip("#").strip())
    sig.discard("")
    return sig


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
        # ``pending`` is the comment/whitespace run that preceded this key. For a
        # *new* key it was consumed above (its leading comments travel with it).
        # For an *existing* key/table it is intentionally not spliced in-place —
        # locating the right insertion point idempotently is fragile, and any
        # such comment-only drift (incl. before an existing table) is surfaced
        # comprehensively by the ``.sfnew`` companion in ``sync_file``.
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


def sync_file(shipped: Path, target: Path,
              dry_run: bool) -> tuple[str, list[str], Path | None]:
    """Sync one shipped file into target.

    Returns ``(status, added-paths, sfnew_path)``. ``status`` is one of
    ``created`` / ``updated`` / ``up to date``. ``sfnew_path`` is the path of a
    written ``<name>.sfnew`` companion (or ``None``): the key-anchored merge can
    only inject active keys (and their leading comments), so when the shipped
    file carries documentation comments or commented-out example settings the
    live file still lacks after the merge, the shipped file is dropped verbatim
    beside the target — pacnew-style — for the operator to diff and adopt.
    """
    if not target.exists():
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shipped, target)
        return ("created", [], None)

    shipped_text = shipped.read_text(encoding="utf-8")
    src = tomlkit.parse(shipped_text)
    dst = tomlkit.parse(target.read_text(encoding="utf-8"))
    added = merge_container(src, dst)
    merged_text = tomlkit.dumps(dst)
    if added and not dry_run:
        target.write_text(merged_text, encoding="utf-8")

    # Comment/example drift the key-merge cannot carry → .sfnew companion.
    # Compare against the *post-merge* text so newly-injected keys' comments
    # don't count as missing.
    sfnew_path = target.with_name(target.name + ".sfnew")
    missing_comments = _comment_signature(shipped_text) - _comment_signature(merged_text)
    sfnew_written: Path | None = None
    if missing_comments:
        sfnew_written = sfnew_path
        if not dry_run:
            sfnew_path.write_text(shipped_text, encoding="utf-8")
    elif sfnew_path.exists() and not dry_run:
        # Drift resolved (operator adopted the comments) — clean up the stale
        # companion so the dir doesn't accumulate orphaned .sfnew files.
        sfnew_path.unlink()

    status = "updated" if added else "up to date"
    return (status, added, sfnew_written)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, default=None,
                    help="live config dir (default: $SYSFORGE_CONFIG_DIR, "
                         "else /etc/sysforge)")
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
    sfnew_count = 0
    wrote = "would write" if args.dry_run else "wrote"
    for shipped in sorted(SHIPPED_DIR.glob("*.toml")):
        if shipped.name in SKIP:
            continue
        target = target_dir / shipped.name
        status, added, sfnew = sync_file(shipped, target, args.dry_run)
        if sfnew is not None:
            sfnew_count += 1
        if status == "up to date":
            note = (f": comments/examples differ — {wrote} {sfnew.name}"
                    if sfnew is not None else "")
            print(f"  {shipped.name}: up to date{note}")
            continue
        changed += 1
        if status == "created":
            print(f"  {shipped.name}: created (copied wholesale)")
            continue
        print(f"  {shipped.name}: +{len(added)} added")
        for path in added:
            print(f"      + {path}")
        if sfnew is not None:
            print(f"      ~ comments/examples differ — {wrote} {sfnew.name}")

    verb = "would update" if args.dry_run else "updated"
    print(f"-> {changed} file(s) {verb}, your values preserved")
    if sfnew_count:
        print(f"-> {sfnew_count} .sfnew companion(s) {wrote} for comment/example "
              "drift — diff & adopt manually")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
