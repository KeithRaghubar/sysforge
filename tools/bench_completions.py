#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Time each `sysforge completions` lookup the shell scripts make (3.5.0-B124).

Reports only, never fails: wall time depends on the machine, so the pass/fail
guards are the hardware-independent ones in tests/test_completions_speed.py.
Each call runs in a fresh interpreter from this checkout, the way a Tab press
starts one, against the live config and caches (the `packages` cache is read,
never rewritten here). Usage: make bench-completions [RUNS=n]
"""
import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# What the completion scripts actually run, one row per call shape.
CALLS = (
    ("local", ["local"]),
    ("manifest", ["manifest"]),
    ("state", ["state"]),
    ("makepkg-flags", ["makepkg-flags"]),
    ("packages --prefix=co", ["packages", "--prefix=co", "--limit=201"]),
)

_ENTRY = ("import sys; from sysforge.cli import main; "
          "sys.argv = ['sysforge', 'completions', *sys.argv[1:]]; main()")


def _time_call(argv: list[str]) -> float:
    start = time.perf_counter()
    subprocess.run([sys.executable, "-c", _ENTRY, *argv], cwd=ROOT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return time.perf_counter() - start


def _rebuild_ms() -> float:
    """The `packages` cache rebuild (pacman -Ssq plus the AUR list), in-process."""
    from sysforge import completions_data
    from sysforge.primitives.config import load_config
    start = time.perf_counter()
    completions_data._build_universe(load_config() or {})  # noqa: SLF001
    return (time.perf_counter() - start) * 1000


def main() -> int:
    ap = argparse.ArgumentParser(description="Time each sysforge completions lookup.")
    ap.add_argument("--runs", type=int, default=5, help="Runs per call (default 5).")
    runs = max(1, ap.parse_args().runs)

    print(f"{'call':<24} {'min ms':>8} {'median ms':>10}   ({runs} runs, fresh interpreter each)")
    for label, argv in CALLS:
        _time_call(argv)  # warm the page cache and the `packages` cache
        samples = [_time_call(argv) * 1000 for _ in range(runs)]
        print(f"{label:<24} {min(samples):>8.0f} {statistics.median(samples):>10.0f}")
    print(f"{'packages cache rebuild':<24} {_rebuild_ms():>8.0f} {'':>10}   "
          "(once per sync DB, AUR list or src dir change)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
