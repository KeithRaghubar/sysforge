# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
run_cmd.py — ``sysforge run <stage>`` verbs.

Thin shims onto the pipeline runner: each verb resolves config + RunOptions and
hands a single stage (or the full pipeline) to ``run_pipeline`` /
``run_stage_standalone``. Dispatched through the Verb framework; the argparse
surface lives in ``cli._add_run_parser``.

These verbs do NOT install a verb-level sentinel: the pipeline framework (and
the stages themselves, e.g. toolchain via ``sentinel_scope``) own their sentinel
coverage. Wrapping the verb in another ``sentinel_scope`` would race with the
inner stage's sentinel against the same ``stage_in_progress.toml``.
"""
import os
from pathlib import Path

from sysforge.primitives.config import load_config
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags
from sysforge.verbs import ExecResult, PreCheckResult, Verb
from sysforge.verbs.helpers import load_config_with_overrides


class _RunVerbBase(Verb):
    """Common scaffolding for ``sysforge run <stage>`` verbs."""

    requires_sentinel = False
    # True for standalone build verbs (packages, kernel, toolchain) that reach
    # a makepkg invocation and are always run as the regular user — fail fast
    # at entry instead of surfacing makepkg's own rejection deep inside a build
    # stage (1.2.0-B11). The full-pipeline verb is deliberately NOT
    # makepkg_bearing: it spans the root-run bootstrap phase, so the runner
    # enforces the no-root rule per stage instead (see RunPipelineVerb,
    # Stage.makepkg_bearing, 2.1.0-B4).
    makepkg_bearing = False

    def pre_check(self, args) -> PreCheckResult:
        if self.makepkg_bearing and os.geteuid() == 0:
            return PreCheckResult(
                blocker=(
                    "refusing to run as root: this command builds packages "
                    "with makepkg, which cannot run as root. Re-run as your "
                    "regular user (e.g. `sudo -u <user> sysforge …`); sysforge "
                    "escalates with sudo only where needed."
                ),
                exit_code=2,
            )
        return PreCheckResult()


class RunPipelineVerb(_RunVerbBase):
    name = "run-pipeline"
    # NOT makepkg_bearing at the verb level: the pipeline spans the bootstrap
    # phase (install/hardware/configure/reconfigure), which legitimately runs
    # as root on the live ISO. The runner enforces the no-root rule per stage
    # against Stage.makepkg_bearing, so the build stages still fail fast at euid
    # 0 while the bootstrap phase is allowed through (2.1.0-B4).
    makepkg_bearing = False

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_pipeline
        from sysforge.pipeline.stages.base import RunOptions
        config = load_config_with_overrides(args)
        options = RunOptions(
            resume=args.resume,
            start_from=args.start_from,
            force_retry=args.force_retry,
            dry_run=args.dry_run,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            no_unified_log=args.no_unified_log,
            no_pkg_logs=args.no_pkg_logs,
            log_dir=Path(args.log_dir) if args.log_dir else None,
            purge_log=args.purge_log,
            persist_log=args.persist_log,
            cache_report=args.cache_report,
            abi_check=args.abi_check,
            no_update=args.no_update,
        )
        run_pipeline(config, options)
        return ExecResult()


class RunHardwareVerb(_RunVerbBase):
    name = "run-hardware"

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.hardware import HardwareStage
        config = load_config() or {}
        options = RunOptions(
            dry_run=args.dry_run,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            no_unified_log=True,
            no_pkg_logs=True,
        )
        run_stage_standalone(HardwareStage(), config, options)
        return ExecResult()


class RunReconfigureVerb(_RunVerbBase):
    name = "run-reconfigure"

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.reconfigure import ReconfigureStage
        config = load_config_with_overrides(args)
        options = RunOptions(
            dry_run=args.dry_run,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            no_unified_log=True,
            no_pkg_logs=True,
        )
        run_stage_standalone(ReconfigureStage(), config, options)
        return ExecResult()


class RunToolchainVerb(_RunVerbBase):
    name = "run-toolchain"
    makepkg_bearing = True

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.toolchain import ToolchainStage
        config = load_config() or {}
        options = RunOptions(
            dry_run=args.dry_run,
            no_update=args.no_update,
            cleansrc=getattr(args, "cleansrc", False),
            cleansrc_force=getattr(args, "cleansrc_force", False),
            cache_report=args.cache_report,
            abi_check=args.abi_check,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            persist_log=args.persist_log,
            makepkg_flags=expand_makepkg_flags(args.makepkg) if args.makepkg else [],
            rebuild_profdata=args.rebuild_profdata,
            auto_pgo=args.auto_pgo,
            allow_dirty_llvm=args.allow_dirty_llvm,
            allow_version_skew=getattr(args, "allow_version_skew", False),
            skip_build_space_check=getattr(args, "skip_build_space_check", False),
            rebuild_soname_consumers=getattr(args, "rebuild_soname_consumers", None),
            reuse_built=getattr(args, "reuse_built", False),
        )
        run_stage_standalone(ToolchainStage(), config, options)
        return ExecResult()


class RunPackagesVerb(_RunVerbBase):
    name = "run-packages"
    makepkg_bearing = True

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.packages import PackagesStage
        config = load_config_with_overrides(args)
        options = RunOptions(
            dry_run=args.dry_run,
            force_retry=args.force_retry,
            no_update=args.no_update,
            no_pkg_logs=args.no_pkg_logs,
            persist_log=args.persist_log,
            log_dir=Path(args.log_dir) if args.log_dir else None,
            cache_report=args.cache_report,
            abi_check=args.abi_check,
            state_dir=Path(args.state_dir) if args.state_dir else None,
        )
        run_stage_standalone(PackagesStage(), config, options)
        return ExecResult()


class RunKernelVerb(_RunVerbBase):
    name = "run-kernel"
    makepkg_bearing = True

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.kernel import KernelStage
        config = load_config() or {}
        options = RunOptions(
            dry_run=args.dry_run,
            no_update=args.no_update,
            cleansrc=getattr(args, "cleansrc", False),
            cleansrc_force=getattr(args, "cleansrc_force", False),
            no_pkg_logs=args.no_pkg_logs,
            persist_log=args.persist_log,
            log_dir=Path(args.log_dir) if args.log_dir else None,
            cache_report=args.cache_report,
            abi_check=args.abi_check,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            profile_conf=getattr(args, "profile_conf", None),
            non_interactive=getattr(args, "non_interactive", False),
            bootloader=getattr(args, "bootloader", None),
            compiler=getattr(args, "compiler", None),
            base_config=getattr(args, "base_config", None),
            allow_no_fallback=getattr(args, "allow_no_fallback", False),
            skip_boot_audit=getattr(args, "skip_boot_audit", False),
            build_headers=getattr(args, "build_headers", None),
            build_docs=getattr(args, "build_docs", None),
            kernel_fdo=getattr(args, "kernel_fdo", None),
            kernel_propeller=getattr(args, "kernel_propeller", False),
        )
        run_stage_standalone(KernelStage(), config, options)
        return ExecResult()
