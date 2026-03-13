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
from pathlib import Path

from sysforge.pipeline.state import PipelineState, resolve_state_dir
from sysforge.pipeline.stages import STAGES, STAGE_NAMES


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
        print(
            f"\n[PIPELINE] A state file already exists at {state.path}\n"
            f"  Pass --resume to continue from the last checkpoint.\n"
            f"  Pass --start-from <stage> to start from a specific stage.\n"
            f"  Delete {state.path} to start completely fresh.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine start index
    if options.start_from:
        if options.start_from not in stage_names:
            print(
                f"[PIPELINE] Unknown stage {options.start_from!r}. "
                f"Valid stages: {stage_names}",
                file=sys.stderr,
            )
            sys.exit(1)

        start_idx = next(i for i, s in enumerate(stages) if s.name == options.start_from)

        # Mark all prior stages as skipped_to (unless --resume preserves their state)
        if not options.resume:
            for stage in stages[:start_idx]:
                if state.stage_status(stage.name) not in ("done",):
                    state.mark_skipped_to(stage.name)
                    print(f"[PIPELINE] Marking {stage.name} as skipped-to")

        state.save()
        print(f"[PIPELINE] Starting from stage: {options.start_from}")

    elif options.resume:
        start_idx = _find_resume_index(stages, state)
        if start_idx == len(stages):
            print("[PIPELINE] All stages already done — nothing to resume.")
            return
        print(f"[PIPELINE] Resuming from stage: {stages[start_idx].name}")

    else:
        start_idx = 0

    # Init meta (no-op if already set)
    state.init_meta()
    state.save()

    # Execute stages
    for stage in stages[start_idx:]:
        status = state.stage_status(stage.name)

        if status == "done":
            print(f"[PIPELINE] {stage.name}: already done — skipping")
            continue

        if options.dry_run:
            print(f"[PIPELINE] [dry-run] would run stage: {stage.name} — {stage.description}")
            continue

        print(f"\n[PIPELINE] ── Stage: {stage.name} ── {stage.description}")

        state.mark_running(stage.name)
        state.save()

        try:
            stage.run(config, state, options)
            state.mark_done(stage.name)
            state.save()
            print(f"[PIPELINE] {stage.name}: complete ✓")

        except NotImplementedError as e:
            # Stub stage — hard stop with clear guidance
            state.mark_failed(stage.name, str(e))
            state.save()
            print(f"\n[PIPELINE] {stage.name}: NOT IMPLEMENTED", file=sys.stderr)
            print(f"  {e}", file=sys.stderr)
            print(
                f"\n  To skip this stage during development:\n"
                f"    sysforge install --start-from {stage.name} --resume\n"
                f"  or jump directly to a later stage:\n"
                f"    sysforge install --start-from packages\n",
                file=sys.stderr,
            )
            sys.exit(1)

        except RuntimeError as e:
            state.mark_failed(stage.name, str(e))
            state.save()
            print(f"\n[PIPELINE] {stage.name}: FAILED — {e}", file=sys.stderr)
            print(
                f"  State saved. Run with --resume to continue after fixing the issue.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not options.dry_run:
        print("\n[PIPELINE] Pipeline complete.")
