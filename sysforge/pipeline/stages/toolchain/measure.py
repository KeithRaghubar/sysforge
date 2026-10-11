# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/measure.py — point at, or run, the bench after a verified install (3.4.0-F11).

Called once a from-source toolchain is installed and Gate-3-verified, outside
the sentinel. By default it prints one line naming ``sysforge bench toolchain``;
with ``toolchain.toml [bench] after_build = true`` it runs that bench here.

The stage's change summary deliberately carries no timing (see
``identity.ToolchainIdentity``): an uncontrolled figure reads as signal when it
is noise. The bench is the controlled measurement (interleaved runs against the
stock build), which is why it is the thing pointed at or run, and why it stays
opt-in: right after an hours-long build the machine is at its noisiest, and
the bench may download the stock packages.

A bench that cannot run, or crashes, never fails the stage: the toolchain is
already installed and verified, so the outcome is reported and the run goes on.
"""
from __future__ import annotations

from sysforge import bench_core, log

_log = log.get_logger("TOOLCHAIN")

_HINT = "Measure the new clang against the stock repo build with: sysforge bench toolchain"


def _skipped(message: str) -> None:
    _log.ui(f"{message} (the toolchain is installed; "
            "retry the measurement with `sysforge bench toolchain`)")


def after_install(tcfg, options) -> None:
    """Hint at, or run, the bench for a toolchain this stage just installed."""
    if options.dry_run:
        return
    if not tcfg.bench_after_build:
        _log.ui(_HINT)
        return

    from sysforge.primitives.paths import resolve_state_dir

    _log.ui("[bench] after_build: timing the new clang against the stock repo build")
    try:
        state_dir = resolve_state_dir(options.state_dir)[0]
        bench_core.run_toolchain_bench(state_dir, offline=False, tcfg=tcfg, fail=_skipped)
    except Exception as exc:  # the install already succeeded; never fail it here
        _skipped(f"bench toolchain did not complete: {exc}")
