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

**Glyphs are pre-resolved here**, not left to the emit path. Every block now
reaches the terminal through ``log.ui``, which applies
:func:`sysforge.log.downgrade_glyphs` itself (2.6.1-F10 closed the bare-``print``
sites that originally motivated this) — so the resolution here is no longer the
only thing standing between a non-Unicode terminal and a mojibake arrow. It is
kept deliberately: ``downgrade_glyphs`` is idempotent, so the double pass costs
nothing, and it keeps a renderer's return value correct on its own terms for
any caller that formats without emitting (tests, future non-``log.ui`` paths).

Leaf module: imports only :mod:`sysforge.log` and the pure
:func:`sysforge.primitives.version.vercmp_local`, so every layer may use it.

Public API:
    arrow()                                   -> str
    em_dash()                                 -> str
    fmt_bytes(n)                              -> str
    version_pair(old, new, *, equal_marker=, classify=)  -> str
    classify_version_change(old, new)         -> str
    tag_header(tag)                           -> str
"""
from __future__ import annotations

import re

from sysforge import log
from sysforge.primitives.version import vercmp_local

# Width of the `  [TAG]` gutter every report block aligns its body against.
_GUTTER = 17

# Stand-in for an unknown version on either side of a pair.
_MISSING = "—"


def arrow() -> str:
    """``→``, or ``->`` where the Unicode gate degrades glyphs."""
    return log.downgrade_glyphs("→")


def em_dash() -> str:
    """``—``, or ``-`` where the Unicode gate degrades glyphs.

    Same rationale as :func:`arrow`: any renderer that wants a standalone
    em-dash (not the ``old — new`` shape ``version_pair`` already handles)
    must go through here rather than hardcoding the glyph.
    """
    return log.downgrade_glyphs("—")


def middot() -> str:
    """``·``, or ``-`` where the Unicode gate degrades glyphs.

    The separator for compact count footers (``12 checked · 3 available``).
    Same rationale as :func:`arrow`: never hardcode the glyph at a call site.
    """
    return log.downgrade_glyphs("·")


def ellipsis_glyph() -> str:
    """``…``, or ``...`` where the Unicode gate degrades glyphs.

    Named with the ``_glyph`` suffix because bare ``ellipsis`` is the name of a
    Python builtin type. Used by truncated report blocks (``… and N more``).
    """
    return log.downgrade_glyphs("…")


def fmt_bytes(n: int | float) -> str:
    """Format a byte count as a human-readable binary-prefix string.

    Promoted from ``cache_probe._fmt_bytes`` so the cache report and the
    post-build change summary size column format identically.
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def version_pair(
    old: str | None,
    new: str | None,
    *,
    equal_marker: bool = True,
    classify: bool = False,
) -> str:
    """Render a version transition as ``old → new``.

    When both sides are known and identical, collapses to ``ver (=)`` — the
    "checked, nothing changed" shape the LLVM pre-flight uses for split members.
    Pass ``equal_marker=False`` to keep the arrow form unconditionally (the
    stage-owned-updates rows read as a proposal, not an observation, so an
    equal pair there is still worth showing as a transition).

    An unknown side renders as ``—``. The whole string goes through the glyph
    downgrade, so both the arrow and the em-dash are ASCII-safe.

    *classify* (3.4.0-F8) tints the pair by :func:`classify_version_change`, so
    an epoch change or a downgrade stands out among routine VCS ticks. Colour
    goes through ``log.use_color()``, so with colour off the output is
    byte-identical to ``classify=False``. **Only safe on a print()/ui()
    surface**, like :func:`tag_header`'s colour.
    """
    left = old or _MISSING
    right = new or _MISSING
    if equal_marker and old and new and old == new:
        return log.downgrade_glyphs(f"{left} (=)")
    text = log.downgrade_glyphs(f"{left} → {right}")
    if not classify:
        return text
    painter = _TAG_COLORS.get(_LEVEL_COLORS.get(classify_version_change(old, new), ""))
    return text if painter is None else painter(text)


# ---------------------------------------------------------------------------
# Version-change classification (3.4.0-F8)
# ---------------------------------------------------------------------------

# Levels, by the first component of [epoch:]pkgver-pkgrel that differs.
EPOCH, MAJOR, MINOR, PATCH = "epoch", "major", "minor", "patch"
PKGREL, VCS, DOWNGRADE, UNKNOWN = "pkgrel", "vcs", "downgrade", "unknown"

# Tint per level, by name in _TAG_COLORS. A level with no entry stays plain:
# patch is routine, and unknown means the shape wasn't understood.
_LEVEL_COLORS = {
    EPOCH: "red",
    DOWNGRADE: "red",
    MAJOR: "yellow",
    MINOR: "cyan",
    PKGREL: "dim",
    VCS: "dim",
}

# A VCS pkgver's moving tail: `r<count>` with an optional commit hash
# (`r1234.abc1234`, `1.2.3.r45.gabcdef0`), or a bare `.g<hash>`.
_VCS_TAIL = re.compile(
    r"(?:^|[._+])(?:r\d+(?:[._+]g?[0-9a-f]{7,40})?|g[0-9a-f]{7,40})$")
# A YYYYMMDD pkgver: a date bump is not a major release.
_DATE = re.compile(r"^(?:19|20)\d{6}$")
_COMPONENT_SEP = re.compile(r"[._+]")


def _split_evr(ver: str) -> tuple[str, str, str]:
    epoch, sep, rest = ver.partition(":")
    if not sep:
        epoch, rest = "0", ver
    pkgver, _, pkgrel = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return epoch or "0", pkgver, pkgrel


def classify_version_change(old: str | None, new: str | None) -> str:
    """Name how big the ``old`` → ``new`` jump is (3.4.0-F8).

    ``downgrade`` when ``new`` sorts before ``old`` (libalpm order, via
    :func:`~sysforge.primitives.version.vercmp_local`); otherwise the first
    component that differs: ``epoch``; ``major``/``minor``/``patch`` for the
    first, second or any deeper pkgver component; ``pkgrel`` when pkgver is
    equal; ``vcs`` when only a trailing ``rN``/``.gHASH`` moved. pkgver is not
    SemVer, so anything not understood (an equal pair, a missing side, a
    ``YYYYMMDD`` date, a shape that sorts equal) is ``unknown``. Never raises.
    """
    if not old or not new or old == new:
        return UNKNOWN
    try:
        order = vercmp_local(old, new)
    except Exception:  # noqa: BLE001 — presentation only; never fail a report
        return UNKNOWN
    if order > 0:
        return DOWNGRADE
    if order == 0:
        return UNKNOWN
    old_epoch, old_ver, old_rel = _split_evr(old)
    new_epoch, new_ver, new_rel = _split_evr(new)
    if old_epoch != new_epoch:
        return EPOCH
    old_base = _VCS_TAIL.sub("", old_ver)
    new_base = _VCS_TAIL.sub("", new_ver)
    had_tail = old_base != old_ver or new_base != new_ver
    if had_tail and old_ver != new_ver and old_base == new_base:
        return VCS
    if old_ver == new_ver:
        return PKGREL if old_rel != new_rel else UNKNOWN
    old_parts = _COMPONENT_SEP.split(old_base)
    new_parts = _COMPONENT_SEP.split(new_base)
    pairs = zip(old_parts, new_parts, strict=False)
    i = next((k for k, (a, b) in enumerate(pairs) if a != b),
             min(len(old_parts), len(new_parts)))  # one is a prefix: 1.2 -> 1.2.1
    if i == 0 and (_DATE.match(old_parts[0]) or _DATE.match(new_parts[0])):
        return UNKNOWN
    return (MAJOR, MINOR)[i] if i < 2 else PATCH


# Colour names accepted by :func:`tag_header`, mapped to the log-layer helpers
# that gate on log.use_color(). Named rather than passed as callables so a caller
# declares intent ("this verdict is bad") without reaching for an escape code.
_TAG_COLORS = {
    "green": log.green,
    "yellow": log.yellow,
    "red": log.red,
    "cyan": log.cyan,
    "dim": log.dim,
}


def tag_header(tag: str, *, color: str | None = None) -> str:
    """Return the ``  [TAG]`` prefix padded to the shared gutter width.

    Always leaves at least one trailing space so an over-long tag still
    separates from its message rather than butting against it.

    *color* (a :data:`_TAG_COLORS` name: ``"green"`` / ``"yellow"`` / ``"red"`` /
    ``"cyan"`` / ``"dim"``) tints the bracketed tag —
    the gutter stays the one home for the ``[TAG]`` shape, so no caller writes an
    escape sequence itself and ``log.use_color()`` gates it for free. Only the
    bracketed token is wrapped; the padding is emitted outside the SGR run so
    column alignment is computed on visible width, not byte length. An unknown
    name degrades to plain rather than raising.

    **Only safe on a print()/ui() surface.** Colour embedded in a message body
    passed to ``log.info``/``warn``/``error`` bypasses the colour gate and lands
    verbatim in the file log — see docs/design/12-logging.md § Colour.
    """
    pad = " " * max(1, _GUTTER - len(tag) - 2)
    painter = _TAG_COLORS.get(color) if color else None
    body = f"[{tag}]" if painter is None else painter(f"[{tag}]")
    return f"  {body}{pad}"
