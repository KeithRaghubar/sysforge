"""
stages/base.py — Stage base class

Each pipeline stage is a Stage subclass. The runner iterates the STAGES list,
reads .name for logging and .depends_on for ordering validation, and calls
.run() for execution.

RunOptions is the decoupled options struct passed to every stage — not tied
to argparse so stages can be called directly in tests or from other code.
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunOptions:
    """
    Options that control pipeline execution behaviour.
    Constructed by the CLI from parsed argparse args.
    """
    resume: bool = False
    start_from: str | None = None    # start from this stage, mark prior as skipped_to
    force_retry: bool = False        # retry failed packages without prompt
    dry_run: bool = False            # log what would run, don't execute
    state_dir: Path | None = None    # overrides env var and default
    # Logging
    no_unified_log: bool = False     # disable unified log file
    no_pkg_logs: bool = False        # disable per-package log files
    log_dir: Path | None = None      # override log file directory (default: state_dir)
    purge_log: bool = False          # truncate unified log before run
    persist_log: bool = False        # don't clear logs on successful completion
    # Cache reporting
    cache_report: bool = False       # print structured cache summary at end of run
    # ABI checking
    abi_check: bool = False          # run post-build ABI compatibility check on .so files
    # Source updates
    no_update: bool = False          # skip git pull --rebase before each build
    # Extra makepkg flags (appended after profile makepkg_flags)
    makepkg_flags: list[str] = field(default_factory=list)
    # PGO profdata
    rebuild_profdata: bool = False   # force full 3-pass PGO even if compatible profdata exists
    auto_pgo: bool = False           # bypass PGO confirmation prompts (required for non-TTY PGO)
    # LLVM safety pre-flight
    allow_dirty_llvm: bool = False   # bypass refuse-on-dirty/diverged for the toolchain stage
    # Execution context
    standalone: bool = False         # True when running a single stage outside the pipeline


class BootstrapRebootRequired(Exception):
    """
    Raised by a stage when the bootstrap phases are complete and the machine
    must be rebooted before pipeline execution can continue.

    The runner handles this as a clean stop (not a failure), prints reboot
    instructions, and preserves state so --resume works after rebooting.
    """


class Stage:
    """
    Base class for all pipeline stages.

    Subclasses must set class attributes `name` and `description`, and
    implement `run()`.

    `depends_on` lists stage names that must be done before this stage runs.
    For the linear SysForge pipeline this is always the previous stage name,
    but the runner validates it so future non-linear stages are safe to add.
    """
    name: str = ""
    description: str = ""
    depends_on: list[str] = []
    stateless: bool = False  # True for stages that don't write pipeline state

    def run(self, config, state, options):
        """
        Execute this stage.

        Args:
            config:  fully loaded SysForge config dict
            state:   PipelineState instance
            options: RunOptions instance

        Raises RuntimeError on unrecoverable failure.
        Stages should call state.mark_package_* methods themselves for
        intra-stage checkpointing (packages stage only).
        """
        raise NotImplementedError(f"Stage {self.name!r} has not been implemented")

    def __repr__(self):
        return f"<Stage {self.name!r}>"
