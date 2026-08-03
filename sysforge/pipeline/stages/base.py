# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
    cleansrc: bool = False           # purge each src dir and re-clone (refuses on dirty trees)
    cleansrc_force: bool = False     # like cleansrc but bypasses the dirty/diverged guard
    # Profile / build configuration
    profile_conf: str | None = None  # path to alternate profiles.toml for PKGBUILD patching
    # Kernel stage
    # opt out of interactive kconfig (kernel default is interactive)
    non_interactive: bool = False
    # CLI override for kernel.toml bootloader (systemd-boot|grub|none)
    bootloader: str | None = None
    compiler: str | None = None      # kernel-stage compiler override ("gcc" or "llvm")
    # CLI override for kernel.toml base_config (pkgbuild|running|<path>)
    base_config: str | None = None
    allow_no_fallback: bool = False  # kernel: override the fallback-kernel guarantee (Gate 1)
    skip_boot_audit: bool = False    # kernel: override the pre-install boot-critical audit (Gate 2)
    # kernel: build -headers subpackage; None → kernel.toml (default on)
    build_headers: bool | None = None
    # kernel: build -docs subpackage; None → kernel.toml (default off)
    build_docs: bool | None = None
    # kernel: sample-based FDO step ("record"|"capture"|"use"); LLVM-only
    kernel_fdo: str | None = None
    kernel_propeller: bool = False     # kernel: layer Propeller on the --autofdo cycle
    # Extra makepkg flags (appended after profile makepkg_flags)
    makepkg_flags: list[str] = field(default_factory=list)
    # PGO profdata
    rebuild_profdata: bool = False   # force full 4-pass PGO even if compatible profdata exists
    auto_pgo: bool = False           # bypass PGO confirmation prompts (required for non-TTY PGO)
    # toolchain: consult the Pass-3 build cache to skip unchanged rebuilds
    reuse_built: bool = False
    # LLVM safety pre-flight
    allow_dirty_llvm: bool = False   # bypass refuse-on-dirty/diverged for the toolchain stage
    # Toolchain Gate-1 overrides (mirror kernel's allow_no_fallback/skip_boot_audit)
    # toolchain: override the PKGBUILD pkgver-skew brick (Gate 1)
    allow_version_skew: bool = False
    # toolchain: override the build-space-headroom brick (Gate 1)
    skip_build_space_check: bool = False
    # toolchain: prompt|auto|off for libLLVM soname-bump consumer rebuild (CLI override of
    # toolchain.toml)
    rebuild_soname_consumers: str | None = None
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
    # True for stages that reach a makepkg invocation (toolchain, packages,
    # kernel). makepkg refuses to run as root, so the runner fails fast at the
    # stage boundary if euid == 0. This lives on the stage — not the pipeline
    # verb — because the bootstrap phase (install/hardware/configure/
    # reconfigure) legitimately runs as root on the live ISO and stops at the
    # reboot boundary before any makepkg-bearing stage runs (1.2.0-B11,
    # 2.1.0-B4).
    makepkg_bearing: bool = False
    # True for stages whose work changes installed packages, so the runner
    # brackets stage.run() with local-DB snapshots and renders a change
    # summary. This lives on the stage — not the pipeline verb — for the same
    # reason makepkg_bearing does: it is a property of the work, and standalone
    # `sysforge run <stage>` must get the same treatment as a full pipeline
    # (2.6.1-F24).
    reports_changes: bool = False
    # Root the snapshots are taken against. None = the live root; the install
    # stage resolves a target root (2.6.1-F27). Target-root support is not
    # implemented yet — a stage that sets this today gets
    # pacman.get_installed_facts(root=...) raising NotImplementedError, which
    # surfaces as a permanently-UNKNOWN change summary ("change summary
    # unavailable (...)") until 2.6.1-F27 lands.
    change_root: str | None = None

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

    def change_extras(self, config, state, options):
        """Return stage-specific ExtraBlocks appended below the version rows.

        Called by the runner after run() when reports_changes is set. The
        default is empty so the runner stays generic — it never needs to know
        what (say) a kconfig is. Overrides must not raise; the runner guards
        anyway, but a raising override loses its own block.

        Returns: list[change_report.ExtraBlock]
        """
        return []

    def __repr__(self):
        return f"<Stage {self.name!r}>"
