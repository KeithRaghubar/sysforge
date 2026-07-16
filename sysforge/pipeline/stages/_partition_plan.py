# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/_partition_plan.py — shared partition-plan safety helpers.

Table-rendering helpers and the destructive-operation confirmation prompt,
plus the existing-partition-table probe. Single home for this logic so it
isn't duplicated across partition-touching stages/tools.
"""

import subprocess
import unicodedata

from sysforge import log
from sysforge.pipeline.stages._bootstrap import BootstrapConfig
from sysforge.primitives.prompt import prompt_choice


def probe_disk_size_bytes(device: str) -> int | None:
    """Return the total size of `device` in bytes, or None if it can't be read.

    archinstall's headless (``--config``) disk schema has no "fill remaining"
    sentinel — every partition size is a concrete value that it converts
    straight to a sector length (a size of 0 becomes a zero-length partition
    and parted rejects it). So to size the root partition to "the rest of the
    disk" we must know the real disk size and compute it ourselves; this is the
    single probe for that. ``lsblk --nodeps --bytes`` reports the whole-device
    size without descending into existing partitions. Any failure returns None
    so the caller can decide (hard-fail for a real run, nominal size for a
    dry-run preview) rather than crashing here.
    """
    result = subprocess.run(
        ["lsblk", "--noheadings", "--nodeps", "--bytes", "--output", "SIZE", device],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip().splitlines()
    if not text or not text[0].strip().isdigit():
        return None
    return int(text[0].strip())


def _has_existing_partitions(device: str) -> bool:
    """Return True if `device` already carries a partition table with partitions.

    ``lsblk`` lists the device followed by one line per child partition, so any
    output beyond the device's own line means existing partitions that a
    ``sgdisk --clear`` would destroy. On any lsblk error we return False — the
    inability to enumerate shouldn't itself block partitioning; the standard
    plan confirmation still applies.
    """
    result = subprocess.run(
        ["lsblk", "--noheadings", "--raw", "--output", "NAME", device],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return len(names) > 1


# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------

# Column interior widths (dashes between connectors): Part, Size, FS, Mount.
# Every border, divider, header, and row in the plan table is derived from this
# single spec so the box can never drift out of alignment (the recurring bug).
_COLS = (7, 12, 10, 26)
_W = sum(_COLS) + len(_COLS) - 1  # full-width interior = 58


def _dwidth(text: str) -> int:
    """Terminal display width: East-Asian Wide/Fullwidth glyphs occupy two cells.

    ``len()`` counts code points, not cells, so a single wide glyph (or an em
    dash that a terminal renders double-width) would shift the right border —
    the recurring misalignment. Measuring display width keeps the box square.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    """Left-justify into ``width`` *display cells*; truncate over-long content.

    Truncation and padding both reckon in display width, so non-ASCII content
    can never push the box out of alignment.
    """
    out = ""
    used = 0
    for c in text:
        w = _dwidth(c)
        if used + w > width:
            break
        out += c
        used += w
    return out + " " * (width - used)


def _cap(left: str, right: str) -> str:
    """Full-width rule with no column connectors (top/bottom caps & full dividers)."""
    return "  " + left + "─" * _W + right


def _coldiv(left: str, mid: str, right: str) -> str:
    """Column divider/border carrying ┬ / ┼ / ┴ connectors."""
    return "  " + left + mid.join("─" * w for w in _COLS) + right


def _fullrow(text: str) -> str:
    """Full-width content row (title, Device, Target)."""
    return "  │" + _pad(text, _W) + "│"


def _colrow(cells: tuple[str, ...]) -> str:
    """Column content row; each cell padded/truncated to its column width."""
    return "  │" + "│".join(_pad(c, w) for c, w in zip(cells, _COLS, strict=True)) + "│"


def _plan_table(cfg: BootstrapConfig) -> list[str]:
    """Build the partition-plan box table as a list of equal-width lines."""
    return [
        _cap("┌", "┐"),
        _fullrow("  SysForge - Partition plan"),
        _cap("├", "┤"),
        _fullrow(f"  Device : {cfg.device}"),
        _fullrow(f"  Target : {cfg.target}"),
        _coldiv("├", "┬", "┤"),
        _colrow(("  Part", " Size", " FS", " Mount")),
        _coldiv("├", "┼", "┤"),
        _colrow(("  1", f" {cfg.esp_size_mib} MiB", " fat32", f" {cfg.target}/boot")),
        _colrow(("  2", " remaining", f" {cfg.root_fs}", f" {cfg.target}")),
        _coldiv("└", "┴", "┘"),
    ]


def _confirm(cfg: BootstrapConfig) -> None:
    """Print the partition plan and require explicit confirmation."""
    # The plan table is built with box-drawing glyphs; route every emitted line
    # through the same downgrade chokepoint log.ui() uses so a Linux VT console
    # (which can't render ─│┼…) gets ASCII fallbacks instead of missing-glyph
    # boxes. _plan_table stays pure (glyph-rich) for testing.
    print()
    for line in _plan_table(cfg):
        print(log.downgrade_glyphs(line))
    print()
    print(log.downgrade_glyphs(f"  WARNING: All data on {cfg.device} will be destroyed."))
    print()

    # When the device already carries a partition table, make the overwrite
    # explicit and default to *no*: the prompt defaults to "n", and any input
    # but an explicit yes (including empty input / EOF in a non-interactive run)
    # aborts. This stops a stray run from silently wiping a populated disk.
    if _has_existing_partitions(cfg.device):
        print(log.downgrade_glyphs(
            f"  {cfg.device} already has an existing partition table — its "
            f"partitions will be erased."
        ))
        print()
        answer = prompt_choice(
            f"  Overwrite all partitions on {cfg.device}? [y/N]: ",
            choices=("y", "yes"),
            default="n",
            eof_default="n",
            retry_on_invalid=False,
            tag="PARTITION",
        )
        if answer not in ("y", "yes"):
            raise RuntimeError(
                "[PARTITION] Aborted by user — existing partitions left intact."
            )
        return

    # Bare/unpartitioned disk: any non-confirming input must abort, never re-prompt.
    answer = prompt_choice(
        "  Type 'yes' to proceed, anything else to abort: ",
        choices=("yes",),
        default="",
        eof_default="",
        retry_on_invalid=False,
        tag="PARTITION",
    )
    if answer != "yes":
        raise RuntimeError("[PARTITION] Aborted by user.")
