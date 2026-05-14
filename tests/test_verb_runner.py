"""
test_verb_runner.py — coverage for the CLI verb dispatcher.

Exercises the three-phase contract (pre_check → execute → post_validate)
plus the sentinel-handoff and error-mapping paths in
:func:`sysforge.verbs.runner.run_verb`.
"""
from __future__ import annotations

import argparse

import pytest

from sysforge.primitives.stage_sentinel import StageSentinel
from sysforge.verbs import ExecResult, PreCheckResult, Verb, run_verb


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# Pre-check terminal states
# ---------------------------------------------------------------------------

class _CountingVerb(Verb):
    name = "test"
    requires_sentinel = False

    def __init__(self, *, pre_result=None, exec_rc=0, raise_in=None):
        self._pre_result = pre_result or PreCheckResult()
        self._exec_rc = exec_rc
        self._raise_in = raise_in
        self.calls = []

    def pre_check(self, args) -> PreCheckResult:
        self.calls.append("pre_check")
        if self._raise_in == "pre_check":
            raise RuntimeError("pre_check exploded")
        return self._pre_result

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        self.calls.append("execute")
        if self._raise_in == "execute":
            raise RuntimeError("execute exploded")
        return ExecResult(exit_code=self._exec_rc)

    def post_validate(self, args, pre, result) -> None:
        self.calls.append("post_validate")
        if self._raise_in == "post_validate":
            raise RuntimeError("post_validate exploded")


def test_proceed_runs_all_three_phases():
    """Plain success path: pre_check → execute → post_validate."""
    v = _CountingVerb()
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 0
    assert v.calls == ["pre_check", "execute", "post_validate"]


def test_skip_short_circuits_after_pre_check():
    """skip_reason → exit 0, no execute, no post_validate."""
    v = _CountingVerb(pre_result=PreCheckResult(skip_reason="not applicable"))
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 0
    assert v.calls == ["pre_check"]


def test_block_short_circuits_with_exit_code():
    """blocker → exit with provided code, no execute, no post_validate."""
    v = _CountingVerb(pre_result=PreCheckResult(blocker="bad args", exit_code=2))
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 2
    assert v.calls == ["pre_check"]


def test_block_with_exit_code_zero_promotes_to_one():
    """A blocker is always a failure; default exit_code 0 must surface as 1."""
    v = _CountingVerb(pre_result=PreCheckResult(blocker="bad args", exit_code=0))
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 1


def test_execute_exit_code_propagates():
    """ExecResult.exit_code becomes the process exit code."""
    v = _CountingVerb(exec_rc=42)
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 42


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", ["pre_check", "execute", "post_validate"])
def test_runtime_error_in_phase_returns_one(phase):
    """RuntimeError from any phase → exit 1, regardless of where it fires."""
    v = _CountingVerb(raise_in=phase)
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 1


# ---------------------------------------------------------------------------
# Sentinel handoff
# ---------------------------------------------------------------------------

class _SentinelVerb(Verb):
    name = "sentverb"
    requires_sentinel = True

    def __init__(self, *, fail_execute=False, recovery="sudo restore"):
        self._fail_execute = fail_execute
        self._recovery = recovery

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre) -> ExecResult:
        if self._fail_execute:
            raise RuntimeError("execute failed")
        return ExecResult()

    def sentinel_recovery_cmd(self, args, pre):
        return self._recovery


def test_sentinel_cleared_on_success(tmp_path):
    """Verb success → sentinel cleared by sentinel_scope exit."""
    v = _SentinelVerb()
    rc = run_verb(v, _args(state_dir=str(tmp_path)))
    assert rc == 0
    assert StageSentinel(tmp_path).get_active() is None


def test_sentinel_preserved_on_failure(tmp_path):
    """Verb RuntimeError → sentinel left in place for next-run recovery."""
    v = _SentinelVerb(fail_execute=True)
    rc = run_verb(v, _args(state_dir=str(tmp_path)))
    assert rc == 1
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["stage"] == "sentverb"
    assert record["recovery_cmd"] == "sudo restore"


def test_sentinel_skipped_when_requires_sentinel_false(tmp_path):
    """Read-only verbs never touch the sentinel file."""
    v = _CountingVerb()
    v.requires_sentinel = False
    rc = run_verb(v, _args(state_dir=str(tmp_path)))
    assert rc == 0
    assert not (tmp_path / "stage_in_progress.toml").exists()


def test_sentinel_metadata_persisted(tmp_path):
    """Custom sentinel_metadata fields land in the sentinel record."""

    class _MetadataVerb(_SentinelVerb):
        def __init__(self):
            super().__init__(fail_execute=True)

        def sentinel_metadata(self, args, pre):
            return {"compiler": "llvm", "pgo": True}

    v = _MetadataVerb()
    run_verb(v, _args(state_dir=str(tmp_path)))
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["compiler"] == "llvm"
    assert record["pgo"] is True
