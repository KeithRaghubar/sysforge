"""
runner.py — pipeline DAG runner

Sequences stage execution, manages checkpoint/resume, and handles
--start-from and --force-retry options.

Resume semantics:
  --resume            Resume from checkpoint. Skips done stages, re-runs from
                      first pending/failed stage. If packages stage has failed
                      packages, prompts the user (or uses --force-retry).
  --start-from NAME   Mark all stages before NAME as skipped_to and start from
                      NAME. Combined with --resume, preserves intra-stage
                      progress for NAME (e.g. already-built packages).

If a state file exists and neither --resume nor --start-from is given,
the runner prints guidance and exits rather than silently overwriting state.

Public API:
    run_pipeline(config, options, stages=None)
"""
import sys
from sysforge import log
_log = log.get_logger("PIPELINE")
from pathlib import Path

_INVOCATION = " ".join(sys.argv)

from sysforge.pipeline.state import PipelineState, resolve_state_dir
from sysforge.pipeline.stages import STAGES, STAGE_NAMES
from sysforge.pipeline.stages.base import BootstrapRebootRequired
from sysforge.primitives.cache_probe import (
    emit_session_report,
    emit_system_probes,
    reset_session,
)


def _validate_stages(stages):
    """Check that all depends_on references are valid stage names."""
    names = {s.name for s in stages}
    for stage in stages:
        for dep in stage.depends_on:
            if dep not in names:
                raise ValueError(
                    f"Stage {stage.name!r} depends_on {dep!r} which is not in the stage list"
                )


def _find_resume_index(stages, state):
    """
    Return the index of the first stage that is not done.
    Returns len(stages) if all are done.
    """
    for i, stage in enumerate(stages):
        status = state.stage_status(stage.name)
        if status not in ("done", "skipped_to"):
            return i
    return len(stages)


def run_stage_standalone(stage, config, options):
    """
    Run a single pipeline stage outside the full pipeline.

    Used by standalone commands (sysforge run packages, sysforge run toolchain, etc.).
    State is loaded from the resolved state dir so toolchain results are
    available to the packages stage when run separately.

    Raises SystemExit on failure.
    """
    options.standalone = True

    state_dir, _ = resolve_state_dir(cli_override=options.state_dir)
    state = PipelineState(state_dir)

    reset_session()
    emit_system_probes()

    log_dir = options.log_dir or state_dir
    unified_log_active = not options.no_unified_log and not options.dry_run
    if unified_log_active:
        unified_log_path = log_dir / "sysforge.log"
        try:
            log.open_unified_log(unified_log_path, purge=options.purge_log)
            _log.info(f"Unified log: {unified_log_path}")
        except PermissionError:
            unified_log_active = False
            _log.warn(f"Cannot write unified log to {unified_log_path} — logging to terminal only")

    _log.info(f"Invocation: {_INVOCATION}")
    success = False
    try:
        if options.dry_run:
            _log.info(f"[dry-run] would run stage: {stage.name} — {stage.description}")
        else:
            _log.info(f"── Stage: {stage.name} ── {stage.description}")
            stage.run(config, state, options)
            if not stage.stateless:
                try:
                    state.save()
                except PermissionError:
                    _log.warn(f"Cannot write state to {state_dir} — progress will not be checkpointed")
            _log.info(f"{stage.name}: complete")
        success = True
    except RuntimeError as e:
        _log.fatal(f"{stage.name}: FAILED — {e}")
    finally:
        if unified_log_active:
            log.close_unified_log(success=success, persist=options.persist_log)
        if options.cache_report:
            emit_session_report()


def run_pipeline(config, options, stages=None):
    """
    Run the pipeline.

    Args:
        config:  loaded SysForge config dict
        options: RunOptions instance
        stages:  override stage list (for testing); defaults to STAGES

    State dir is resolved from options.state_dir, SYSFORGE_STATE_DIR, or default.
    """
    if stages is None:
        stages = STAGES

    _validate_stages(stages)

    # Resolve state dir
    state_dir, state_source = resolve_state_dir(
        cli_override=options.state_dir,
    )
    state = PipelineState(state_dir)

    stage_names = [s.name for s in stages]

    # Guard against accidental state clobber
    existing_state = state.path.exists()
    if existing_state and not options.resume and not options.start_from:
        resolved = state.path.resolve()
        _log.fatal(f"A state file already exists at {resolved}\n"
            f"  Pass --resume to continue from the last checkpoint.\n"
            f"  Pass --start-from <stage> to start from a specific stage.\n"
            f"  Delete {resolved} to start completely fresh.\n"
            f"  (During bootstrap state lives under /mnt/var/lib/sysforge; "
            f"after reboot under /var/lib/sysforge.)")

    # Determine start index
    if options.start_from:
        if options.start_from not in stage_names:
            _log.fatal(f"Unknown stage {options.start_from!r}. Valid stages: {stage_names}")

        start_idx = next(i for i, s in enumerate(stages) if s.name == options.start_from)

        # Mark all prior stages as skipped_to (unless --resume preserves their state)
        if not options.resume:
            for stage in stages[:start_idx]:
                if state.stage_status(stage.name) not in ("done",):
                    state.mark_skipped_to(stage.name)
                    _log.info(f"Marking {stage.name} as skipped-to")

        state.save()
        _log.info(f"Starting from stage: {options.start_from}")

    elif options.resume:
        start_idx = _find_resume_index(stages, state)
        if start_idx == len(stages):
            _log.info("All stages already done — nothing to resume.")
            return
        _log.info(f"Resuming from stage: {stages[start_idx].name}")

    else:
        start_idx = 0

    # Init meta (no-op if already set)
    state.init_meta()
    state.save()

    # Reset cache session accumulator and emit system-level probes
    reset_session()
    emit_system_probes()

    # Open unified log
    unified_log_active = not options.no_unified_log and not options.dry_run
    if unified_log_active:
        log_dir = options.log_dir or state_dir
        unified_log_path = log_dir / "sysforge.log"
        log.open_unified_log(unified_log_path, purge=options.purge_log)
        _log.info(f"Unified log: {unified_log_path}")

    _log.info(f"Invocation: {_INVOCATION}")

    pipeline_success = False
    try:
        # Execute stages
        for stage in stages[start_idx:]:
            status = state.stage_status(stage.name)

            if status == "done":
                _log.info(f"{stage.name}: already done — skipping")
                continue

            if options.dry_run:
                _log.info(f"[dry-run] would run stage: {stage.name} — {stage.description}")
                continue

            _log.info(f"── Stage: {stage.name} ── {stage.description}")

            state.mark_running(stage.name)
            state.save()

            try:
                stage.run(config, state, options)
                state.mark_done(stage.name)
                state.save()
                _log.info(f"{stage.name}: complete ✓")

            except NotImplementedError as e:
                # Stub stage — hard stop with clear guidance
                state.mark_failed(stage.name, str(e))
                state.save()
                _log.error(f"{stage.name}: NOT IMPLEMENTED")
                _log.error(f"  {e}")
                _log.fatal(f"To skip this stage during development:\n"
                    f"    sysforge run pipeline --start-from {stage.name} --resume\n"
                    f"  or jump directly to a later stage:\n"
                    f"    sysforge run pipeline --start-from packages")

            except BootstrapRebootRequired as e:
                state.save()
                _log.ui(str(e))
                _log.ui("State saved. After rebooting, run:")
                _log.ui("  sysforge run pipeline --resume")
                sys.exit(0)

            except RuntimeError as e:
                state.mark_failed(stage.name, str(e))
                state.save()
                _log.error(f"{stage.name}: FAILED — {e}")
                _log.fatal("State saved. Run with --resume to continue after fixing the issue.")

        if not options.dry_run:
            pipeline_success = True
            _log.ui("Pipeline complete — all stages finished successfully.")

    finally:
        if unified_log_active:
            log.close_unified_log(success=pipeline_success, persist=options.persist_log)
            if pipeline_success and not options.persist_log:
                _log.info(f"Unified log cleared after successful run: {unified_log_path}")
        if options.cache_report:
            emit_session_report()
