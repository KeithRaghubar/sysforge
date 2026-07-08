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
    _write_notes(tmp_path, "# sysforge (unreleased)\n\n## Changed\n\n- thing (1.0.0-F1)\n")
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
        "# sysforge (unreleased)\n\n## Changed\n\n- thing\n\n### Migration\n\nsteps\n",
    )
    assert check_standards.check_changelog(tmp_path) == []
