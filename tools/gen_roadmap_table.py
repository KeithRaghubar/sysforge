#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""gen_roadmap_table.py -- generate the Planned summary table in ROADMAP.md.

ROADMAP.md's `## Planned` section opens with a GENERATED at-a-glance table
(ID / item / priority / effort / bump), derived from the `*Priority: <level> ·
Effort: <size> · Bump: <patch|minor|major>*` tag that closes every Planned
entry. This mirrors the `make design` / `make check-design` generate-and-check
pattern -- edit the entries (and their tags), run `make roadmap-table`, and
`make check-roadmap-table` guards against the committed table drifting from the
entries (wired into the release preflight).

The tool is the *sole* parser of the Priority/Effort/Bump tag grammar, so it
also validates the tags: a Planned entry missing a tag, or using a level/size/
bump outside the allowed vocabulary, is a hard error in both modes (there is no
valid table to emit from a malformed entry). It shares the Bump *vocabulary*
(not the parsing) with `check_standards.py` via `tools/_semver_vocab.py`.

`--print` renders the same table to stdout without touching the file, and
`--sort COLUMN` reorders it on any column (ordinal columns sort by their
vocabulary rank, not alphabetically). The in-file table is *always* written in
the canonical triage order regardless of `--sort`, so `--check` stays
deterministic and a sorted view can never be committed by accident.

The printed view is formatted for a terminal rather than for the file: cells are
padded to their column width and the Item column is truncated to whatever the
line has left, so every row occupies exactly one line at `--width` (default: the
terminal, or 80 when piped). The in-file table is neither padded nor truncated.

Usage:
    python tools/gen_roadmap_table.py            # rewrite the table in place
    python tools/gen_roadmap_table.py --check     # exit 1 if the table is stale
    python tools/gen_roadmap_table.py --print --sort effort   # view, don't write
    python tools/gen_roadmap_table.py --repo DIR  # operate on another tree (tests)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Priority/Effort below are this tool's own vocabulary. Bump is NOT: it is the
# SemVer impact the entry will carry when it lands (standards row 3), and
# check_standards.py derives the same values from the release-notes accumulator.
# One definition in tools/_semver_vocab.py, imported by both, so they cannot drift.
from _semver_vocab import BUMP_ORDER

REPO = Path(__file__).resolve().parent.parent

_BEGIN = "<!-- BEGIN roadmap-table"
_END = "<!-- END roadmap-table -->"

# Allowed vocabulary, ordered best-to-triage first (used as the sort rank).
_PRIORITY_ORDER = ["high", "med", "low"]
_EFFORT_ORDER = ["small", "medium", "large"]

# An entry bullet: `- **`<ID>` — <title>.** ...`. The ID is a code-span; the
# title is the remaining bold text up to the closing `**` of the lead span.
_ENTRY_RE = re.compile(r"^- \*\*`(\d+\.\d+\.\d+-[A-Z]+\d+)`\s*—\s*(.*?)\*\*", re.DOTALL)
# The machine-readable tag closing a Planned entry.
_TAG_RE = re.compile(
    r"\*Priority:\s*(\w+)\s*·\s*Effort:\s*(\w+)\s*·\s*Bump:\s*(\w+)\*")
# ID grammar for sorting: <version>-<TYPE><n>.
_ID_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)-([A-Z]+)(\d+)")


@dataclass
class Entry:
    id: str
    title: str
    priority: str
    effort: str
    bump: str


class RoadmapError(Exception):
    """A Planned entry is missing or has an invalid Priority/Effort tag."""


def _planned_section(text: str) -> str:
    """The text of `## Planned` up to the next `##` heading (the entries only).

    A bare `---` is deliberately *not* a terminator: entries are separated by
    horizontal rules for readability, so treating one as the section end would
    truncate the table to the first entry. Since `2.5.1-F4` moved the abandoned
    entries to `docs/ROADMAP-ABANDONED.md`, `## Planned` runs to EOF, so the
    loop's fallthrough — not a heading — is what usually ends it; a later `##`
    still terminates it if one is ever added back.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_planned = False
    for line in lines:
        if re.match(r"^##\s+Planned\b", line):
            in_planned = True
            continue
        if in_planned and re.match(r"^##\s+", line):
            break
        if in_planned:
            out.append(line)
    return "\n".join(out)


def _split_entries(section: str) -> list[str]:
    """Split a section into per-entry blobs (bullet line + indented continuation)."""
    blobs: list[str] = []
    cur: list[str] = []
    for line in section.splitlines():
        if line.startswith("- **"):
            if cur:
                blobs.append("\n".join(cur))
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blobs.append("\n".join(cur))
    return blobs


def parse_entries(text: str) -> list[Entry]:
    """Parse every Planned entry, validating its Priority/Effort tag.

    Raises RoadmapError on a malformed or untagged entry, or one whose tag uses
    a level/size outside the allowed vocabulary.
    """
    entries: list[Entry] = []
    for blob in _split_entries(_planned_section(text)):
        m = _ENTRY_RE.match(blob)
        if not m:
            continue  # not an ID-bulleted entry (defensive; shouldn't happen)
        entry_id, raw_title = m.group(1), m.group(2)
        # Title: drop the ID code-span already consumed; collapse whitespace and
        # inline code backticks so the table cell renders on one clean line.
        title = re.sub(r"\s+", " ", raw_title).strip().rstrip(".")
        title = title.replace("`", "")
        tag = _TAG_RE.search(blob)
        if not tag:
            raise RoadmapError(
                f"{entry_id}: Planned entry has no "
                f"`*Priority: … · Effort: … · Bump: …*` tag"
            )
        priority, effort, bump = tag.group(1), tag.group(2), tag.group(3)
        if priority not in _PRIORITY_ORDER:
            raise RoadmapError(
                f"{entry_id}: priority {priority!r} not in {_PRIORITY_ORDER}"
            )
        if effort not in _EFFORT_ORDER:
            raise RoadmapError(
                f"{entry_id}: effort {effort!r} not in {_EFFORT_ORDER}"
            )
        if bump not in BUMP_ORDER:
            raise RoadmapError(
                f"{entry_id}: bump {bump!r} not in {BUMP_ORDER}"
            )
        entries.append(Entry(entry_id, title, priority, effort, bump))
    return entries


def _id_key(e: Entry) -> tuple:
    m = _ID_RE.match(e.id)
    ver = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)
    typ = m.group(4) if m else ""
    num = int(m.group(5)) if m else 0
    return (typ, ver, num)


def _sort_key(e: Entry) -> tuple:
    # Triage order: highest priority first, then cheapest effort, then ID.
    return (_PRIORITY_ORDER.index(e.priority), _EFFORT_ORDER.index(e.effort),
            *_id_key(e))


# Per-column sort keys for `--sort`. The three tag columns are *ordinal*, so
# they rank by vocabulary index (high < med < low), never alphabetically; each
# falls back to the canonical triage key so ties stay stable and meaningful.
SORT_COLUMNS: dict[str, Callable[[Entry], tuple]] = {
    "triage": _sort_key,
    "id": _id_key,
    "item": lambda e: (e.title.lower(), *_id_key(e)),
    "priority": lambda e: (_PRIORITY_ORDER.index(e.priority), _sort_key(e)),
    "effort": lambda e: (_EFFORT_ORDER.index(e.effort), _sort_key(e)),
    "bump": lambda e: (BUMP_ORDER.index(e.bump), _sort_key(e)),
}


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - len(_ELLIPSIS))] + _ELLIPSIS


_HEADER = ("ID", "Item", "Priority", "Effort", "Bump")
_RULE = ("----", "------", "----------", "--------", "------")


# Narrowest the Item column may be squeezed to before we stop shrinking it and
# let the row overflow instead — below this a title is all ellipsis and the
# view is useless anyway.
_MIN_ITEM = 24
_ELLIPSIS = "..."


def _terminal_width(width: int | None) -> int:
    if width is not None:
        return width
    # Honours COLUMNS, then the tty, then falls back to 80 when piped.
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def render_table(entries: list[Entry], sort: str = "triage",
                 reverse: bool = False, pad: bool = False,
                 width: int | None = None) -> str:
    """Render the Planned summary table.

    `pad` widens every cell to its column's widest value and truncates the
    Item column so each row fits on one terminal line. It is for the
    read-only `--print` view only: the committed table stays unpadded and
    untruncated so `--check` compares against a stable form and diffs don't
    churn whenever the longest title changes.
    """
    ordered = sorted(entries, key=SORT_COLUMNS[sort], reverse=reverse)
    cells = [
        (f"`{e.id}`", e.title, e.priority, e.effort, e.bump)
        for e in ordered
    ]
    if pad:
        # `_RULE` is excluded: in padded mode the separator is regenerated from
        # these widths, so letting its placeholder dashes vote would inflate the
        # bounded columns (a 3-char `med` stretched to `----------`'s 10).
        widths = [
            max(len(row[i]) for row in (_HEADER, *cells))
            for i in range(len(_HEADER))
        ]
        # Item is the one unbounded column; the other four come from fixed
        # vocabularies, so give them their natural width and hand what's left
        # of the line to Item. Overhead is "| " + " | "*4 + " |" = 16 columns.
        overhead = 3 * len(widths) + 1
        budget = _terminal_width(width) - overhead - sum(widths) + widths[1]
        item_w = max(_MIN_ITEM, min(widths[1], budget))
        if item_w < widths[1]:
            widths[1] = item_w
            cells = [
                (c[0], _truncate(c[1], item_w), *c[2:]) for c in cells
            ]
        rule = tuple("-" * w for w in widths)

        def line(row: tuple[str, ...], fill: str = " ") -> str:
            # strict: a row that does not match the column widths is a bug in
            # the caller, not something to silently truncate.
            padded = (c.ljust(w, fill) for c, w in zip(row, widths, strict=True))
            return f"|{fill}" + f"{fill}|{fill}".join(padded) + f"{fill}|"

        body = "\n".join([line(_HEADER), line(rule, "-"), *(line(c) for c in cells)])
    else:
        def plain(row: tuple[str, ...]) -> str:
            return "| " + " | ".join(row) + " |"

        body = "\n".join([
            plain(_HEADER),
            "|" + "|".join(_RULE) + "|",
            *(plain(c) for c in cells),
        ])
    return f"{body}\n"


def _splice_table(text: str, table: str) -> str:
    """Replace the content between the BEGIN/END markers with `table`."""
    begin = text.index(_BEGIN)
    begin_eol = text.index("\n", begin) + 1
    end = text.index(_END, begin_eol)
    return text[:begin_eol] + table + text[end:]


def build(text: str) -> str:
    return _splice_table(text, render_table(parse_entries(text)))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate the Planned summary table in ROADMAP.md."
    )
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed table is stale")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="render the table to stdout without writing ROADMAP.md")
    ap.add_argument("--sort", choices=sorted(SORT_COLUMNS), default="triage",
                    help="column to sort the printed table by (implies --print; "
                         "default: triage = priority, then effort, then ID)")
    ap.add_argument("--reverse", action="store_true",
                    help="reverse the --sort order")
    ap.add_argument("--width", type=int, default=None,
                    help="line width to fit the printed table to "
                         "(default: terminal width, or 80 when piped)")
    ap.add_argument("--repo", type=Path, default=REPO,
                    help="repo root to operate on (default: this script's repo)")
    args = ap.parse_args()

    roadmap = args.repo.resolve() / "ROADMAP.md"
    text = roadmap.read_text(encoding="utf-8")

    # A sorted view never reaches the file: --sort/--print are read-only, so the
    # committed table keeps its canonical triage order and --check stays stable.
    if args.print_only or args.sort != "triage" or args.reverse or args.width:
        try:
            entries = parse_entries(text)
        except RoadmapError as e:
            print(f"ROADMAP.md: {e}", file=sys.stderr)
            return 1
        print(render_table(entries, sort=args.sort, reverse=args.reverse,
                           pad=True, width=args.width), end="")
        return 0

    try:
        regenerated = build(text)
    except RoadmapError as e:
        print(f"ROADMAP.md: {e}", file=sys.stderr)
        return 1

    if args.check:
        if regenerated != text:
            print("ROADMAP.md summary table is stale — run `make roadmap-table`.",
                  file=sys.stderr)
            return 1
        return 0

    roadmap.write_text(regenerated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
