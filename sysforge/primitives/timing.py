# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
timing.py — wall-clock phase timing for long-running verbs.

Records named phase durations (``time.monotonic_ns()`` deltas, stored as
milliseconds — same lineage as ``env_chain``'s ``cost_ms``) so verbs like
``update`` and ``build`` can report where wall time went. Subprocess-heavy
phases (makepkg, pacman, git) dominate sysforge's runtime, so coarse
per-phase wall-clock is the useful signal here; Python-level hotspots are
the ``--py-profile`` flag's job.

Pure primitive: stdlib only, never imports the pipeline layer, and never
logs — the caller renders ``render_report()`` lines under its own tag
(``update`` under ``[UPDATE]``, ``build`` under ``[BUILD]``).

Public API:
    PhaseTimer().phase(name)        — context manager recording one phase
    PhaseTimer().start(name)/stop() — explicit pair for long inline regions
    PhaseTimer().total_ms()         — sum of recorded durations
    render_report(timer, title=...) -> list[str]
"""
from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class PhaseRecord:
    """One completed (or aborted) phase and how long it ran."""

    name: str
    duration_ms: int


@dataclass
class PhaseTimer:
    """Accumulates :class:`PhaseRecord` entries in completion order."""

    records: list[PhaseRecord] = field(default_factory=list)
    _open: tuple[str, int] | None = field(default=None, repr=False)

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        """Time the enclosed block; a raising body still records its duration."""
        start = time.monotonic_ns()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic_ns() - start) // 1_000_000
            self.records.append(PhaseRecord(name, int(elapsed_ms)))

    def start(self, name: str) -> None:
        """Begin an open phase, paired with :meth:`stop` — for long inline
        regions where re-indenting under a with-block is impractical.
        Starting a new phase while one is open implicitly stops the first."""
        if self._open is not None:
            self.stop()
        self._open = (name, time.monotonic_ns())

    def stop(self) -> None:
        """Close the phase begun by :meth:`start`; no-op when none is open
        (so an early-exit path can call it unconditionally)."""
        if self._open is None:
            return
        name, begun = self._open
        self._open = None
        elapsed_ms = (time.monotonic_ns() - begun) // 1_000_000
        self.records.append(PhaseRecord(name, int(elapsed_ms)))

    def total_ms(self) -> int:
        return sum(r.duration_ms for r in self.records)


def _fmt_ms(ms: int) -> str:
    """Render a duration compactly: sub-second in ms, otherwise seconds."""
    if ms < 1000:
        return f"{ms}ms"
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.1f}s"
    mins, rem = divmod(secs, 60)
    return f"{int(mins)}m{rem:04.1f}s"


_BAR_WIDTH = 24
_BAR_EIGHTHS = "▏▎▍▌▋▊▉█"


def _bar(ms: int, max_ms: int) -> str:
    """Proportional bar scaled so the longest phase fills ``_BAR_WIDTH``
    cells, with eighth-block partials; any nonzero duration shows at
    least a sliver."""
    if max_ms <= 0:
        return ""
    eighths = round(ms / max_ms * _BAR_WIDTH * 8)
    if eighths == 0 and ms > 0:
        eighths = 1
    full, rem = divmod(eighths, 8)
    return "█" * full + (_BAR_EIGHTHS[rem - 1] if rem else "")


def render_report(timer: PhaseTimer, *, title: str = "Phase timings") -> list[str]:
    """Build aligned report lines for the recorded phases plus a total,
    each phase carrying a bar proportional to the longest phase.

    Returns an empty list when nothing was recorded, so callers can emit
    unconditionally without special-casing no-op runs.
    """
    if not timer.records:
        return []
    name_w = max(len(r.name) for r in timer.records)
    max_ms = max(r.duration_ms for r in timer.records)
    durations = [_fmt_ms(r.duration_ms) for r in timer.records] + [
        _fmt_ms(timer.total_ms())
    ]
    dur_w = max(len(d) for d in durations)
    lines = [f"{title}:"]
    lines.extend(
        f"  {r.name.ljust(name_w)}  {d.rjust(dur_w)}  {_bar(r.duration_ms, max_ms)}".rstrip()
        # durations has one extra trailing entry (the total, appended above and
        # consumed separately below) — ragged by design, not a bug.
        for r, d in zip(timer.records, durations, strict=False)
    )
    lines.append(f"  {'total'.ljust(name_w)}  {durations[-1].rjust(dur_w)}")
    return lines
