# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_estimate.py — learned build-time estimate from recorded durations.

Pure/IO-free: operates on an already-loaded BuildState. Each package's
``build_seconds`` is a bounded CSV ring (see build_state); the estimate takes
the outlier-robust *median* of each ring and sums across distinct pkgbases
(so a split package's one build is counted once). Never-built packages are
counted as unknown, never guessed — the number never overstates history.

Public API:
    estimate_seconds(names, build_state) -> (est_seconds, n_known, n_unknown)
    format_estimate(names, build_state) -> str | None
    format_estimate_vs_actual(estimated_s, actual_s) -> str
"""
from __future__ import annotations

from statistics import median

__all__ = ["estimate_seconds", "format_estimate", "format_estimate_vs_actual"]


def _fmt_hms(seconds: int) -> str:
    """Compact duration: ~Nh Nm, ~Nm, or ~Ns."""
    seconds = int(seconds)
    if seconds < 60:
        return f"~{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"~{mins}m"
    return f"~{mins // 60}h {mins % 60:02d}m"


def estimate_seconds(names, build_state) -> tuple[int, int, int]:
    """Sum per-pkgbase median durations. Returns
    (estimated_seconds, n_with_history, n_unknown), both counts by distinct
    pkgbase over the requested names."""
    seen_base_median: dict[str, int | None] = {}
    for name in names:
        entry = build_state.get(name)
        base = (entry or {}).get("pkgbase", name)
        if base in seen_base_median:
            continue
        ring = [
            int(s)
            for s in (entry or {}).get("build_seconds", "").split(",")
            if s.strip().isdigit()
        ]
        seen_base_median[base] = int(median(ring)) if ring else None
    est = sum(v for v in seen_base_median.values() if v is not None)
    known = sum(1 for v in seen_base_median.values() if v is not None)
    unknown = sum(1 for v in seen_base_median.values() if v is None)
    return est, known, unknown


def format_estimate(names, build_state) -> str | None:
    est, known, unknown = estimate_seconds(names, build_state)
    if known == 0:
        return None
    total = known + unknown
    tail = f" ({known} of {total} packages have history; {unknown} unknown)"
    return f"Estimated build time: {_fmt_hms(est)}{tail}"


def format_estimate_vs_actual(estimated_s: int, actual_s: int) -> str:
    pct = round((actual_s - estimated_s) / estimated_s * 100) if estimated_s else 0
    sign = "+" if pct >= 0 else ""
    return (
        f"Build time: estimated {_fmt_hms(estimated_s)} · "
        f"actual {_fmt_hms(actual_s)} ({sign}{pct}%)"
    )
