# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
from sysforge.primitives import build_estimate as be


class _FakeState:
    def __init__(self, data):
        self._d = data

    def get(self, name):
        return self._d.get(name)


def test_estimate_median_and_counts():
    state = _FakeState({
        "a": {"pkgbase": "a", "build_seconds": "100,200,300"},   # median 200
        "b": {"pkgbase": "b", "build_seconds": "60"},            # median 60
        "c": {"pkgbase": "c"},                                   # no ring
    })
    est, known, unknown = be.estimate_seconds(["a", "b", "c", "d"], state)
    assert (est, known, unknown) == (260, 2, 2)  # d is absent → unknown


def test_estimate_dedups_by_pkgbase():
    # split package: two pkgnames share one pkgbase and one ring
    state = _FakeState({
        "libfoo": {"pkgbase": "foo", "build_seconds": "300"},
        "foo":    {"pkgbase": "foo", "build_seconds": "300"},
    })
    est, known, unknown = be.estimate_seconds(["libfoo", "foo"], state)
    assert (est, known, unknown) == (300, 1, 0)  # counted once, not 600


def test_estimate_skips_corrupt_ring_tokens():
    state = _FakeState({"a": {"pkgbase": "a", "build_seconds": "100,x,300"}})
    est, known, unknown = be.estimate_seconds(["a"], state)
    assert (est, known, unknown) == (200, 1, 0)


def test_estimate_all_corrupt_ring_is_unknown():
    state = _FakeState({"b": {"pkgbase": "b", "build_seconds": "x,y"}})
    est, known, unknown = be.estimate_seconds(["b"], state)
    assert (est, known, unknown) == (0, 0, 1)


def test_median_is_outlier_robust():
    state = _FakeState({"a": {"pkgbase": "a", "build_seconds": "100,100,9000"}})
    est, _, _ = be.estimate_seconds(["a"], state)
    assert est == 100  # the 9000 outlier does not dominate


def test_format_estimate_none_when_no_history():
    state = _FakeState({"a": {"pkgbase": "a"}})
    assert be.format_estimate(["a"], state) is None


def test_format_estimate_line():
    state = _FakeState({"a": {"pkgbase": "a", "build_seconds": "8100"}})  # 2h15m
    line = be.format_estimate(["a", "b"], state)
    assert "~2h 15m" in line
    assert "1 of 2" in line and "1 unknown" in line


def test_format_estimate_vs_actual_signed_percent():
    line = be.format_estimate_vs_actual(8100, 9000)  # +11%
    assert "estimated ~2h 15m" in line
    assert "actual ~2h 30m" in line
    assert "+11%" in line
