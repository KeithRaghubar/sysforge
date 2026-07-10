# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Unit guards for tools/check_standards.py::check_roadmap_ids and its extractors.

Guards the roadmap-ID collision detector: ROADMAP.md (open items) and
docs/release-notes/ (shipped items) are disjoint homes for the same ID space,
and nothing else cross-checks them, so an open ID can silently reuse a shipped
number (the 2.1.0-B2/B3 vs shipped B2-B7 incident this check exists to prevent).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_standards.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_standards", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass Finding can resolve its
    # own __module__ in sys.modules (Python 3.14 dataclass annotation lookup).
    sys.modules["check_standards"] = mod
    spec.loader.exec_module(mod)
    return mod


check_standards = _load()


def test_parse_id_splits_version_type_number():
    assert check_standards._parse_id("2.1.0-F12") == ("2.1.0", "F", 12)
    assert check_standards._parse_id("not-an-id") is None


def test_roadmap_bold_bullet_is_extracted_as_planned():
    text = "## Planned\n\n- **`2.2.0-F7`** — a real entry.\n"
    assert check_standards.roadmap_ids_from_text(text) == {"2.2.0-F7": "planned"}


def test_roadmap_id_scheme_prose_is_not_extracted():
    """Inline examples in the `## ID scheme` section are prose, not entries."""
    text = "## ID scheme\n\nIDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature).\n"
    assert check_standards.roadmap_ids_from_text(text) == {}


def test_roadmap_bullet_after_abandoned_header_is_abandoned():
    text = (
        "## Planned\n\n- **`2.2.0-F1`** — live.\n\n"
        "## Abandoned / decided against\n\n- **`2.2.0-Q3`** — dropped.\n"
    )
    assert check_standards.roadmap_ids_from_text(text) == {
        "2.2.0-F1": "planned",
        "2.2.0-Q3": "abandoned",
    }


def test_roadmap_non_id_abandoned_entry_is_ignored():
    text = "## Abandoned\n\n- **`-sysforge` suffix** — scrapped.\n"
    assert check_standards.roadmap_ids_from_text(text) == {}


def test_shipped_ignores_html_comment_examples():
    """The accumulator's `<!-- e.g. (1.2.0-F35) -->` header must not count."""
    text = (
        "# sysforge (unreleased)\n\n"
        "<!-- Reference the roadmap ID inline, e.g. (1.2.0-F35). -->\n\n"
        "## Added\n\n- a real thing (2.2.0-F4).\n"
    )
    assert check_standards.shipped_ids_from_text(text) == {"2.2.0-F4"}


def _mkrepo(tmp_path: Path, roadmap: str, notes: dict[str, str], version="2.2.0"):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "sysforge"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "ROADMAP.md").write_text(roadmap, encoding="utf-8")
    nd = tmp_path / "docs" / "release-notes"
    nd.mkdir(parents=True)
    for name, body in notes.items():
        (nd / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_planned_id_also_shipped_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.1.0-B2`** — open bug.\n",
        {"v2.1.0.md": "# v2.1.0\n\n## Fixed\n\n- old bug (2.1.0-B2).\n"},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.1.0-B2" in f.message
               and "shipped" in f.message for f in findings), findings


def test_planned_id_also_abandoned_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — live.\n\n"
        "## Abandoned\n\n- **`2.2.0-F1`** — dropped.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.2.0-F1" in f.message
               and "Abandoned" in f.message for f in findings), findings


def test_shipped_q_id_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n_(none)_\n",
        {"unreleased.md": "# sysforge (unreleased)\n\n## Changed\n\n- shipped a question (2.2.0-Q4).\n"},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.2.0-Q4" in f.message for f in findings), findings


def test_shipped_q_in_released_notes_is_grandfathered(tmp_path):
    # A Q cited in an immutable released v*.md must NOT fire check 4.
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n_(none)_\n",
        {"v2.2.0.md": "# v2.2.0\n\n## Changed\n\n- shipped a question (2.2.0-Q4).\n"},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert not [f for f in findings if "2.2.0-Q4" in f.message], findings


def test_promoted_from_lineage_is_not_shipped():
    ids = check_standards.shipped_ids_from_text(
        "- a fix (2.1.0-B18, promoted from 2.1.0-Q1)."
    )
    assert ids == {"2.1.0-B18"}


def test_active_prefix_gap_is_warn(tmp_path):
    # Active version 2.2.0; F1 and F3 present, F2 missing -> one warn.
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — a.\n- **`2.2.0-F3`** — c.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert [f for f in findings if f.severity == "warn" and "2.2.0-F2" in f.message]
    assert not [f for f in findings if f.severity == "error"]


def test_historical_prefix_gap_is_not_reported(tmp_path):
    # Active version 2.2.0; a 1.2.0 gap must stay silent (grandfathered).
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`1.2.0-F1`** — a.\n- **`1.2.0-F3`** — c.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert not [f for f in findings if "1.2.0-F2" in f.message], findings


def test_real_repo_has_no_roadmap_id_errors():
    """The live tree must be collision-free (warns permitted)."""
    findings = check_standards.check_roadmap_ids(check_standards.REPO)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], errors


def test_next_id_returns_max_plus_one_including_abandoned(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — a.\n\n"
        "## Abandoned\n\n- **`2.2.0-F5`** — dropped (still consumes the number).\n",
        {"v2.2.0.md": "# v2.2.0\n\n## Added\n\n- shipped (2.2.0-F3).\n"},
    )
    assert check_standards.next_id(repo, "2.2.0-F") == "2.2.0-F6"


def test_next_id_for_unused_type_starts_at_one(tmp_path):
    repo = _mkrepo(tmp_path, "## Planned\n\n_(none)_\n", {})
    assert check_standards.next_id(repo, "2.2.0-STD") == "2.2.0-STD1"


def test_next_id_flag_prints_and_exits_zero(tmp_path, capsys):
    _mkrepo(tmp_path, "## Planned\n\n- **`2.2.0-F2`** — a.\n", {})
    rc = check_standards.main(["--next-id", "2.2.0-F", "--repo", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "2.2.0-F3"


def test_section_after_abandoned_resets_to_planned():
    text = (
        "## Abandoned\n\n- **`2.2.0-Q3`** — dropped.\n\n"
        "## Something Else\n\n- **`2.2.0-F9`** — still live.\n"
    )
    assert check_standards.roadmap_ids_from_text(text) == {
        "2.2.0-Q3": "abandoned",
        "2.2.0-F9": "planned",
    }


def test_next_id_rejects_malformed_prefix(tmp_path):
    repo = _mkrepo(tmp_path, "## Planned\n\n_(none)_\n", {})
    with pytest.raises(ValueError):
        check_standards.next_id(repo, "2.2.0")
