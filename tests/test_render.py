# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_render.py — the shared preflight/summary presentation helpers (2.6.1-F9).

Covers:
  - arrow(): pretty glyph, ASCII under the TERM=linux downgrade
  - version_pair(): old → new, equal marker, missing sides
  - tag_header(): the 17-col `  [TAG]` gutter shared by every block
"""

import pytest

from sysforge.primitives.render import arrow, tag_header, version_pair


@pytest.fixture(autouse=True)
def _plain(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")


# ---------------------------------------------------------------------------
# arrow
# ---------------------------------------------------------------------------

def test_arrow_is_unicode_on_capable_terminal():
    assert arrow() == "→"


def test_arrow_degrades_under_term_linux(monkeypatch):
    monkeypatch.setenv("TERM", "linux")
    assert arrow() == "->"


# ---------------------------------------------------------------------------
# version_pair
# ---------------------------------------------------------------------------

def test_version_pair_renders_change():
    assert version_pair("24.0", "24.1") == "24.0 → 24.1"


def test_version_pair_marks_equal_versions():
    assert version_pair("24.1", "24.1") == "24.1 (=)"


def test_version_pair_can_suppress_equal_marker():
    assert version_pair("24.1", "24.1", equal_marker=False) == "24.1 → 24.1"


def test_version_pair_missing_sides_use_placeholder():
    assert version_pair(None, "24.1") == "— → 24.1"
    assert version_pair("24.0", None) == "24.0 → —"
    assert version_pair(None, None) == "— → —"


def test_version_pair_degrades_all_glyphs_under_term_linux(monkeypatch):
    monkeypatch.setenv("TERM", "linux")
    out = version_pair(None, "24.1")
    assert out == "-- -> 24.1"
    assert "→" not in out
    assert "—" not in out


# ---------------------------------------------------------------------------
# tag_header
# ---------------------------------------------------------------------------

def test_tag_header_pads_to_shared_gutter():
    # `  [PREFLIGHT]` + at least one space, total gutter width 17.
    # Matches the literal every renderer previously inlined:
    #   f"  [{TAG}]" + " " * max(1, 17 - len(TAG) - 2)
    assert tag_header("PREFLIGHT") == "  [PREFLIGHT]" + " " * 6


def test_tag_header_always_leaves_one_space_for_long_tags():
    out = tag_header("A_VERY_LONG_TAG_NAME")
    assert out.endswith("] ")


# ---------------------------------------------------------------------------
# fmt_bytes
# ---------------------------------------------------------------------------

def test_fmt_bytes_scales_through_units():
    from sysforge.primitives.render import fmt_bytes
    assert fmt_bytes(0) == "0.0 B"
    assert fmt_bytes(512) == "512.0 B"
    assert fmt_bytes(1536) == "1.5 KiB"
    assert fmt_bytes(1024 * 1024 * 3) == "3.0 MiB"
    assert fmt_bytes(1024 ** 4) == "1.0 TiB"
    assert fmt_bytes(1024 ** 5) == "1.0 PiB"


def test_cache_probe_formats_identically_after_promotion():
    """Regression: cache_probe's report must not change shape from the move."""
    from sysforge.primitives import cache_probe
    from sysforge.primitives.render import fmt_bytes
    for n in (0, 999, 1024, 1024 ** 3 + 12345):
        assert cache_probe._fmt_bytes(n) == fmt_bytes(n)


# ---------------------------------------------------------------------------
# classify_version_change / version_pair(classify=True) (3.4.0-F8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old,new,level", [
    ("1:1.0-1", "2:0.9-1", "epoch"),
    ("1.0-1", "1:1.0-1", "epoch"),
    ("1.2.3-1", "2.0.0-1", "major"),
    ("1.2.3-1", "1.3.0-1", "minor"),
    ("1.2.3-1", "1.2.4-1", "patch"),
    ("1.2.3.4-1", "1.2.3.5-1", "patch"),
    ("1.2-1", "1.2.1-1", "patch"),
    ("1-1", "1.1-1", "minor"),
    ("1.2.3-1", "1.2.3-2", "pkgrel"),
    ("1.2.3-1", "1.2.3-1.1", "pkgrel"),
    ("r1234.abc1234-1", "r1240.def5678-1", "vcs"),
    ("1.2.3.r45.gabcdef0-1", "1.2.3.r50.g1234567-1", "vcs"),
    ("1.2.3.r45.gabcdef0-1", "1.2.3.r50.g1234567-2", "vcs"),
    ("1.2.3-1", "1.2.3.r2.g1234567-1", "vcs"),
    ("1.2.3.r45.gabcdef0-1", "1.3.0.r2.g1234567-1", "minor"),
    ("1.2_3-1", "1.2_4-1", "patch"),
    ("1.2+git5-1", "1.2+git7-1", "patch"),
    ("2024.01.15-1", "2024.02.01-1", "minor"),
    ("20240101-1", "20240215-1", "unknown"),
    ("1.2.3", "1.2.4", "patch"),
    ("1.2.3-2", "1.2.3-1", "downgrade"),
    ("1.3.0-1", "1.2.9-1", "downgrade"),
    ("2:1.0-1", "1:9.0-1", "downgrade"),
    ("r1240.def5678-1", "r1234.abc1234-1", "downgrade"),
    ("1.0-1", "1.0-1", "unknown"),
    ("001-1", "1-1", "unknown"),
    (None, "1.0-1", "unknown"),
    ("1.0-1", None, "unknown"),
    ("", "", "unknown"),
    ("???", "!!!", "unknown"),
    ("1.0-1", "1.0rc-1", "downgrade"),
])
def test_classify_version_change(old, new, level):
    from sysforge.primitives.render import classify_version_change
    assert classify_version_change(old, new) == level


def test_classify_never_raises_on_odd_input():
    from sysforge.primitives.render import classify_version_change
    for old, new in [(":", ":"), ("1:", "2:"), ("-", "-1"), ("1.0-", "1.0-1"),
                     ("..", "..."), ("x:1.0-1", "y:1.0-1"), ("1.0-1-1", "1.0-1-2")]:
        assert classify_version_change(old, new) in {
            "epoch", "major", "minor", "patch", "pkgrel", "vcs", "downgrade", "unknown"}


@pytest.mark.parametrize("old,new,equal_marker", [
    ("1.2.3-1", "2.0.0-1", True), ("1.0-1", "1.0-2", False),
    ("1.0-1", "1.0-1", True), ("1.0-1", "1.0-1", False), (None, "1.0-1", True),
])
def test_classify_with_colour_off_is_byte_identical(old, new, equal_marker):
    assert (version_pair(old, new, equal_marker=equal_marker, classify=True)
            == version_pair(old, new, equal_marker=equal_marker))


@pytest.mark.parametrize("old,new,sgr", [
    ("1:1.0-1", "2:1.0-1", "\x1b[31m"),    # epoch: red
    ("1.3.0-1", "1.2.9-1", "\x1b[31m"),    # downgrade: red
    ("1.2.3-1", "2.0.0-1", "\x1b[33m"),    # major: yellow
    ("1.2.3-1", "1.3.0-1", "\x1b[36m"),    # minor: cyan
    ("1.2.3-1", "1.2.3-2", "\x1b[2m"),     # pkgrel: dim
    ("r1-1", "r2-1", "\x1b[2m"),           # vcs: dim
])
def test_classify_tints_the_pair_under_colour(monkeypatch, old, new, sgr):
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    out = version_pair(old, new, equal_marker=False, classify=True)
    assert out.startswith(sgr) and out.endswith("\x1b[0m")
    assert f"{old} → {new}" in out


@pytest.mark.parametrize("old,new", [("1.2.3-1", "1.2.4-1"), ("20240101-1", "20240215-1")])
def test_patch_and_unknown_stay_plain_under_colour(monkeypatch, old, new):
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert version_pair(old, new, classify=True) == f"{old} → {new}"


def test_equal_pair_is_never_tinted(monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert version_pair("1.0-1", "1.0-1", classify=True) == "1.0-1 (=)"


def test_update_summary_row_is_tinted_under_colour(monkeypatch):
    """One call site end to end: a rebuilt package's row in the update summary."""
    from types import SimpleNamespace
    from sysforge import update_summary
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    summary = SimpleNamespace(labels={}, versions={"mesa": ("24.1.0-1", "25.0.0-1")})
    row = update_summary._fmt_pkg(summary, "mesa")
    assert row == "mesa: \x1b[33m24.1.0-1 → 25.0.0-1\x1b[0m"
