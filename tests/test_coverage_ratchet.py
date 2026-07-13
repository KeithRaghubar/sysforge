# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_coverage_ratchet.py — the soft coverage-floor ratchet (2.2.0-F5).

Exercises the three verdicts (HOLD / IMPROVE / DROP) and their exit codes,
the tolerance band, and the ``--update`` restamp that rewrites the baseline
table + date/suite lines from a fresh coverage.json.
"""
import json
import sys

import pytest

sys.path.insert(0, "tools")
import coverage_ratchet as cr  # noqa: E402


_BASELINE_TEMPLATE = """\
# Coverage baseline (ratchet floor)

Established 2026-01-01 from `make coverage`.

Suite at baseline: **1000 tests passing**, total **{total}%**.

| Scope | Coverage |
|---|---|
| **TOTAL** | **{total}%** |
| `sysforge/cli.py` | {cli}% |

Notes:

- placeholder.
"""


def _write_baseline(path, *, total, cli):
    path.write_text(_BASELINE_TEMPLATE.format(total=total, cli=cli))


def _write_report(path, *, total, files=None):
    doc = {
        "totals": {"percent_covered": total},
        "files": files or {},
    }
    path.write_text(json.dumps(doc))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the tool's module-level paths at tmp fixtures."""
    baseline = tmp_path / "COVERAGE_BASELINE.md"
    report = tmp_path / "coverage.json"
    monkeypatch.setattr(cr, "_BASELINE", baseline)
    monkeypatch.setattr(cr, "_COVERAGE_JSON", report)
    return baseline, report


class TestCheck:
    def test_hold_when_equal(self, wired, capsys):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        _write_report(report, total=80.0)
        assert cr.main(["--check"]) == 0
        assert "HOLD" in capsys.readouterr().out

    def test_improve_above_tolerance(self, wired, capsys):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        _write_report(report, total=84.9)
        assert cr.main(["--check"]) == 0
        assert "IMPROVE" in capsys.readouterr().out

    def test_drop_below_tolerance_exits_3(self, wired, capsys):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        _write_report(report, total=78.0)  # 2 pts < 0.5 tolerance
        assert cr.main(["--check"]) == 3
        assert "DROP" in capsys.readouterr().out

    def test_small_dip_within_tolerance_holds(self, wired, capsys):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        _write_report(report, total=79.7)  # 0.3 pts, under tolerance
        assert cr.main(["--check"]) == 0
        assert "HOLD" in capsys.readouterr().out

    def test_missing_report_errors(self, wired):
        _write_baseline(wired[0], total="80.0", cli="25.0")
        # report file not written
        with pytest.raises(SystemExit):
            cr.main(["--check"])


class TestUpdate:
    def test_restamps_total_and_module_and_date(self, wired, capsys):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        _write_report(
            report, total=84.95,
            files={"sysforge/cli.py": {"summary": {"percent_covered": 96.4}}},
        )
        assert cr.main(["--update", "--tests", "3750"]) == 0
        text = baseline.read_text()
        assert "| **TOTAL** | **85.0%** |" in text
        assert "| `sysforge/cli.py` | 96.4% |" in text
        assert "**3750 tests passing**" in text
        assert text.startswith("# Coverage baseline")
        # The Established/Re-seeded date line is refreshed, not duplicated.
        assert text.count("Re-seeded ") == 1
        assert "Established " not in text

    def test_update_leaves_untracked_module_row_untouched(self, wired):
        baseline, report = wired
        _write_baseline(baseline, total="80.0", cli="25.0")
        # report has no entry for cli.py → its row must be preserved as-is.
        _write_report(report, total=81.0, files={})
        assert cr.main(["--update"]) == 0
        assert "| `sysforge/cli.py` | 25.0% |" in baseline.read_text()
