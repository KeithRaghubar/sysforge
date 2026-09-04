# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
import os
import sys
from pathlib import Path
from sysforge import log
_log = log.get_logger("PIPELINE")

_INVOCATION = " ".join(sys.argv)

from sysforge.pipeline.state import PipelineState, resolve_state_dir
from sysforge.pipeline.stages import STAGES
from sysforge.pipeline.stages.base import BootstrapRebootRequired
from sysforge.primitives import change_report
from sysforge.primitives.init_notice import maybe_emit_stage_config_notice
from sysforge.primitives.cache_probe import (
    emit_session_report,
    emit_system_probes,
    reset_session,
)
from sysforge.ui import progress
from sysforge.ui.headers import (
    closing_lines,
    stage_complete_line,
    stage_lines,
    stage_list_lines,
    welcome_lines,
)


def _emit(lines):
    """Emit each line of a multi-line banner via _log.ui."""
    for line in lines:
        _log.ui(line)


def _run_stage_with_change_report(stage, config, state, options):
    """Run a stage, bracketing it with local-DB snapshots when it opts in.

    Re-raises whatever stage.run() raised, unchanged and after rendering, so
    the caller's existing except-clauses keep sole authority over success and
    exit codes. Every part of the reporting path is guarded: a snapshot,
    classify, extras or render failure degrades to a warning and never
    reaches the caller (2.6.1-F24).

    Note: stage.run() is wrapped in ``except BaseException``, so a
    KeyboardInterrupt during a build now also triggers the after-snapshot,
    diff, and summary render before propagating — Ctrl-C takes measurably
    longer to take effect than it used to. This is intended: it lets the
    operator see what landed before the abort. A Ctrl-C landing inside the
    reporting window itself is subordinate to the stage's own error: the
    ``finally`` below re-raises ``stage_error`` over it (2.6.1-F25).
    """
    if not getattr(stage, "reports_changes", False):
        stage.run(config, state, options)
        return

    change_root = getattr(stage, "change_root", None)
    root = Path(change_root) if change_root else None
    unavailable: str | None = None
    before: dict = {}
    try:
        before = change_report.snapshot(root)
    except change_report.SnapshotError as e:
        unavailable = str(e)

    stage_error: BaseException | None = None
    try:
        stage.run(config, state, options)
    except BaseException as e:  # noqa: BLE001 — re-raised below untouched
        stage_error = e

    try:
        rows = []
        if unavailable is None:
            try:
                after = change_report.snapshot(root)
                rows = change_report.diff(before, after)
            except change_report.SnapshotError as e:
                unavailable = str(e)

        extras = []
        if stage_error is None:
            try:
                extras = stage.change_extras(config, state, options)
            # The literal em-dashes below are intentional, not an oversight:
            # every _log.* call is routed through log.downgrade_glyphs()
            # automatically (see log.py), so only strings built for
            # non-log emitters need render.em_dash()/arrow() explicitly.
            except Exception as e:  # noqa: BLE001 — advisory blocks never block
                _log.warn(f"{stage.name}: change summary extras unavailable — {e}")

        outcome = change_report.classify(
            rows, stage_failed=stage_error is not None, unavailable=unavailable
        )
        change_report.render(
            rows,
            stage=stage.name,
            outcome=outcome,
            extras=extras,
            reason=unavailable,
            emit=_log.ui,
        )
    except Exception as e:  # noqa: BLE001 — reporting can never fail a build
        _log.warn(f"{stage.name}: change summary failed to render — {e}")
    finally:
        # The reporting block above guards Exception, but stage.run() is caught
        # with BaseException — so a KeyboardInterrupt landing in the snapshot/
        # render window would otherwise replace the stage's own error and skip
        # the caller's state.mark_failed(). Raising from `finally` replaces the
        # in-flight exception, which is exactly the precedence wanted: a stage
        # failure always beats a reporting failure.
        if stage_error is not None:
            raise stage_error


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
        unified_log_path = log_dir / f"sysforge-run-{stage.name}.log"
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
            # 3.1.0-F5: point a fresh install at the TOML that decides what
            # `run toolchain` / `run kernel` actually build, before they build
            # it. Advisory only — never prompts, blocks, or raises.
            maybe_emit_stage_config_notice(stage.name, state)
            progress.phase(stage.name)
            _run_stage_with_change_report(stage, config, state, options)
            if not stage.stateless:
                try:
                    state.save()
                except PermissionError:
                    _log.warn(
                        f"Cannot write state to {state_dir} — progress will not be checkpointed"
                    )
            _log.info(f"{stage.name}: complete")
        success = True
    except RuntimeError as e:
        _log.fatal(f"{stage.name}: FAILED — {e}")
    finally:
        if unified_log_active:
            log.close_unified_log(success=success, persist=True)
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
    unified_log_path: Path | None = None
    if unified_log_active:
        log_dir = options.log_dir or state_dir
        unified_log_path = log_dir / "sysforge.log"
        log.open_unified_log(unified_log_path, purge=options.purge_log)
        _log.info(f"Unified log: {unified_log_path}")

    _log.info(f"Invocation: {_INVOCATION}")

    _emit(welcome_lines(stage_names))
    _emit(stage_list_lines(
        stage_names,
        {s.name: state.stage_status(s.name) for s in stages},
        next_idx=start_idx,
    ))

    pipeline_success = False
    try:
        # Execute stages
        for global_idx, stage in enumerate(stages, start=1):
            if global_idx - 1 < start_idx:
                continue
            status = state.stage_status(stage.name)

            if status == "done":
                _log.info(f"{stage.name}: already done — skipping")
                continue

            if options.dry_run:
                _emit(stage_lines(global_idx, len(stages),
                                  f"[dry-run] {stage.name}", stage.description))
                continue

            # makepkg can't run as root. The bootstrap phase runs as root on
            # the live ISO and stops at the reboot boundary before any
            # makepkg-bearing stage; a resume that re-enters as root, or a
            # --start-from that jumps straight to a build stage, must fail fast
            # here rather than surfacing makepkg's own rejection deep inside the
            # build (1.2.0-B11, 2.1.0-B4).
            if getattr(stage, "makepkg_bearing", False) and os.geteuid() == 0:
                _log.fatal(
                    f"refusing to run stage {stage.name!r} as root: it builds "
                    f"packages with makepkg, which cannot run as root. Reboot "
                    f"into the installed system and resume as your regular user "
                    f"(e.g. `sudo -u <user> sysforge run pipeline --resume`).",
                    exit_code=2,
                )

            _emit(stage_lines(global_idx, len(stages), stage.name, stage.description))

            # 3.1.0-F5: same advisory on the full-pipeline path — the state
            # object is already in hand here, before the stage executes.
            maybe_emit_stage_config_notice(stage.name, state)

            state.mark_running(stage.name)
            state.save()

            progress.phase(stage.name)
            try:
                _run_stage_with_change_report(stage, config, state, options)
                state.mark_done(stage.name)
                state.save()
                _log.ui(stage_complete_line(stage.name))

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
            _emit(closing_lines())

    finally:
        if unified_log_active:
            log.close_unified_log(success=pipeline_success, persist=options.persist_log)
            if pipeline_success and not options.persist_log:
                _log.info(f"Unified log cleared after successful run: {unified_log_path}")
        if options.cache_report:
            emit_session_report()
