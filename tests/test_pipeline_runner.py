"""
test_pipeline_runner.py — unit tests for PipelineRunner sequencing and
checkpoint/resume logic.

Uses mock stages that record calls without touching real system tools.
"""

import pytest

from sysforge.pipeline.runner import run_pipeline
from sysforge.pipeline.state import PipelineState
from sysforge.pipeline.stages.base import BootstrapRebootRequired, RunOptions, Stage
from sysforge.primitives import change_report
from sysforge.primitives.change_report import ExtraBlock, PkgFacts


# ---------------------------------------------------------------------------
# Mock stage helpers
# ---------------------------------------------------------------------------

class OkStage(Stage):
    """Stage that always succeeds and records it was called."""
    def __init__(self, name, depends_on=None, makepkg_bearing=False):
        self.name = name
        self.description = f"Mock ok stage: {name}"
        self.depends_on = depends_on or []
        self.makepkg_bearing = makepkg_bearing
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


# ---------------------------------------------------------------------------
# Root guard on makepkg-bearing stages (2.1.0-B4)
# ---------------------------------------------------------------------------

def test_makepkg_bearing_stage_refuses_root(tmp_path, monkeypatch):
    """A makepkg-bearing stage reached at euid 0 must fail fast, not run.

    The bootstrap phase runs as root on the live ISO; only the build stages
    (toolchain/packages/kernel) carry makepkg_bearing and must be blocked at
    root. Regression guard for the ISO bootstrap being dead-on-arrival when the
    no-root check lived on the pipeline verb instead of the stage.
    """
    monkeypatch.setattr("sysforge.pipeline.runner.os.geteuid", lambda: 0)
    build = OkStage("packages", makepkg_bearing=True)
    with pytest.raises(SystemExit):
        run_pipeline({}, make_options(state_dir=tmp_path), stages=[build])
    assert not build.called  # blocked before run()


def test_bootstrap_stages_run_as_root(tmp_path, monkeypatch):
    """Non-makepkg (bootstrap) stages must run even at euid 0."""
    monkeypatch.setattr("sysforge.pipeline.runner.os.geteuid", lambda: 0)
    stages = [OkStage("install"), OkStage("hardware"), OkStage("configure")]
    run_pipeline({}, make_options(state_dir=tmp_path), stages=stages)
    assert all(s.called for s in stages)


def test_shipped_build_stages_are_makepkg_bearing():
    """The three build stages carry the flag; bootstrap stages do not."""
    from sysforge.pipeline.stages import STAGES

    by_name = {s.name: s for s in STAGES}
    for name in ("toolchain", "packages", "kernel"):
        assert by_name[name].makepkg_bearing, f"{name} must be makepkg_bearing"
    for name in ("install", "hardware", "configure", "reconfigure"):
        assert not by_name[name].makepkg_bearing, f"{name} must not be makepkg_bearing"


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


# ---------------------------------------------------------------------------
# Standalone run logging (2.1.0-F4)
# ---------------------------------------------------------------------------

def test_standalone_stage_writes_per_stage_log(tmp_path, monkeypatch):
    """Standalone run leaves sysforge-run-<stage>.log, not shared sysforge.log."""
    from sysforge.pipeline import runner as _runner
    monkeypatch.setattr(_runner, "emit_system_probes", lambda: None)
    monkeypatch.setattr(_runner, "reset_session", lambda: None)
    stage = OkStage("kernel")
    stage.stateless = True
    _runner.run_stage_standalone(
        stage, {}, make_options(state_dir=tmp_path, log_dir=tmp_path))
    assert (tmp_path / "sysforge-run-kernel.log").exists()
    assert not (tmp_path / "sysforge.log").exists()


def test_standalone_stage_log_persisted_on_success(tmp_path, monkeypatch):
    """Kept on success even with the default persist_log=False."""
    from sysforge.pipeline import runner as _runner
    monkeypatch.setattr(_runner, "emit_system_probes", lambda: None)
    monkeypatch.setattr(_runner, "reset_session", lambda: None)
    stage = OkStage("kernel")
    stage.stateless = True
    opts = make_options(state_dir=tmp_path, log_dir=tmp_path)
    assert opts.persist_log is False
    _runner.run_stage_standalone(stage, {}, opts)
    body = (tmp_path / "sysforge-run-kernel.log").read_text()
    assert "log cleared" not in body


def test_standalone_stage_skips_log_on_dry_run(tmp_path, monkeypatch):
    """Dry runs write nothing."""
    from sysforge.pipeline import runner as _runner
    monkeypatch.setattr(_runner, "emit_system_probes", lambda: None)
    monkeypatch.setattr(_runner, "reset_session", lambda: None)
    stage = OkStage("kernel")
    stage.stateless = True
    _runner.run_stage_standalone(
        stage, {}, make_options(state_dir=tmp_path, log_dir=tmp_path, dry_run=True))
    assert not (tmp_path / "sysforge-run-kernel.log").exists()


# ---------------------------------------------------------------------------
# Post-build change summary (2.6.1-F24)
# ---------------------------------------------------------------------------

class ReportingStage(Stage):
    """Fake stage that opts into change reporting."""
    def __init__(self, name="packages", raises=False):
        self.name = name
        self.description = "Mock reporting stage"
        self.depends_on = []
        self.stateless = True
        self.reports_changes = True
        self._raises = raises

    def run(self, config, state, options):
        if self._raises:
            raise RuntimeError(f"[{self.name}] simulated failure")


def _standalone(stage, tmp_path, monkeypatch, **opt_kwargs):
    """Run a stage standalone with probes stubbed, as the log tests above do."""
    from sysforge.pipeline import runner as _runner
    monkeypatch.setattr(_runner, "emit_system_probes", lambda: None)
    monkeypatch.setattr(_runner, "reset_session", lambda: None)
    _runner.run_stage_standalone(
        stage, {}, make_options(state_dir=tmp_path, log_dir=tmp_path, **opt_kwargs))


def _snapshots(monkeypatch, sequence):
    """Feed snapshot() a fixed sequence of return values; record the roots asked for."""
    calls = []

    def fake(root=None):
        calls.append(root)
        return sequence[len(calls) - 1]

    monkeypatch.setattr(change_report, "snapshot", fake)
    return calls


def test_non_reporting_stage_takes_no_snapshots(tmp_path, monkeypatch):
    calls = _snapshots(monkeypatch, [])
    stage = OkStage("configure")
    stage.stateless = True
    _standalone(stage, tmp_path, monkeypatch)
    assert calls == []


def test_reporting_stage_snapshots_both_sides_and_renders(tmp_path, monkeypatch, capsys):
    calls = _snapshots(monkeypatch, [
        {"p": PkgFacts("1-1")},
        {"p": PkgFacts("1-2")},
    ])
    _standalone(ReportingStage(), tmp_path, monkeypatch)
    assert len(calls) == 2
    out = capsys.readouterr().err
    assert "1-1" in out and "1-2" in out


def test_failed_stage_still_reports_partial(tmp_path, monkeypatch, capsys):
    _snapshots(monkeypatch, [
        {"p": PkgFacts("1-1")},
        {"p": PkgFacts("1-2")},
    ])
    # run_stage_standalone's existing RuntimeError handler still calls
    # _log.fatal -> sys.exit(1) after our helper renders the summary — that
    # exit-code authority is unchanged by this task.
    with pytest.raises(SystemExit):
        _standalone(ReportingStage(raises=True), tmp_path, monkeypatch)
    out = capsys.readouterr().err
    assert "FAILED after applying changes" in out


def test_failed_stage_with_no_changes_reports_none_applied(tmp_path, monkeypatch, capsys):
    snap = {"p": PkgFacts("1-1")}
    _snapshots(monkeypatch, [dict(snap), dict(snap)])
    with pytest.raises(SystemExit):
        _standalone(ReportingStage(raises=True), tmp_path, monkeypatch)
    assert "system unchanged" in capsys.readouterr().err.lower()


def test_snapshot_failure_yields_unknown_and_does_not_fail_the_stage(
    tmp_path, monkeypatch, capsys
):
    def boom(root=None):
        raise change_report.SnapshotError("pacman unavailable")

    monkeypatch.setattr(change_report, "snapshot", boom)
    # Must not raise, and must not change the stage's own success determination.
    _standalone(ReportingStage(), tmp_path, monkeypatch)
    out = capsys.readouterr().err
    assert "unavailable" in out.lower()
    assert "pacman unavailable" in out


def test_render_exception_never_propagates(tmp_path, monkeypatch):
    """The explicit guardrail: reporting can never fail a build."""
    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])

    def boom(*args, **kwargs):
        raise ValueError("render bug")

    monkeypatch.setattr(change_report, "render", boom)
    _standalone(ReportingStage(), tmp_path, monkeypatch)  # must not raise


def test_dry_run_takes_no_snapshots(tmp_path, monkeypatch):
    calls = _snapshots(monkeypatch, [])
    _standalone(ReportingStage(), tmp_path, monkeypatch, dry_run=True)
    assert calls == []


def test_change_extras_blocks_are_rendered(tmp_path, monkeypatch, capsys):
    class ExtrasStage(ReportingStage):
        def change_extras(self, config, state, options):
            return [ExtraBlock(label="Extra:", lines=["hello"])]

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])
    _standalone(ExtrasStage(), tmp_path, monkeypatch)
    out = capsys.readouterr().err
    assert "Extra:" in out and "hello" in out


def test_change_extras_failure_degrades_to_warning(tmp_path, monkeypatch, capsys):
    class BadExtrasStage(ReportingStage):
        def change_extras(self, config, state, options):
            raise ValueError("extras bug")

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])
    _standalone(BadExtrasStage(), tmp_path, monkeypatch)  # must not raise
    out = capsys.readouterr().err
    assert "1-2" in out  # version rows still render without the extras


def test_run_pipeline_reporting_stage_renders_before_complete_line(tmp_path, monkeypatch, capsys):
    """The pipeline path (not just standalone) wires the reporting stage,
    and the summary lands before the stage's own completion line so the
    completion line stays last on screen."""
    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])
    stage = ReportingStage("packages")
    run_pipeline({}, make_options(state_dir=tmp_path), stages=[stage])
    out = capsys.readouterr().err
    assert "1-1" in out and "1-2" in out
    summary_idx = out.index("[SYSFORGE] Packages stage changes")
    complete_idx = out.index("packages complete")
    assert summary_idx < complete_idx


def test_run_pipeline_bootstrap_reboot_still_exits_zero_with_reporting_stage(
    tmp_path, monkeypatch, capsys
):
    """BootstrapRebootRequired must still be a clean sys.exit(0), even for a
    stage that opted into change reporting — the helper must not swallow or
    reclassify this control-flow exception."""
    calls = _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])

    class RebootStage(ReportingStage):
        def run(self, config, state, options):
            raise BootstrapRebootRequired("reboot now")

    with pytest.raises(SystemExit) as exc:
        run_pipeline({}, make_options(state_dir=tmp_path), stages=[RebootStage("install")])
    assert exc.value.code == 0
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Exception identity through _run_stage_with_change_report (2.6.1-F24 review)
# ---------------------------------------------------------------------------

def test_same_exception_object_propagates(tmp_path, monkeypatch):
    """The helper must re-raise the exact exception instance the stage
    raised, not a copy or a re-wrap, so the runner's except-clauses (which
    match by type and read the caught instance) keep working unchanged."""
    from sysforge.pipeline import runner as _runner

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])
    sentinel = RuntimeError("sentinel failure")

    class SentinelStage(ReportingStage):
        def run(self, config, state, options):
            raise sentinel

    state = PipelineState(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        _runner._run_stage_with_change_report(SentinelStage(), {}, state, make_options())
    assert exc.value is sentinel


def test_render_exception_does_not_mask_a_failed_stage(tmp_path, monkeypatch):
    """If the stage itself failed AND rendering also blows up, the stage's
    own exception must still win — a reporting bug must never swallow a real
    build failure."""
    from sysforge.pipeline import runner as _runner

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])

    def boom(*args, **kwargs):
        raise ValueError("render bug")

    monkeypatch.setattr(change_report, "render", boom)
    sentinel = RuntimeError("real build failure")

    class SentinelStage(ReportingStage):
        def run(self, config, state, options):
            raise sentinel

    state = PipelineState(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        _runner._run_stage_with_change_report(SentinelStage(), {}, state, make_options())
    assert exc.value is sentinel


def test_keyboardinterrupt_in_the_reporting_window_loses_to_the_stage_error(
    tmp_path, monkeypatch
):
    """2.6.1-F25: the reporting path guards Exception, but stage.run() is
    caught with BaseException. A Ctrl-C landing in the snapshot/render window
    must not replace the stage's own error — otherwise the caller's
    state.mark_failed() never runs and the pipeline stays 'running'."""
    from sysforge.pipeline import runner as _runner

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])
    sentinel = RuntimeError("the real build failure")

    class SentinelStage(ReportingStage):
        def run(self, config, state, options):
            raise sentinel

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_runner.change_report, "render", interrupt)

    state = PipelineState(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        _runner._run_stage_with_change_report(SentinelStage(), {}, state, make_options())
    assert exc.value is sentinel


def test_keyboardinterrupt_in_reporting_still_propagates_when_the_stage_succeeded(
    tmp_path, monkeypatch
):
    """With no stage error to defer to, the interrupt is the only exception in
    flight and must reach the caller — the finally must not swallow it."""
    from sysforge.pipeline import runner as _runner

    _snapshots(monkeypatch, [{"p": PkgFacts("1-1")}, {"p": PkgFacts("1-2")}])

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(_runner.change_report, "render", interrupt)

    state = PipelineState(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        _runner._run_stage_with_change_report(ReportingStage(), {}, state, make_options())
