"""Tests for sysforge.primitives.timing — the pure phase-timing primitive."""
import pytest

from sysforge.primitives.timing import PhaseRecord, PhaseTimer, _fmt_ms, render_report


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
    with pytest.raises(ValueError):
        with t.phase("boom"):
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
    assert lines[1] == "  dep prep    500ms"
    assert lines[2] == "  build: foo  1m05.0s"
    assert lines[3] == "  total       1m05.5s"


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
    src = open(timing.__file__).read()
    assert "sysforge.log" not in src
    assert "from sysforge import log" not in src
