"""
test_pipeline_runner.py — unit tests for PipelineRunner sequencing and
checkpoint/resume logic.

Uses mock stages that record calls without touching real system tools.
"""

import pytest

from sysforge.pipeline.runner import run_pipeline
from sysforge.pipeline.state import PipelineState
from sysforge.pipeline.stages.base import Stage, RunOptions


# ---------------------------------------------------------------------------
# Mock stage helpers
# ---------------------------------------------------------------------------

class OkStage(Stage):
    """Stage that always succeeds and records it was called."""
    def __init__(self, name, depends_on=None):
        self.name = name
        self.description = f"Mock ok stage: {name}"
        self.depends_on = depends_on or []
        self.called = False

    def run(self, config, state, options):
        self.called = True


class FailStage(Stage):
    """Stage that always raises RuntimeError."""
    def __init__(self, name):
        self.name = name
        self.description = f"Mock fail stage: {name}"
        self.depends_on = []

    def run(self, config, state, options):
        raise RuntimeError(f"[{self.name}] simulated failure")


class StubStage(Stage):
    """Stage that raises NotImplementedError (like stages 1-4)."""
    def __init__(self, name):
        self.name = name
        self.description = "Stub"
        self.depends_on = []

    def run(self, config, state, options):
        raise NotImplementedError(f"{self.name} not implemented")


def make_options(**kwargs):
    defaults = dict(resume=False, start_from=None, force_retry=False,
                    dry_run=False, state_dir=None)
    defaults.update(kwargs)
    return RunOptions(**defaults)


# ---------------------------------------------------------------------------
# Basic sequencing
# ---------------------------------------------------------------------------

def test_all_stages_run_in_order(tmp_path):
    stages = [OkStage("a"), OkStage("b"), OkStage("c")]
    run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)
    assert all(s.called for s in stages)

def test_state_written_after_each_stage(tmp_path):
    stages = [OkStage("a"), OkStage("b")]
    run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)
    state = PipelineState(tmp_path)
    assert state.stage_status("a") == "done"
    assert state.stage_status("b") == "done"

def test_failed_stage_exits(tmp_path):
    stages = [OkStage("a"), FailStage("b"), OkStage("c")]
    with pytest.raises(SystemExit):
        run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)
    state = PipelineState(tmp_path)
    assert state.stage_status("a") == "done"
    assert state.stage_status("b") == "failed"
    # c should not have run
    assert state.stage_status("c") == "pending"

def test_stub_stage_exits(tmp_path):
    stages = [StubStage("unimplemented")]
    with pytest.raises(SystemExit):
        run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)


# ---------------------------------------------------------------------------
# --resume
# ---------------------------------------------------------------------------

def test_resume_skips_done_stages(tmp_path):
    # Set up state with "a" already done
    state = PipelineState(tmp_path)
    state.mark_done("a")
    state.save()

    a = OkStage("a")
    b = OkStage("b")
    run_pipeline({}, make_options(resume=True, state_dir=tmp_path), stages=[a, b])
    assert not a.called   # already done, skipped
    assert b.called

def test_resume_with_all_done(tmp_path):
    state = PipelineState(tmp_path)
    for name in ("a", "b"):
        state.mark_done(name)
    state.save()

    stages = [OkStage("a"), OkStage("b")]
    # Should return without error
    run_pipeline({}, make_options(resume=True, state_dir=tmp_path), stages=stages)
    assert not any(s.called for s in stages)

def test_no_resume_flag_with_existing_state_exits(tmp_path):
    # Create a state file
    state = PipelineState(tmp_path)
    state.init_meta()
    state.save()

    stages = [OkStage("a")]
    with pytest.raises(SystemExit):
        run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)


# ---------------------------------------------------------------------------
# --start-from
# ---------------------------------------------------------------------------

def test_start_from_skips_prior_stages(tmp_path):
    a = OkStage("a")
    b = OkStage("b")
    c = OkStage("c")
    run_pipeline(
        {}, make_options(start_from="b", state_dir=tmp_path),
        stages=[a, b, c],
    )
    assert not a.called   # skipped-to
    assert b.called
    assert c.called

def test_start_from_marks_prior_skipped_to(tmp_path):
    stages = [OkStage("a"), OkStage("b"), OkStage("c")]
    run_pipeline(
        {}, make_options(start_from="b", state_dir=tmp_path),
        stages=stages,
    )
    state = PipelineState(tmp_path)
    assert state.stage_status("a") == "skipped_to"
    assert state.stage_status("b") == "done"

def test_start_from_unknown_stage_exits(tmp_path):
    stages = [OkStage("a")]
    with pytest.raises(SystemExit):
        run_pipeline(
            {}, make_options(start_from="nonexistent", state_dir=tmp_path),
            stages=stages,
        )

def test_start_from_with_resume_preserves_prior_done(tmp_path):
    # "a" is already done in state; --start-from b --resume should not clobber it
    state = PipelineState(tmp_path)
    state.mark_done("a")
    state.save()

    a = OkStage("a")
    b = OkStage("b")
    run_pipeline(
        {}, make_options(start_from="b", resume=True, state_dir=tmp_path),
        stages=[a, b],
    )
    state2 = PipelineState(tmp_path)
    assert state2.stage_status("a") == "done"   # preserved, not overwritten


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_run(tmp_path):
    stages = [OkStage("a"), OkStage("b")]
    run_pipeline({}, make_options(dry_run=True, state_dir=tmp_path), stages=stages)
    assert not any(s.called for s in stages)

def test_dry_run_does_not_write_done_state(tmp_path):
    stages = [OkStage("a")]
    run_pipeline({}, make_options(dry_run=True, state_dir=tmp_path), stages=stages)
    state = PipelineState(tmp_path)
    assert state.stage_status("a") == "pending"


# ---------------------------------------------------------------------------
# Stage dependency validation
# ---------------------------------------------------------------------------

def test_invalid_depends_on_raises(tmp_path):
    bad = OkStage("b", depends_on=["nonexistent"])
    with pytest.raises(ValueError, match="nonexistent"):
        run_pipeline({}, make_options(state_dir=tmp_path), stages=[bad])


def test_shipped_stage_graph_is_valid():
    """Every depends_on in the real STAGES list must name an existing stage.

    Regression guard for a rename hazard: the runner validates the graph on
    every pipeline run, but the synthetic-stage tests above never exercise the
    shipped STAGES list. When the `base_install` stage was folded into `install`
    (2.0.1-F3), `hardware.depends_on` still pointed at the deleted stage — a
    dead-on-arrival ValueError that unit tests missed. This asserts the real
    graph resolves.
    """
    from sysforge.pipeline.runner import _validate_stages
    from sysforge.pipeline.stages import STAGES

    _validate_stages(STAGES)  # raises ValueError on a dangling dependency

    names = {s.name for s in STAGES}
    for stage in STAGES:
        for dep in stage.depends_on:
            assert dep in names, f"{stage.name} depends on unknown stage {dep!r}"
