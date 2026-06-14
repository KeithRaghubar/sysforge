# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
headers.py — shared visual primitives for prominent stage/section headers.

Pure string-building (no I/O). Callers route the returned lines through
``log.ui()`` so each line lands in the unified log with its own prefix.

Visual vocabulary:

    ════════════════════════════════════════════════════════
      [3/8] configure
      Bootstrap configuration — hostname, locale, services
    ════════════════════════════════════════════════════════

The rule glyph (U+2550) is repeated to the terminal width; the result is
bold-cyan when stderr is a colour-capable TTY (gated via ``log.use_color``).
"""

from __future__ import annotations

import shutil
from importlib.metadata import PackageNotFoundError, version

from sysforge.log import bold as _bold
from sysforge.log import cyan as _cyan
from sysforge.log import dim as _dim
from sysforge.log import green as _green
from sysforge.log import use_color

_RULE_GLYPH = "═"
_MIN_WIDTH = 40
_MAX_WIDTH = 100


def _width() -> int:
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(_MIN_WIDTH, min(_MAX_WIDTH, cols))


def _rule() -> str:
    bar = _RULE_GLYPH * _width()
    return _bold(_cyan(bar))


def _sysforge_version() -> str:
    try:
        return version("sysforge")
    except PackageNotFoundError:
        return "unknown"


def stage_lines(idx: int, total: int, name: str, description: str) -> list[str]:
    """Return the lines that comprise a stage transition banner."""
    return [
        _rule(),
        f"  {_bold(f'[{idx}/{total}] {name}')}",
        f"  {description}",
        _rule(),
    ]


def stage_complete_line(name: str) -> str:
    """Return a single line marking a stage as complete."""
    glyph = _green("✓") if use_color() else "✓"
    return f"  {glyph} {name} complete"


def welcome_lines(stage_names: list[str]) -> list[str]:
    """Banner shown once at the start of ``sysforge run pipeline``."""
    chain = " → ".join(stage_names)
    return [
        _rule(),
        f"  {_bold('SysForge bootstrap pipeline')}",
        f"  v{_sysforge_version()} — {len(stage_names)} stages",
        f"  {chain}",
        _rule(),
    ]


def closing_lines(message: str = "Pipeline complete — all stages finished successfully.") -> list[str]:
    """Closing banner for a successful pipeline run."""
    return [
        _rule(),
        f"  {_bold(message)}",
        _rule(),
    ]


_STATUS_GLYPHS = {
    "done": ("✓", _green),
    "running": ("▸", _bold),
    "failed": ("✗", lambda t: t),  # log.error already colours these elsewhere
    "skipped_to": ("↳", _dim),
    "pending": ("·", _dim),
}


def stage_list_lines(
    names: list[str],
    statuses: dict[str, str],
    next_idx: int,
) -> list[str]:
    """Show the ordered stage list with per-stage status glyphs.

    ``next_idx`` is the 0-based index of the first stage that will actually run
    (i.e. ``start_idx`` in the runner). Stages at that index get the running
    glyph; stages before it are shown using their state-tracked status.
    """
    out = [f"  {_bold('Stages:')}"]
    width = max(len(n) for n in names) if names else 0
    for i, name in enumerate(names):
        if i == next_idx:
            key = "running"
        else:
            key = statuses.get(name, "pending")
        glyph, fmt = _STATUS_GLYPHS.get(key, _STATUS_GLYPHS["pending"])
        label = name.ljust(width)
        out.append(f"    {fmt(glyph)} {label}  {_dim(key)}")
    return out
