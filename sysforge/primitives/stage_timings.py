# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stage_timings.py — ``<state_dir>/stage_timings.toml``, how long each step of a
stage took (3.5.0-F6).

The one reader and writer. One table per stage (``[toolchain]``), one key per
step (``optimize = "3720,3655"``): a bounded ring of the last five whole-second
active durations, newest last, the shape ``build_state``'s ``build_seconds``
uses. The step names already separate the kinds of work (an instrumented
``instrument`` build is not an ``optimize`` build), so the steps every path
shares (``audit``, ``install``) learn from every run.

The progress bar's stage ETA is the median of each ring. Advisory only: a read
that fails is no history, and a write never escalates (the ``bench_state``
shape) and never raises, so timing can never fail a stage.
"""
from __future__ import annotations

import contextlib
import tomllib
from pathlib import Path
from statistics import median

from sysforge.primitives import atomic_write

FILENAME = "stage_timings.toml"
_RING = 5


def _read(state_dir: Path) -> dict:
    try:
        with (Path(state_dir) / FILENAME).open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _ring(value) -> list[int]:
    return [int(s) for s in str(value or "").split(",") if s.strip().isdigit()]


def expected(state_dir: Path, stage: str) -> dict[str, int]:
    """Each recorded step of *stage* → its median duration in seconds."""
    table = _read(state_dir).get(stage)
    if not isinstance(table, dict):
        return {}
    out: dict[str, int] = {}
    for step, value in table.items():
        ring = _ring(value)
        if ring:
            out[step] = int(median(ring))
    return out


def _serialize(data: dict) -> str:
    out = ["# Written by sysforge: recent step durations per stage, in seconds,"
           " for the progress bar's estimate. Safe to delete."]
    for stage in sorted(data):
        out.append(f"\n[{stage}]")
        for step, ring in data[stage].items():
            out.append(f'{step} = "{ring}"')
    return "\n".join(out) + "\n"


def _write(path: Path, text: str) -> None:
    """Temp file in the destination dir, fsync, rename. Never escalates."""
    atomic_write.write_state(path, text)


def record(state_dir: Path, stage: str, step: str, seconds: float) -> None:
    """Append *seconds* to *stage*'s ring for *step*. Never raises."""
    if not step.replace("-", "").replace("_", "").isalnum():
        return  # only bare TOML keys are written
    with contextlib.suppress(Exception):
        data = {s: {k: v for k, v in t.items() if isinstance(v, str)}
                for s, t in _read(state_dir).items() if isinstance(t, dict)}
        table = data.setdefault(stage, {})
        ring = [*_ring(table.get(step)), int(seconds)][-_RING:]
        table[step] = ",".join(str(s) for s in ring)
        _write(Path(state_dir) / FILENAME, _serialize(data))
