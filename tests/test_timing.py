"""Tests for sysforge.primitives.timing — the pure phase-timing primitive."""
from pathlib import Path

import pytest

from sysforge.primitives.timing import (
    _BAR_WIDTH,
    PhaseRecord,
    PhaseTimer,
    _bar,
    _fmt_ms,
    render_report,
)


def test_phase_records_in_order():
    t = PhaseTimer()
    with t.phase("first"):
        pass
    with t.phase("second"):
        pass
    assert [r.name for r in t.records] == ["first", "second"]
    assert all(r.duration_ms >= 0 for r in t.records)


def test_phase_measures_duration(monkeypatch):
    ticks = iter([0, 2_500_000_000])  # 2.5s in ns
    monkeypatch.setattr("sysforge.primitives.timing.time.monotonic_ns", lambda: next(ticks))
    t = PhaseTimer()
    with t.phase("work"):
        pass
    assert t.records == [PhaseRecord("work", 2500)]


def test_raising_phase_still_records_and_propagates():
    t = PhaseTimer()
    with pytest.raises(ValueError), t.phase("boom"):
        raise ValueError("nope")
    assert len(t.records) == 1
    assert t.records[0].name == "boom"


def test_start_stop_records_phase():
    t = PhaseTimer()
    t.start("region")
    t.stop()
    assert [r.name for r in t.records] == ["region"]


def test_stop_without_start_is_noop():
    t = PhaseTimer()
    t.stop()
    assert t.records == []


def test_start_while_open_stops_previous():
    t = PhaseTimer()
    t.start("first")
    t.start("second")
    t.stop()
    assert [r.name for r in t.records] == ["first", "second"]


def test_total_ms_sums_records():
    t = PhaseTimer()
    t.records = [PhaseRecord("a", 100), PhaseRecord("b", 250)]
    assert t.total_ms() == 350


def test_render_report_empty_timer():
    assert render_report(PhaseTimer()) == []


def test_render_report_alignment_and_total():
    t = PhaseTimer()
    t.records = [PhaseRecord("dep prep", 500), PhaseRecord("build: foo", 65_000)]
    lines = render_report(t, title="Build timings")
    assert lines[0] == "Build timings:"
    assert lines[1] == "  dep prep      500ms  ▏"
    assert lines[2] == "  build: foo  1m05.0s  " + "█" * _BAR_WIDTH
    assert lines[3] == "  total       1m05.5s"


def test_render_report_bars_scale_to_longest_phase():
    t = PhaseTimer()
    t.records = [PhaseRecord("half", 500), PhaseRecord("full", 1000)]
    lines = render_report(t)
    half_bar = lines[1].split("  ")[-1]
    full_bar = lines[2].split("  ")[-1]
    assert full_bar == "█" * _BAR_WIDTH
    assert half_bar == "█" * (_BAR_WIDTH // 2)


def test_render_report_all_zero_durations_have_no_bars():
    t = PhaseTimer()
    t.records = [PhaseRecord("a", 0), PhaseRecord("b", 0)]
    lines = render_report(t)
    assert lines[1] == "  a  0ms"
    assert lines[2] == "  b  0ms"
    assert not any(line.endswith(" ") for line in lines)


def test_bar_partials_and_sliver():
    assert _bar(0, 1000) == ""
    assert _bar(1, 1000) == "▏"  # nonzero never renders empty
    assert _bar(1000, 1000) == "█" * _BAR_WIDTH
    # 1/8 of one cell beyond half: 12.5 cells -> 12 full + 4/8 partial
    assert _bar(125, 240) == "█" * 12 + "▌"
    assert _bar(500, 0) == ""


def test_fmt_ms_ranges():
    assert _fmt_ms(0) == "0ms"
    assert _fmt_ms(999) == "999ms"
    assert _fmt_ms(1000) == "1.0s"
    assert _fmt_ms(59_949) == "59.9s"
    assert _fmt_ms(60_000) == "1m00.0s"
    assert _fmt_ms(125_300) == "2m05.3s"


def test_primitive_is_log_free():
    import sysforge.primitives.timing as timing

    assert timing.__file__ is not None
    src = Path(timing.__file__).read_text()
    assert "sysforge.log" not in src
    assert "from sysforge import log" not in src
