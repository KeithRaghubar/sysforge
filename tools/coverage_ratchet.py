# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
coverage_ratchet.py — soft coverage floor for release prep.

Reads the coverage report produced by ``make coverage``
(``coverage.json``) and the recorded floor in
``tests/COVERAGE_BASELINE.md``, and either:

  --check   (default) compares current TOTAL against the baseline TOTAL and
            reports HOLD / IMPROVE / DROP. Exit 0 unless the total dropped
            below the floor (beyond a small tolerance), in which case exit 3
            so a caller can map it to a *warning* rather than a hard failure.
            The suite is instrumented and slow, and a drop is advisory — this
            is a ratchet, not a gate.

  --update  re-stamp the baseline from the current report: rewrites the TOTAL
            and every module row already tracked in the table with today's
            numbers and date. Run this when cutting a release so the floor
            tracks the shipped suite instead of a stale snapshot.

The baseline's TOTAL row is the only value ``--check`` gates on; the per-module
rows are informational (refreshed on ``--update`` for awareness, never gated).
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BASELINE = _REPO_ROOT / "tests" / "COVERAGE_BASELINE.md"
_COVERAGE_JSON = _REPO_ROOT / "coverage.json"

# A drop smaller than this (percentage points) is treated as noise — coverage
# wobbles slightly with unrelated changes and we don't want a warn on rounding.
_TOLERANCE = 0.5

# Matches a table row: | `path` | 87.6% |  or  | **TOTAL** | **80.3%** |
_ROW_RE = re.compile(
    r"^\|\s*(?P<label>.+?)\s*\|\s*\*{0,2}(?P<pct>[0-9]+(?:\.[0-9]+)?)%\*{0,2}\s*\|\s*$"
)
_TOTAL_LABEL_RE = re.compile(r"\*\*TOTAL\*\*", re.IGNORECASE)
# The label cell of a module row wraps the module path in backticks.
_MODULE_LABEL_RE = re.compile(r"`([^`]+)`")


def _load_report() -> dict:
    if not _COVERAGE_JSON.exists():
        sys.stderr.write(
            f"ERROR: {_COVERAGE_JSON.name} not found — run `make coverage` first.\n"
        )
        raise SystemExit(1)
    with _COVERAGE_JSON.open() as fh:
        return json.load(fh)


def _current_total(report: dict) -> float:
    return float(report["totals"]["percent_covered"])


def _current_module_pct(report: dict, module: str) -> float | None:
    entry = report["files"].get(module)
    if entry is None:
        return None
    return float(entry["summary"]["percent_covered"])


def _baseline_total() -> float:
    """Parse the TOTAL percentage out of the baseline markdown table."""
    for line in _BASELINE.read_text().splitlines():
        if not _TOTAL_LABEL_RE.search(line):
            continue
        m = _ROW_RE.match(line)
        if m:
            return float(m.group("pct"))
    sys.stderr.write(f"ERROR: no TOTAL row found in {_BASELINE}.\n")
    raise SystemExit(1)


def _check(report: dict) -> int:
    current = _current_total(report)
    floor = _baseline_total()
    delta = current - floor
    if delta < -_TOLERANCE:
        print(
            f"RATCHET: DROP — total {current:.1f}% is below the "
            f"{floor:.1f}% floor (Δ{delta:+.1f} pts). "
            f"Investigate, or re-stamp with `make coverage-ratchet-update` "
            f"if the drop is intended."
        )
        return 3
    if delta > _TOLERANCE:
        print(
            f"RATCHET: IMPROVE — total {current:.1f}% is above the "
            f"{floor:.1f}% floor (Δ{delta:+.1f} pts). "
            f"Re-stamp to lock in the gain: `make coverage-ratchet-update`."
        )
        return 0
    print(f"RATCHET: HOLD — total {current:.1f}% at the {floor:.1f}% floor.")
    return 0


def _update(report: dict) -> int:
    """Rewrite the baseline table's percentages + date from the current report."""
    total = _current_total(report)
    today = datetime.date.today().isoformat()
    ntests = _suite_size()

    out: list[str] = []
    for line in _BASELINE.read_text().splitlines():
        # Re-stamp the "Established"/"Re-seeded" date line if present.
        if line.startswith("Re-seeded ") or line.startswith("Established "):
            out.append(f"Re-seeded {today} from `make coverage`.")
            continue
        # Re-stamp the suite-size sentence.
        if ntests is not None and line.startswith("Suite at baseline:"):
            out.append(
                f"Suite at baseline: **{ntests} tests passing**, "
                f"total **{total:.1f}%**."
            )
            continue
        m = _ROW_RE.match(line)
        if m:
            if _TOTAL_LABEL_RE.search(line):
                out.append(f"| **TOTAL** | **{total:.1f}%** |")
                continue
            mod = _MODULE_LABEL_RE.search(m.group("label"))
            if mod:
                pct = _current_module_pct(report, mod.group(1))
                if pct is not None:
                    out.append(f"| `{mod.group(1)}` | {pct:.1f}% |")
                    continue
        out.append(line)

    _BASELINE.write_text("\n".join(out) + "\n")
    print(f"RATCHET: baseline re-stamped to total {total:.1f}% ({today}).")
    return 0


def _suite_size() -> int | None:
    """Best-effort test count from the coverage report's context, else None.

    coverage.json doesn't record the test count; callers who want it accurate
    can pass --tests. Absent that we leave the recorded count untouched by
    returning None (the update loop then keeps the existing line)."""
    return _SUITE_SIZE_OVERRIDE


_SUITE_SIZE_OVERRIDE: int | None = None


def main(argv: list[str] | None = None) -> int:
    global _SUITE_SIZE_OVERRIDE
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="compare current total against the floor (default)")
    mode.add_argument("--update", action="store_true",
                      help="re-stamp the baseline from the current report")
    ap.add_argument("--tests", type=int, default=None,
                    help="test count to record when --update re-stamps")
    args = ap.parse_args(argv)
    _SUITE_SIZE_OVERRIDE = args.tests

    report = _load_report()
    if args.update:
        return _update(report)
    return _check(report)


if __name__ == "__main__":
    raise SystemExit(main())
