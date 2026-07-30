# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
render.py — shared presentation primitives for tag-gutter report blocks.

The single home for the formatting vocabulary that ``sysforge update``'s
summary and the two pre-flight blocks all speak (2.6.1-F9). Before this module
each renderer inlined its own copy:

  * ``update_summary._print_summary`` / ``_print_result_summary``
  * ``llvm_state.render_preflight``        (LLVM source pre-flight)
  * ``toolchain_preflight.render_preflight`` (toolchain availability)

They were documented as mirroring each other but had drifted — only the
``  [TAG]`` gutter was genuinely shared, and the arrow glyph was hardcoded in
``llvm_state`` while ``update_summary`` pre-resolved it.

**Glyphs are pre-resolved here**, not left to the emit path. ``log.ui`` applies
:func:`sysforge.log.downgrade_glyphs`, but every pre-flight block is written
with a bare ``print()`` (``update.py``, ``build_cmd.py``, ``fetch.py``,
``pipeline/stages/toolchain.py``), which bypasses that chokepoint. Resolving at
format time makes the output correct regardless of how the caller emits it.

Leaf module: imports only :mod:`sysforge.log`, so every layer may use it.

Public API:
    arrow()                                   -> str
    version_pair(old, new, *, equal_marker=)  -> str
    tag_header(tag)                           -> str
"""
from __future__ import annotations

from sysforge import log

# Width of the `  [TAG]` gutter every report block aligns its body against.
_GUTTER = 17

# Stand-in for an unknown version on either side of a pair.
_MISSING = "—"


def arrow() -> str:
    """``→``, or ``->`` where the Unicode gate degrades glyphs."""
    return log.downgrade_glyphs("→")


def version_pair(
    old: str | None,
    new: str | None,
    *,
    equal_marker: bool = True,
) -> str:
    """Render a version transition as ``old → new``.

    When both sides are known and identical, collapses to ``ver (=)`` — the
    "checked, nothing changed" shape the LLVM pre-flight uses for split members.
    Pass ``equal_marker=False`` to keep the arrow form unconditionally (the
    stage-owned-updates rows read as a proposal, not an observation, so an
    equal pair there is still worth showing as a transition).

    An unknown side renders as ``—``. The whole string goes through the glyph
    downgrade, so both the arrow and the em-dash are ASCII-safe.
    """
    left = old or _MISSING
    right = new or _MISSING
    if equal_marker and old and new and old == new:
        return log.downgrade_glyphs(f"{left} (=)")
    return log.downgrade_glyphs(f"{left} → {right}")


def tag_header(tag: str) -> str:
    """Return the ``  [TAG]`` prefix padded to the shared gutter width.

    Always leaves at least one trailing space so an over-long tag still
    separates from its message rather than butting against it.
    """
    return f"  [{tag}]" + " " * max(1, _GUTTER - len(tag) - 2)
