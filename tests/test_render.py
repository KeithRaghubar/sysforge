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
