"""
test_sentinel_scope.py — coverage for the shared ``sentinel_scope`` context
manager used by both the toolchain pipeline stage and the CLI verb runner.

Mirrors the behaviour previously tested only via the toolchain stage's
inline copy; the shared primitive must guarantee:

  • Sentinel is written on entry and cleared on a clean exit.
  • Sentinel is left in place on any exception inside the body.
  • CleanExitRequested is translated to a RuntimeError that surfaces
    both ``retry_cmd`` and ``recovery_cmd`` to the operator.
"""
from __future__ import annotations

import pytest

from sysforge.primitives.interrupt import CleanExitRequested
from sysforge.primitives.stage_sentinel import StageSentinel, sentinel_scope


def test_clean_exit_clears_sentinel(tmp_path):
    """Body completes without raising → sentinel cleared."""
    with sentinel_scope(tmp_path, "toolchain", recovery_cmd="sudo pacman -S llvm"):
        record = StageSentinel(tmp_path).get_active()
        assert record is not None
        assert record["stage"] == "toolchain"
    assert StageSentinel(tmp_path).get_active() is None


def test_runtime_error_preserves_sentinel(tmp_path):
    """Body raises RuntimeError → sentinel left in place."""
    with pytest.raises(RuntimeError):
        with sentinel_scope(tmp_path, "kernel", recovery_cmd="sudo pacman -S linux"):
            raise RuntimeError("kernel build failed")
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["stage"] == "kernel"


def test_clean_exit_requested_translates_to_runtime_error(tmp_path):
    """CleanExitRequested inside scope → RuntimeError with retry+recovery hints,
    sentinel left in place for next-run recovery prompt."""
    with pytest.raises(RuntimeError) as exc_info:
        with sentinel_scope(
            tmp_path,
            "toolchain",
            recovery_cmd="sudo pacman -S llvm llvm-libs",
            retry_cmd="sysforge run toolchain",
        ):
            raise CleanExitRequested()
    msg = str(exc_info.value)
    assert "interrupted by user" in msg.lower()
    assert "sysforge run toolchain" in msg
    assert "sudo pacman -S llvm llvm-libs" in msg
    assert StageSentinel(tmp_path).get_active() is not None


def test_metadata_round_trips_through_sentinel_file(tmp_path):
    """Extra kwargs land in the sentinel TOML for the recovery prompt."""
    with pytest.raises(RuntimeError):
        with sentinel_scope(
            tmp_path,
            "toolchain",
            recovery_cmd="sudo pacman -S llvm",
            compiler="llvm",
            pgo=True,
        ):
            raise RuntimeError("boom")
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["compiler"] == "llvm"
    assert record["pgo"] is True
    assert record["recovery_cmd"] == "sudo pacman -S llvm"
