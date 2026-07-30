"""
test_verb_runner.py — coverage for the CLI verb dispatcher.

Exercises the three-phase contract (pre_check → execute → post_validate)
plus the sentinel-handoff and error-mapping paths in
:func:`sysforge.verbs.runner.run_verb`.
"""
from __future__ import annotations

import argparse

import pytest

from sysforge.primitives.config import ConfigError
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

    def __init__(self, *, pre_result=None, exec_rc=0, raise_in=None,
                 exc_cls: type[Exception] = RuntimeError):
        self._pre_result = pre_result or PreCheckResult()
        self._exec_rc = exec_rc
        self._raise_in = raise_in
        self._exc_cls = exc_cls
        self.calls = []

    def pre_check(self, args) -> PreCheckResult:
        self.calls.append("pre_check")
        if self._raise_in == "pre_check":
            raise self._exc_cls("pre_check exploded")
        return self._pre_result

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        self.calls.append("execute")
        if self._raise_in == "execute":
            raise self._exc_cls("execute exploded")
        return ExecResult(exit_code=self._exec_rc)

    def post_validate(self, args, pre, result) -> None:
        self.calls.append("post_validate")
        if self._raise_in == "post_validate":
            raise self._exc_cls("post_validate exploded")


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


@pytest.mark.parametrize("phase", ["pre_check", "execute", "post_validate"])
def test_config_error_in_phase_returns_one_not_a_traceback(phase):
    """2.6.1-B3: a config-validation failure is an expected outcome, not a bug.

    ``ConfigError`` subclasses ``RuntimeError`` precisely so it lands on the
    runner's handled path — the user upgrading with a stale ``packages.toml``
    gets an error line and exit 1, not a stack trace. This is the guard
    against someone "simplifying" ConfigError back to a bare ValueError.
    """
    v = _CountingVerb(raise_in=phase, exc_cls=ConfigError)
    rc = run_verb(v, _args(state_dir=None))
    assert rc == 1


def test_non_runtime_error_still_propagates():
    """The counterpart guard: widening the runner to catch ValueError would
    make genuine bugs look like tidy user errors. They must still traceback."""
    v = _CountingVerb(raise_in="execute", exc_cls=ValueError)
    with pytest.raises(ValueError):
        run_verb(v, _args(state_dir=None))


# ---------------------------------------------------------------------------
# Universal startup progress phase
# ---------------------------------------------------------------------------

class _PhaseRecorder:
    """Drop-in for ``runner.progress`` that records phase() calls."""

    def __init__(self):
        self.calls = []

    def phase(self, label):
        self.calls.append(label)


@pytest.mark.parametrize(
    "pre_result, raise_in, name, expected_label",
    [
        (None, None, "test", "test: starting…"),
        (PreCheckResult(skip_reason="nope"), None, "test", "test: starting…"),
        (PreCheckResult(blocker="bad", exit_code=2), None, "test", "test: starting…"),
        (None, "execute", "test", "test: starting…"),
        (None, None, "run-toolchain", "toolchain: starting…"),
    ],
)
def test_startup_phase_painted_and_cleared(
    monkeypatch, pre_result, raise_in, name, expected_label
):
    """Every dispatch path paints a humanized startup phase then clears it."""
    rec = _PhaseRecorder()
    monkeypatch.setattr("sysforge.verbs.runner.progress", rec)
    v = _CountingVerb(pre_result=pre_result, raise_in=raise_in)
    v.name = name
    run_verb(v, _args(state_dir=None))
    # First call paints the startup label; the run always ends by clearing.
    assert rec.calls[0] == expected_label
    assert rec.calls[-1] is None


# ---------------------------------------------------------------------------
# Consolidated per-verb run log (F6)
# ---------------------------------------------------------------------------

class _LoggingVerb(_CountingVerb):
    """A verb that opts into a consolidated run log and emits one UI line."""

    def __init__(self, *, basename="sysforge-build.log", exec_rc=0, raise_in=None):
        super().__init__(exec_rc=exec_rc, raise_in=raise_in)
        self._basename = basename

    def unified_log_basename(self, args):
        return self._basename

    def execute(self, args, pre):
        from sysforge import log as _log
        _log.get_logger("TEST").ui("hello from execute")
        return super().execute(args, pre)


def test_consolidated_log_written_and_persisted(tmp_path):
    """A verb that returns a basename leaves a kept log in --log-dir."""
    v = _LoggingVerb()
    rc = run_verb(v, _args(state_dir=str(tmp_path), log_dir=str(tmp_path), dry_run=False))
    assert rc == 0
    log_path = tmp_path / "sysforge-build.log"
    assert log_path.exists()
    body = log_path.read_text()
    assert "hello from execute" in body
    # persist=True keeps the content — no "cleared after successful run" marker.
    assert "log cleared" not in body


def test_consolidated_log_failure_writes_failed_marker(tmp_path):
    """A verb that raises leaves a kept log carrying a FAILED marker."""
    v = _LoggingVerb(raise_in="execute")
    rc = run_verb(
        v, _args(state_dir=str(tmp_path), log_dir=str(tmp_path), dry_run=False)
    )
    assert rc == 1
    log_path = tmp_path / "sysforge-build.log"
    assert log_path.exists()
    body = log_path.read_text()
    # The pre-failure output and an explicit FAILED marker are both captured.
    assert "hello from execute" in body
    assert "FAILED" in body


def test_consolidated_log_skipped_on_dry_run(tmp_path):
    """Dry runs never write the consolidated log to disk."""
    v = _LoggingVerb()
    rc = run_verb(v, _args(state_dir=str(tmp_path), log_dir=str(tmp_path), dry_run=True))
    assert rc == 0
    assert not (tmp_path / "sysforge-build.log").exists()


def test_consolidated_log_opt_out_writes_nothing(tmp_path):
    """A verb returning None (the default) gets no run-level log."""
    v = _LoggingVerb(basename=None)
    rc = run_verb(v, _args(state_dir=str(tmp_path), log_dir=str(tmp_path), dry_run=False))
    assert rc == 0
    assert not (tmp_path / "sysforge-build.log").exists()


def test_consolidated_log_default_basename_is_none():
    """The base Verb opts out by default, so most verbs are unaffected."""
    assert _CountingVerb().unified_log_basename(_args()) is None


def test_wants_run_log_derives_basename_from_name():
    """A verb opting in via the flag logs to sysforge-<name>.log."""
    v = _CountingVerb()
    v.name = "doctor"
    v.wants_run_log = True
    assert v.unified_log_basename(_args()) == "sysforge-doctor.log"


def test_wants_run_log_preserves_hyphenated_names():
    """Hyphenated verb names map straight through (state-repair, etc.)."""
    v = _CountingVerb()
    v.name = "state-repair"
    v.wants_run_log = True
    assert v.unified_log_basename(_args()) == "sysforge-state-repair.log"


def test_wants_run_log_defaults_off():
    """The flag defaults False, so the base verb still opts out."""
    v = _CountingVerb()
    assert v.wants_run_log is False
    assert v.unified_log_basename(_args()) is None


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


# ---------------------------------------------------------------------------
# Substantial verbs opt in to per-verb run logs (2.1.0-F4)
# ---------------------------------------------------------------------------


def test_doctor_verb_writes_kept_run_log(tmp_path):
    """A real substantial verb (doctor) leaves a kept sysforge-doctor.log."""
    from sysforge.doctor import DoctorPkgVerb
    assert DoctorPkgVerb().unified_log_basename(_args(apply=False, dry_run=False)) == (
        "sysforge-doctor.log"
    )


def test_doctor_verb_logs_on_dry_run_apply(tmp_path):
    """`doctor --apply --dry-run` still opens its own run log — the rebuild
    delegation to cmd_update only happens on a real (non-dry-run) apply."""
    from sysforge.doctor import DoctorPkgVerb
    assert DoctorPkgVerb().unified_log_basename(_args()) == "sysforge-doctor.log"
    assert DoctorPkgVerb().unified_log_basename(
        _args(apply=True, dry_run=True)
    ) == "sysforge-doctor.log"


def test_doctor_verb_opts_out_of_run_log_on_real_apply(tmp_path):
    """`doctor --apply` (non-dry-run) delegates to cmd_update, which opens its
    own sysforge-update.log on the process-global unified-log handle
    (2.1.0-F4). Doctor must not also open a log here — the singleton has no
    reentrancy guard, so a doctor-opened handle would be leaked (never
    closed) and rebuild output would silently land in the wrong file."""
    from sysforge.doctor import DoctorPkgVerb
    assert DoctorPkgVerb().unified_log_basename(
        _args(apply=True, dry_run=False)
    ) is None


def test_flagged_verb_persists_log_via_runner(tmp_path):
    """Through run_verb, a wants_run_log verb keeps its log on success."""
    v = _CountingVerb()
    v.name = "doctor"
    v.wants_run_log = True
    rc = run_verb(
        v, _args(state_dir=str(tmp_path), log_dir=str(tmp_path), dry_run=False)
    )
    assert rc == 0
    log_path = tmp_path / "sysforge-doctor.log"
    assert log_path.exists()
    assert "log cleared" not in log_path.read_text()
