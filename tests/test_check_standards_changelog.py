# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Unit guards for tools/check_standards.py::check_changelog.

The changelog lint is what keeps the running accumulator
(docs/release-notes/unreleased.md) honest as per-task entries land, so the
release-notes skill only curates a well-formed file rather than repairing it.
The regression these guard against: a category heading authored at the wrong
level (e.g. `### Changed`) was invisible to the old `startswith("## ")` check
and drifted in unnoticed until release-time reconciliation.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_standards.py"


def _load():
    # tools/ on sys.path first: check_standards imports its sibling
    # `_semver_vocab`, which fails in a single-file run where no other test
    # module has done this insert (the 2.6.1-B14 defect, same shape).
    if str(_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("check_standards", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass Finding can resolve its
    # own __module__ in sys.modules (Python 3.14 dataclass annotation lookup).
    sys.modules["check_standards"] = mod
    spec.loader.exec_module(mod)
    return mod


check_standards = _load()


def _write_notes(repo: Path, body: str) -> None:
    notes = repo / "docs" / "release-notes"
    notes.mkdir(parents=True)
    (notes / "unreleased.md").write_text(body, encoding="utf-8")


def test_well_formed_accumulator_passes(tmp_path):
    _write_notes(tmp_path,
                 "# sysforge (unreleased)\n\n## Changed\n\n"
                 "- **`1.0.0-F1` — thing.** body\n")
    assert check_standards.check_changelog(tmp_path) == []


def test_mis_leveled_category_heading_is_error(tmp_path):
    """A `### Changed` section must fail — the exact drift the skill was masking."""
    _write_notes(tmp_path, "# sysforge (unreleased)\n\n### Changed\n\n- thing\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("must be a `## `" in f.message for f in findings), findings


def test_non_category_level2_heading_is_error(tmp_path):
    _write_notes(tmp_path, "# sysforge (unreleased)\n\n## Improvements\n\n- thing\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("not a Keep a Changelog category" in f.message for f in findings)


def test_non_category_sub_heading_is_allowed(tmp_path):
    """A `### foo` that is *not* a category word is legitimate entry sub-prose."""
    _write_notes(
        tmp_path,
        "# sysforge (unreleased)\n\n## Changed\n\n- **`1.0.0-F1` — thing.** body\n\n"
        "### Migration\n\nsteps\n",
    )
    assert check_standards.check_changelog(tmp_path) == []


# ---------------------------------------------------------------------------
# Accumulator entry ordering (2.6.1-B20)
# ---------------------------------------------------------------------------

def test_ascending_ids_pass(tmp_path):
    _write_notes(tmp_path,
                 "# t\n\n## Added\n\n- **`1.0.0-B2` — a.** x\n\n"
                 "- **`1.0.0-F1` — b.** x\n\n- **`1.0.0-STD1` — c.** x\n\n"
                 "- **`1.1.0-F1` — d.** x\n")
    assert check_standards.check_changelog(tmp_path) == []


def test_descending_ids_are_error(tmp_path):
    _write_notes(tmp_path,
                 "# t\n\n## Added\n\n- **`1.0.0-F2` — b.** x\n\n"
                 "- **`1.0.0-F1` — a.** x\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("must ascend by roadmap ID" in f.message
               and "1.0.0-F1 follows 1.0.0-F2" in f.message
               for f in findings), findings


def test_version_ordering_is_numeric_not_lexical(tmp_path):
    """2.10.0 sorts after 2.9.0 — a string compare would invert this."""
    _write_notes(tmp_path,
                 "# t\n\n## Added\n\n- **`2.10.0-F1` — a.** x\n\n"
                 "- **`2.9.0-F1` — b.** x\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("2.9.0-F1 follows 2.10.0-F1" in f.message for f in findings), findings


def test_sections_are_ordered_independently(tmp_path):
    """A later section restarting at a lower ID is not drift."""
    _write_notes(tmp_path, "# t\n\n## Added\n\n- **`1.0.0-F9` — a.** x\n\n"
                           "## Fixed\n\n- **`1.0.0-B1` — b.** x\n")
    assert check_standards.check_changelog(tmp_path) == []


def test_entry_without_roadmap_id_is_error(tmp_path):
    _write_notes(tmp_path, "# t\n\n## Added\n\n- untraceable thing\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("cites no roadmap ID" in f.message for f in findings), findings


def test_first_id_in_entry_is_the_filing_id(tmp_path):
    """Later IDs in an entry body are cross-references, not the filing ID."""
    _write_notes(tmp_path, "# t\n\n## Added\n\n- **`1.0.0-F1` — a.** Supersedes\n"
                           "  the approach from (1.0.0-F9)\n\n- **`1.0.0-F2` — b.** x\n")
    assert check_standards.check_changelog(tmp_path) == []


def test_entry_not_leading_with_its_id_is_error(tmp_path):
    """An entry must open `- **`<ID>` — <title>.**`, ROADMAP.md's own shape."""
    _write_notes(tmp_path, "# t\n\n## Added\n\n- a thing shipped (1.0.0-F1)\n")
    findings = check_standards.check_changelog(tmp_path)
    assert any("must lead with its roadmap ID" in f.message
               for f in findings), findings


def test_promoted_from_lineage_is_not_the_filing_id(tmp_path):
    """`promoted from <ID>` names the old pre-promotion ID; it must not sort.

    The ID-first lead makes this structural — the filing ID precedes any body
    mention — but the stripping stays covered so it cannot regress into
    counting a lineage ID if the lead check is ever relaxed.
    """
    _write_notes(tmp_path, "# t\n\n## Added\n\n- **`1.0.0-F1` — a.** x\n\n"
                           "- **`1.0.0-F2` — b (promoted from 1.0.0-Q1).** x\n")
    assert check_standards.check_changelog(tmp_path) == []


def test_released_notes_are_exempt_from_ordering(tmp_path):
    """Released v*.md are immutable history — grandfathered, like the Q check."""
    notes = tmp_path / "docs" / "release-notes"
    notes.mkdir(parents=True)
    (notes / "v1.0.0.md").write_text(
        "# v1.0.0\n\n## Added\n\n- b (1.0.0-F2)\n\n- a (1.0.0-F1)\n", encoding="utf-8")
    assert check_standards.check_changelog(tmp_path) == []
