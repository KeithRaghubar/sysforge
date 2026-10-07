# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
run_cmd.py — ``sysforge run <stage>`` verbs.

Thin shims onto the pipeline runner: each verb resolves config + RunOptions and
hands a single stage (or the full pipeline) to ``run_pipeline`` /
``run_stage_standalone``. Dispatched through the Verb framework; the argparse
surface is each verb's ``add_parser``, grouped by ``RUN_GROUP``.

These verbs do NOT install a verb-level sentinel: the pipeline framework (and
the stages themselves, e.g. toolchain via ``sentinel_scope``) own their sentinel
coverage. Wrapping the verb in another ``sentinel_scope`` would race with the
inner stage's sentinel against the same ``stage_in_progress.toml``.
"""
import argparse
import os
from pathlib import Path

from sysforge.primitives.config import load_config
from sysforge.build.makepkg_wrapper import expand_makepkg_flags
from sysforge.verbs import ExecResult, PreCheckResult, Verb, VerbGroup
from sysforge.verbs.helpers import PACKAGES_FILE_HELP, load_config_with_overrides


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

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("pipeline",
            help="Run the full install pipeline (stages 1–8).")
        p.add_argument("--resume", action="store_true",
            help="Resume from the last checkpoint.")
        p.add_argument("--start-from", metavar="STAGE", dest="start_from",
            help="Start from this stage, marking all prior stages as skipped. "
                 "Useful on a live system: --start-from reconfigure")
        p.add_argument("--force-retry", action="store_true", dest="force_retry",
            help="Retry all failed packages without prompting.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show what would run without executing anything.")
        p.add_argument("--packages", metavar="FILE",
            help=PACKAGES_FILE_HELP)
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.add_argument("--no-unified-log", action="store_true", dest="no_unified_log",
            help="Disable the unified log file.")
        p.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
            help="Disable per-package log files.")
        p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
            help="Directory for log files.")
        p.add_argument("--purge-log", action="store_true", dest="purge_log",
            help="Truncate the unified log before this run.")
        p.add_argument("--persist-log", action="store_true", dest="persist_log",
            help="Keep log files after successful completion.")
        p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
            help="Path to a profiles.toml to use instead of the default.")
        p.add_argument("--cache-report", action="store_true", dest="cache_report",
            help="Print a structured cache summary after the pipeline completes.")
        p.add_argument("--abi-check", action="store_true", dest="abi_check",
            help="Run a post-build ABI compatibility check on built shared libraries.")
        p.add_argument("--no-update", action="store_true", dest="no_update",
            help="Skip git pull --rebase before each build.")
        p.set_defaults(verb_cls=cls)
        return p

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

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("hardware",
            help="Re-run hardware detection and refresh hardware_profile.toml.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show what would be written without writing.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.set_defaults(verb_cls=cls)
        return p

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.hardware import HardwareStage
        config = load_config() or {}
        options = RunOptions(
            dry_run=args.dry_run,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            no_unified_log=False,
            no_pkg_logs=True,
        )
        run_stage_standalone(HardwareStage(), config, options)
        return ExecResult()


class RunReconfigureVerb(_RunVerbBase):
    name = "run-reconfigure"

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("reconfigure",
            help="Pre-build checkpoint: review configs, disk, network, GPG, build preview.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Run all steps non-interactively without writing any changes.")
        p.add_argument("--packages", metavar="FILE",
            help="Path to packages.toml (used by disk and preview steps).")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.set_defaults(verb_cls=cls)
        return p

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.reconfigure import ReconfigureStage
        config = load_config_with_overrides(args)
        options = RunOptions(
            dry_run=args.dry_run,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            no_unified_log=False,
            no_pkg_logs=True,
        )
        run_stage_standalone(ReconfigureStage(), config, options)
        return ExecResult()


class RunToolchainVerb(_RunVerbBase):
    name = "run-toolchain"
    makepkg_bearing = True

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("toolchain",
            help="Build and install the LLVM/GCC toolchain from toolchain.toml.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show what would run without executing anything.")
        p.add_argument("--no-update", action="store_true", dest="no_update",
            help="Skip git pull --rebase before each build.")
        p.add_argument("--makepkg", "-m", metavar="FLAGS",
            help="Additional makepkg flags appended to each build. "
                 "Example: -m '-f' to force rebuild of already-built packages. "
                 "Unlike build/update, -m is required here — bare makepkg flags "
                 "are not forwarded implicitly on run subcommands. "
                 "Install flags (-i/--install) are ignored; the toolchain controls "
                 "which passes install to the system.")
        p.add_argument("--persist-log", action="store_true", dest="persist_log",
            help="Keep log files after successful completion.")
        p.add_argument("--cache-report", action="store_true", dest="cache_report",
            help="Print a structured cache summary after the run.")
        p.add_argument("--abi-check", action="store_true", dest="abi_check",
            help="Run a post-build ABI compatibility check on built shared libraries.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.add_argument("--rebuild-profdata", action="store_true", dest="rebuild_profdata",
            help="Force a full 4-pass PGO build even if compatible profdata already exists.")
        p.add_argument("--reuse-built", action="store_true", dest="reuse_built",
            help="Skip rebuilding a Pass-3 PGO package whose inputs are unchanged "
                 "(PKGBUILD, source commit, profdata, flags, compiler, dep versions) "
                 "and whose built artifact is still on disk. Lets a rerun after a "
                 "late-package failure reuse the already-optimized llvm/llvm-libs "
                 "instead of rebuilding them. Opt-in; overrides toolchain.toml "
                 "reuse_unchanged. Any input change or missing artifact rebuilds.")
        p.add_argument("--auto-pgo", action="store_true", dest="auto_pgo",
            help="Bypass the PGO confirmation prompts (profdata reuse, staging/pgo_store "
                 "purge, 4-pass start, suspicious profdata size). Required for non-interactive "
                 "PGO runs; without it, a non-TTY invocation aborts the PGO sub-flow because PGO "
                 "is fragile and silent mis-optimisation is the failure mode.")
        p.add_argument("--allow-dirty-llvm", action="store_true", dest="allow_dirty_llvm",
            help="Bypass the LLVM safety pre-flight refusal on dirty or diverged "
                 "trees. PGO profdata version mismatches cannot be bypassed. "
                 "Note: this only suppresses the refusal — it does not modify the "
                 "tree. Use --cleansrc-force to actually overwrite the local "
                 "trees with upstream.")
        p.add_argument("--allow-version-skew", action="store_true",
            dest="allow_version_skew",
            help="Override the pre-build Gate-1 abort when the in-tree LLVM "
                 "PKGBUILDs disagree on pkgver across the lockstep suite "
                 "(llvm/llvm-libs/clang/lld/compiler-rt/polly/openmp). By default a "
                 "skew aborts before building because dependency resolution will "
                 "fail; this builds anyway. spirv-llvm-translator and lib32-* are "
                 "never part of the skew check.")
        p.add_argument("--skip-build-space-check", action="store_true",
            dest="skip_build_space_check",
            help="Override the pre-build Gate-1 abort when a filesystem hosting the "
                 "staging dirs / pgo_store / build output lacks min_build_free_gb "
                 "free (default 40 GiB). Dangerous — the multi-hour LLVM build may "
                 "fail partway with no space left.")
        p.add_argument("--rebuild-soname-consumers",
            dest="rebuild_soname_consumers", choices=("prompt", "auto", "off"),
            default=None,
            help="What to do when the built libLLVM changes its soname and would "
                 "break installed consumers (mesa, etc.): 'prompt' (default) warns "
                 "and asks before building; 'auto' approves and rebuilds them after "
                 "install; 'off' builds the toolchain but leaves consumers for you "
                 "to rebuild. Overrides toolchain.toml rebuild_soname_consumers.")
        p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
            help="Purge each toolchain package's src dir and re-clone before "
                 "building. Refuses (per package) if the existing clone has "
                 "uncommitted changes, ahead-of-upstream commits, or no upstream.")
        p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
            help="Like --cleansrc but bypasses the dirty/diverged guard and "
                 "overwrites every local toolchain tree unconditionally. Use when "
                 "the upstream rewrote history (e.g. Arch packaging repos "
                 "force-push every release) and local commits have no value.")
        p.set_defaults(verb_cls=cls)
        return p

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

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("packages",
            help="Build and install non-kernel packages from packages.toml.")
        p.add_argument("--packages", metavar="FILE",
            help=PACKAGES_FILE_HELP)
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show what would run without executing anything.")
        p.add_argument("--force-retry", action="store_true", dest="force_retry",
            help="Retry all failed packages without prompting.")
        p.add_argument("--no-update", action="store_true", dest="no_update",
            help="Skip git pull --rebase before each build.")
        p.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
            help="Disable per-package log files.")
        p.add_argument("--persist-log", action="store_true", dest="persist_log",
            help="Keep log files after successful completion.")
        p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
            help="Directory for log files.")
        p.add_argument("--cache-report", action="store_true", dest="cache_report",
            help="Print a structured cache summary after the run.")
        p.add_argument("--abi-check", action="store_true", dest="abi_check",
            help="Run a post-build ABI compatibility check on built shared libraries.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
            help="Path to a profiles.toml to use instead of the default.")
        p.set_defaults(verb_cls=cls)
        return p

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

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("kernel",
            help="Build and install the custom kernel configured in kernel.toml.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show what would run without executing anything.")
        p.add_argument("--no-update", action="store_true", dest="no_update",
            help="Skip git pull --rebase before each build.")
        p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
            help="Purge the kernel src dir and re-clone before building. "
                 "Refuses if the existing clone has uncommitted changes, "
                 "ahead-of-upstream commits, or no upstream.")
        p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
            help="Like --cleansrc but bypasses the dirty/diverged guard and "
                 "overwrites the local tree unconditionally.")
        p.add_argument("--non-interactive", action="store_true", dest="non_interactive",
            help="Disable interactive kconfig (default is interactive — the PKGBUILD's "
                 "`make nconfig`/`menuconfig`/etc. runs as written). With this flag, "
                 "interactive targets are patched to `make olddefconfig` for unattended runs.")
        p.add_argument("--compiler", choices=["gcc", "llvm"], dest="compiler",
            help="Kernel-stage compiler override (gcc or llvm). Independent of the "
                 "global toolchain stage — lets you keep gcc system-wide but build "
                 "the kernel with LLVM (or vice versa). Resolution order: this flag > "
                 "kernel.toml compiler > toolchain-stage pipeline state.")
        p.add_argument(
            "--bootloader", choices=["systemd-boot", "grub", "none"], dest="bootloader",
            help="Override kernel.toml bootloader for this invocation "
                 "(systemd-boot is the default).")
        p.add_argument("--base-config", metavar="SRC", dest="base_config",
            help="Override kernel.toml base_config for this run: the starting .config "
                 "before the sysforge.config fragment overlay. One of 'pkgbuild' "
                 "(the PKGBUILD's own base), 'running' (the running kernel's config), "
                 "or a path to a .config file. Resolution order: this flag > "
                 "kernel.toml base_config > 'pkgbuild' default.")
        p.add_argument("--base-config-merge", choices=["replace", "overlay"],
            dest="base_config_merge",
            help="Override kernel.toml base_config_merge for this run: 'replace' "
                 "(default) copies the base over the PKGBUILD's .config; 'overlay' "
                 "merges it on top, so symbols the base does not mention (e.g. new "
                 "in this kernel) keep the PKGBUILD's values instead of kconfig "
                 "defaults.")
        p.add_argument("--headers",
            action=argparse.BooleanOptionalAction, default=None, dest="build_headers",
            help="Build the kernel -headers subpackage (default: on, per kernel.toml "
                 "build_headers). --no-headers drops it from the build; DKMS modules "
                 "(nvidia-open-dkms, virtualbox, …) and any out-of-tree module then "
                 "cannot rebuild and will not load on reboot.")
        p.add_argument("--docs",
            action=argparse.BooleanOptionalAction, default=None, dest="build_docs",
            help="Build the kernel -docs subpackage (default: off, per kernel.toml "
                 "build_docs). Pass --docs to build the kernel HTML/man documentation.")
        p.add_argument("--keep-hotplug-drivers",
            action=argparse.BooleanOptionalAction, default=None, dest="keep_hotplug_drivers",
            help="Re-enable hotplug driver classes (USB, USB4/Thunderbolt, MMC/SD, "
                 "hot-plug PCI/CardBus, hot-plug HID) as modules AFTER config "
                 "minimization, so devices plugged in later still work (default: off, "
                 "per kernel.toml keep_hotplug_drivers). Only meaningful when a "
                 "minimizing kconfig_targets sequence (e.g. localmodconfig) is set.")
        p.add_argument("--autofdo", choices=("record", "capture", "use"),
            dest="kernel_fdo",
            help="Sample-based kernel optimization (AutoFDO; LLVM toolchain only), "
                 "in steps spanning reboots. 'record' builds+installs the profiling "
                 "kernel (e.g. linux-sysforge-profiling); 'capture', run on that "
                 "booted kernel, prints the perf and converter commands (no build); "
                 "'use' builds the optimized kernel (e.g. linux-sysforge-fdo) beside "
                 "the plain one. Add --propeller for the optional round 2. kernel.toml "
                 "[fdo] mode makes a plain run a 'use' build.")
        p.add_argument("--propeller", action="store_true", dest="kernel_propeller",
            help="Round 2: layer Propeller (basic-block layout) on round 1's AutoFDO "
                 "profile, which it pins. Requires --autofdo; run record, capture and "
                 "use again with --propeller. The use build installs as e.g. "
                 "linux-sysforge-propeller. Recommended over BOLT for the kernel.")
        p.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
            help="Disable per-package log files.")
        p.add_argument("--persist-log", action="store_true", dest="persist_log",
            help="Keep log files after successful completion.")
        p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
            help="Directory for log files.")
        p.add_argument("--cache-report", action="store_true", dest="cache_report",
            help="Print a structured cache summary after the run.")
        p.add_argument("--abi-check", action="store_true", dest="abi_check",
            help="Run a post-build ABI compatibility check on built shared libraries.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
            help="Path to a profiles.toml to use instead of the default.")
        p.add_argument("--allow-no-fallback", action="store_true",
            dest="allow_no_fallback",
            help="Override the boot-safety guarantee that a fallback kernel "
                 "(stock linux/linux-lts with a boot image) exists before "
                 "installing a custom kernel. Without a fallback, a broken custom "
                 "kernel leaves no recovery path short of a live USB.")
        p.add_argument("--skip-boot-audit", action="store_true",
            dest="skip_boot_audit",
            help="Override the pre-install boot-critical kconfig audit (Gate 2). "
                 "By default a built kernel that drops the root filesystem / "
                 "storage controller / core boot infra aborts before install; this "
                 "flag installs it anyway. Dangerous — can leave the system unbootable.")
        p.set_defaults(verb_cls=cls)
        return p

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
            base_config_merge=getattr(args, "base_config_merge", None),
            allow_no_fallback=getattr(args, "allow_no_fallback", False),
            skip_boot_audit=getattr(args, "skip_boot_audit", False),
            build_headers=getattr(args, "build_headers", None),
            build_docs=getattr(args, "build_docs", None),
            kernel_fdo=getattr(args, "kernel_fdo", None),
            kernel_propeller=getattr(args, "kernel_propeller", False),
        )
        run_stage_standalone(KernelStage(), config, options)
        return ExecResult()


#: `sysforge run`: pipeline / hardware / reconfigure / toolchain / packages /
#: kernel (stage required).
RUN_GROUP = VerbGroup(
    name="run",
    help="Execute a pipeline stage "
         "(pipeline, hardware, reconfigure, toolchain, packages, kernel).",
    dest="run_stage",
    metavar="STAGE",
    members=(RunPipelineVerb, RunHardwareVerb, RunReconfigureVerb,
             RunToolchainVerb, RunPackagesVerb, RunKernelVerb),
)
