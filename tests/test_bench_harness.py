# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""bench_harness: interleaved timing, statistics, version relation (3.4.0-F6)."""
from types import SimpleNamespace

import pytest

from sysforge.primitives import bench_harness as bh


def test_summarize_odd_and_even():
    assert bh.summarize([3.0, 1.0, 2.0]) == bh.Stat(2.0, 1.0, 3.0)
    assert bh.summarize([4.0, 1.0, 2.0, 3.0]) == bh.Stat(2.5, 1.0, 4.0)


def test_summarize_rejects_empty():
    with pytest.raises(ValueError):
        bh.summarize([])


@pytest.mark.parametrize("stock,inst,rel", [
    ("23.1.1-1", "23.1.1-1", bh.SAME),
    ("23.1.1-1", "23.1.2-1", bh.DIFFERS),
    ("23.1.1-1", "23.1.1-2", bh.DIFFERS),
    ("22.1.8-1", "23.1.1-1", bh.MAJOR_GAP),
    ("1:23.1.1-1", "23.1.1-1", bh.MAJOR_GAP),
    ("23.1.1-1", "23.1.1.r12.gabc-1", bh.DIFFERS),
])
def test_version_relation(stock, inst, rel):
    assert bh.version_relation(stock, inst) == rel


def test_ratio_is_stock_over_installed():
    r = bh.ratio(bh.Stat(4.2, 4.0, 4.4), bh.Stat(3.5, 3.4, 3.6), bh.SAME)
    assert r == pytest.approx(1.2)


def test_ratio_none_across_major_gap():
    assert bh.ratio(bh.Stat(4, 4, 4), bh.Stat(3, 3, 3), bh.MAJOR_GAP) is None


def test_run_interleaved_order_warmup_and_timing():
    calls = []

    def runner(argv, env):
        calls.append((argv[0], env))
        return SimpleNamespace(returncode=0, stderr="")

    ticks = iter(range(100))
    jobs = {
        "stock": bh.Job((("s1",), ("s2",)), {"LD_LIBRARY_PATH": "/x"}),
        "sysforge": bh.Job((("i1",),)),
    }
    out = bh.run_interleaved(jobs, 2, runner=runner, clock=lambda: float(next(ticks)))
    # warm-up round (untimed) then 2 timed rounds, labels alternating
    assert [c[0] for c in calls] == ["s1", "s2", "i1"] * 3
    assert calls[0][1] == {"LD_LIBRARY_PATH": "/x"} and calls[2][1] is None
    assert len(out["stock"]) == 2 and len(out["sysforge"]) == 2
    assert all(t == 1.0 for t in out["stock"] + out["sysforge"])


def test_run_interleaved_raises_on_failure():
    def runner(argv, env):
        return SimpleNamespace(returncode=1, stderr="a\nb\nerror: boom\n")

    with pytest.raises(bh.BenchFailed) as ei:
        bh.run_interleaved({"stock": bh.Job((("cc", "x.c"),))}, 1, runner=runner)
    assert ei.value.label == "stock" and "boom" in str(ei.value)


def test_run_interleaved_rejects_zero_runs():
    with pytest.raises(ValueError):
        bh.run_interleaved({"a": bh.Job((("x",),))}, 0)


def test_run_interleaved_reports_each_round_before_running_it():
    """3.4.0-F9: one call for the warm-up and one per timed round, each made
    before that round's first compile, so the label names the work under way."""
    events = []

    def runner(argv, env):
        events.append(argv[0])
        return SimpleNamespace(returncode=0, stderr="")

    jobs = {"stock": bh.Job((("s",),)), "sysforge": bh.Job((("i",),))}
    bh.run_interleaved(jobs, 2, runner=runner, on_round=events.append)
    assert events == ["warm-up", "s", "i", "round 1/2", "s", "i", "round 2/2", "s", "i"]
