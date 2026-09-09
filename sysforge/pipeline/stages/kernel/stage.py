# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/stage.py — the ``KernelStage`` entry point.

Sequencing only: the order in which config resolution, source sync, kconfig
authoring, the three gates, the build and the post-install steps run, and the
resolution summary that reports what was chosen and why. Every step delegates
to the sibling module that owns it.
"""
import dataclasses
import contextlib

from sysforge.build_core import make_build_options
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives import kernel_fdo
from sysforge.primitives import sudo_session
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.makepkg_wrapper import AlreadyBuilt
from sysforge.primitives.makepkg_wrapper import install_built_packages
from sysforge.primitives.makepkg_wrapper import run as makepkg_run
from sysforge.primitives.stage_sentinel import sentinel_scope

from sysforge.pipeline.stages.kernel import (
    config,
    constants,
    fdo,
    gates,
    install,
    kconfig,
    source,
)

from sysforge import log

_log = log.get_logger("KERNEL")


# Resolution summary
# ---------------------------------------------------------------------------


def log_resolution_summary(
    *, pkgname, compiler, compiler_origin, cc, cxx, variant, bootloader,
    bootloader_installed, source, kconfig_target, base_config_source,
    hw_kconfig_count, manual_kconfig_count, device_kconfig_count,
    kernel_cfg, skip_boot_audit, build_headers, build_docs,
    fdo_kconfig_count=0, fdo_label="off",
):
    """Emit one labelled block of the resolved kernel-build plan.

    Consolidates what the stage decided (compiler + its origin, inherited
    toolchain variant, bootloader, source, interactive mode, kconfig counts,
    and the boot-safety gate settings) so the operator can eyeball it before a
    long build — and so ``--dry-run`` has a readable summary rather than only
    scattered ``[dry-run] would …`` lines.
    """
    compiler_label = compiler or "profile default"
    boot_note = "" if bootloader_installed else "  (not detected installed!)"
    gates = (
        f"fallback={'required' if kernel_cfg.require_fallback_kernel else 'off'} "
        f"boot_audit={'on' if kernel_cfg.boot_audit else 'off'}"
        f"{' SKIPPED' if skip_boot_audit else ''} "
        f"min_boot_free={kernel_cfg.min_boot_free_mb}MiB "
        f"lsmod_snapshot={'on' if kernel_cfg.capture_lsmod_snapshot else 'off'}"
    )
    _log.ui("Kernel build plan:")
    _log.ui(f"  package:    {pkgname}")
    _log.ui(
        f"  compiler:   {compiler_label} (from {compiler_origin}; "
        f"cc={cc or '-'} cxx={cxx or '-'})"
    )
    _log.ui(f"  variant:    {variant}")
    _log.ui(f"  bootloader: {bootloader}{boot_note}")
    _log.ui(f"  source:     {source}")
    kconfig_counts = (
        f"{hw_kconfig_count} hardware, {device_kconfig_count} device, "
        f"{manual_kconfig_count} manual"
    )
    if fdo_kconfig_count:
        kconfig_counts += f", {fdo_kconfig_count} fdo"
    _log.ui(f"  kconfig:    {kconfig_target} ({kconfig_counts})")
    _log.ui(f"  base cfg:   {base_config_source}")
    _log.ui(f"  fdo:        {fdo_label}")
    _log.ui(
        f"  subpkgs:    headers={'on' if build_headers else 'off'} "
        f"docs={'on' if build_docs else 'off'}"
    )
    _log.ui(f"  gates:      {gates}")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class KernelStage(Stage):
    name = "kernel"
    description = "Build and install a custom kernel"
    depends_on = ["packages"]
    makepkg_bearing = True
    reports_changes = True

    # Captured during run() for the F25 change-summary blocks. Class attributes,
    # not constructor fields: stages/__init__.py instantiates every stage once
    # at import, so this instance is effectively a module-level singleton, and
    # each run() must reset them rather than inherit a prior run's result.
    _kconfig_drift = None    # list[KconfigDrift] | None; None = check did not run
    _kconfig_diff = None     # (prev_release, list[KconfigChange]) | None

    def change_extras(self, sysforge_config, state, options):
        """Kconfig diff + drift blocks (2.6.1-F25). Advisory; never raises."""
        from sysforge.primitives.change_report import ExtraBlock

        blocks = []
        if self._kconfig_diff is not None:
            prev_release, changes = self._kconfig_diff
            blocks.append(ExtraBlock(
                label="Kconfig vs previous build:",
                lines=gates.kconfig_diff_lines(prev_release, changes),
            ))
            # The inline block is capped; the log gets everything.
            for change in changes[gates.KCONFIG_DIFF_CAP:]:
                _log.info(
                    f"kconfig diff (full): {change.option}: "
                    f"{change.old or '(absent)'} → {change.new or '(absent)'} "
                    f"({change.kind})"
                )
        if self._reported_kconfig_merge:
            blocks.append(ExtraBlock(
                label="Kconfig merge drift:",
                lines=gates.kconfig_drift_lines(self._kconfig_drift),
            ))
        return blocks

    # True once a run has reached the point where the merge-drift check either
    # ran or was skipped. Without it, a stage that no-opped early (kernel.toml
    # disabled) would render a "did NOT run" block about a check that was never
    # applicable.
    _reported_kconfig_merge = False

    def run(self, sysforge_config, state, options):
        from sysforge.pipeline.state import get_toolchain_variant, resolve_state_dir
        from sysforge.primitives.build_state import BuildState

        self._kconfig_drift = None
        self._kconfig_diff = None
        self._reported_kconfig_merge = False

        # Parsed once, here, into a frozen dataclass (3.2.0-F6).
        kernel_cfg = config.load()
        if kernel_cfg is None or not kernel_cfg.enabled:
            _log.ui("kernel.toml absent or disabled — stage is a no-op")
            return

        from sysforge.primitives import snapshot
        snapshot.ensure_pre_build_snapshot(sysforge_config, dry_run=options.dry_run)

        # pkgbuild_src_dir is optional in kernel.toml: when unset, fall back to
        # the global [paths] pkgbuild_src_dir. Resolve once and carry it forward
        # so the kernel_cfg-only config.pkgbuild_path() call sites pick up the
        # global value without each needing the full config dict.
        #
        # This used to write back into the config dict in place. KernelConfig is
        # frozen (3.2.0-F6), so it rebinds to a new instance instead: the
        # resolved value still reaches every downstream reader, but nothing
        # holding the original sees it change underneath them. `raw` is updated
        # alongside the field, since the resolvers below read it.
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        eff_src_dir = resolve_pkgbuild_src_dir(sysforge_config, build_cfg=kernel_cfg.raw)
        if eff_src_dir:
            kernel_cfg = dataclasses.replace(
                kernel_cfg,
                pkgbuild_src_dir=eff_src_dir,
                raw={**kernel_cfg.raw, "pkgbuild_src_dir": eff_src_dir},
            )

        # F40: decouple what to pull (upstream_pkgname) from what to build/
        # install as (pkgname); source auto-resolves local → repo → aur when
        # omitted, keyed off the (possibly not-yet-cloned) source dir.
        upstream_pkgname, pkgname = config.resolve_names(kernel_cfg)
        bootloader = config.resolve_bootloader(kernel_cfg, options)
        src_dir = config.srcdir_path(kernel_cfg)
        src_kind = config.resolve_source(kernel_cfg, src_dir)

        # Hoisted once: shared by the drift check below, the compiler nudge
        # downstream, and the BuildState.record() variant stamp later in run.
        variant = get_toolchain_variant(state)
        state_dir, _ = resolve_state_dir(options.state_dir)

        # Sample-based FDO (AutoFDO / Propeller). Resolved up front so the
        # LLVM-only gate fires before any work, the read-only `capture` step can
        # short-circuit (print perf/create_llvm_prof commands, no build), and the
        # `use` step's profile presence is checked fail-fast. `fdo_env` (the
        # CLANG_AUTOFDO_PROFILE/CLANG_PROPELLER_PROFILE_PREFIX make-variables) and
        # `fdo_opt_build_mode` (autofdo_kernel/propeller_kernel → -sysforge coexist
        # rename) thread into the build call below; `fdo_eff_pkgname` is the
        # installed name the post-install gates must verify (the use build is
        # renamed inside makepkg_wrapper).
        fdo_mode, fdo_propeller = fdo.resolve_fdo(options)
        fdo_env = None
        fdo_opt_build_mode = None
        fdo_eff_pkgname = pkgname
        if fdo_mode:
            _fdo_compiler, _fdo_cc, _ = config.resolve_compiler(kernel_cfg, options, state)
            fdo.gate_fdo_llvm(fdo_mode, fdo_propeller, _fdo_compiler, _fdo_cc)
            if fdo_mode == "capture":
                fdo.run_fdo_capture(pkgname, fdo_propeller, options.dry_run)
                return
            if fdo_mode == "record":
                # F26: the instrumented profiling kernel must not overwrite the
                # production kernel. Install it under a distinct sysforge-owned
                # coexist name (its own /boot entry, bootloader fallback), applied
                # via the same rename_pkgbase_to seam as the use-build. The coexist
                # name is itself the ownership gate — a reinstall only ever
                # replaces a prior sysforge profiling kernel.
                fdo_eff_pkgname = kernel_fdo.record_pkgname(pkgname)
                _log.ui(
                    f"AutoFDO{' + Propeller' if fdo_propeller else ''} record-build: "
                    f"profiling kernel installs as {fdo_eff_pkgname} "
                    f"(coexists with {pkgname}; boot into it to collect samples)"
                )
            if fdo_mode == "use":
                _fdo_store = kernel_fdo.resolve_store(pkgname, propeller=fdo_propeller)
                # Clean pre-build abort if the record→capture profile is missing.
                kernel_fdo.require_profile(_fdo_store, propeller=fdo_propeller)
                fdo_env = kernel_fdo.use_env(_fdo_store, propeller=fdo_propeller)
                fdo_opt_build_mode = kernel_fdo.build_mode(propeller=fdo_propeller)
                if not pkgname.endswith("-sysforge"):
                    fdo_eff_pkgname = f"{pkgname}-sysforge"
                _log.ui(
                    f"AutoFDO{' + Propeller' if fdo_propeller else ''} use-build: "
                    f"consuming {_fdo_store} → {fdo_eff_pkgname} "
                    f"(coexists with {pkgname})"
                )

        # A1: per-kernel toolchain drift. update.py's drift sweep skips
        # stage-owned packages (the kernel is one), so this stage owns the
        # drift signal for its own package. Same shape as update.py:1180-1205.
        recorded_variant = (BuildState(state_dir).get(pkgname) or {}).get("toolchain_variant")
        if recorded_variant and variant != "system" and recorded_variant != variant:
            _log.warn(
                f"Installed {pkgname} was built under toolchain variant "
                f"{recorded_variant!r}; active variant is {variant!r}. "
                "Rebuilding will switch toolchains."
            )

        # A2: surface bootloader mismatch before the build runs, so users on a
        # grub-only system who left bootloader defaulting to systemd-boot get
        # a single early warning instead of a post-install bootctl failure.
        if bootloader != "none":
            installed_bootloaders = source.probe_installed_bootloader()
            if installed_bootloaders and bootloader not in installed_bootloaders:
                _log.warn(
                    f"kernel.toml bootloader = {bootloader!r} but installation "
                    f"not detected (found: {sorted(installed_bootloaders) or 'none'}). "
                    "Post-install step will likely fail — pass "
                    "--bootloader=<installed> or update kernel.toml."
                )

        # Interactive kconfig is the kernel-stage default — flipped off by
        # --non-interactive (or `interactive = false` in kernel.toml). When
        # interactive=True is passed into BuildOptions, makepkg_wrapper skips
        # the plan's non-interactive rewrite so the user's PKGBUILD kconfig target
        # (typically `make nconfig`) runs as written, and the makepkg
        # subprocess inherits the parent's stdout/stderr so ncurses-driven
        # kconfig UIs render on the controlling TTY.
        # B8: interactive requires config ∧ ¬--non-interactive ∧ TTY — the
        # same three-way resolution the stage's sibling prompts (repo-pkgname
        # collision, diverged-source confirm) already use. Without the TTY leg
        # the stage promised an nconfig review that a piped/captured run could
        # never render, then silently EOF'd through it.
        from sysforge.primitives.prompt import is_interactive as _tty
        cfg_interactive = kernel_cfg.interactive
        flag_non_interactive = bool(getattr(options, "non_interactive", False))
        interactive = cfg_interactive and not flag_non_interactive and _tty()
        if cfg_interactive and not flag_non_interactive and not interactive:
            _log.warn(
                "interactive kconfig review requested (kernel.toml "
                "interactive = true) but no TTY is attached — running "
                "unattended; the config review will NOT run. Pass "
                "--non-interactive to make this explicit."
            )

        # F37: configured kconfig_targets sequence — resolved/validated here
        # (config-load time) so a bad list (unknown target, two UI targets, a
        # prompting target requested non-interactively) raises ValueError and
        # aborts before any makepkg invocation, rather than failing mid-build.
        # None when the key is unset — zero behavior change.
        kconfig_targets = kconfig.resolve_kconfig_targets(kernel_cfg, interactive=interactive)

        # lsmod snapshot — captured before build for localmodconfig
        # reproducibility. Opt-out via `capture_lsmod_snapshot = false` (Gate 1
        # warns that localmodconfig strips drivers for inactive hardware).
        if kernel_cfg.capture_lsmod_snapshot:
            kconfig.capture_lsmod_snapshot(state_dir, options.dry_run)

        # Pre-sync the kernel PKGBUILD tree through SourceSyncScheduler. This
        # is the only allowed code path for refreshing sources (CLAUDE.md #3).
        # Running it here (vs. relying on makepkg_wrapper's internal sync)
        # makes --cleansrc work even when --no-update is also set. Sync runs
        # BEFORE the PKGBUILD path is required (F40) so a missing tree is
        # bootstrapped by the scheduler's clone-if-missing path instead of
        # aborting with "clone it first".
        synced = source.presync_kernel_source(
            src_dir, options, state_dir, source=src_kind,
        )
        pkgbuild = config.pkgbuild_path(kernel_cfg)

        # A3: static-parse the freshly-synced PKGBUILD and confirm its pkgbase
        # matches the *pre-rename* name of the tree — upstream_pkgname when
        # tracking an upstream, else the local pkgname. Catches a mis-cloned/
        # typo'd tree before makepkg --install fails late after a multi-hour
        # build; the local rename is a patch applied later in makepkg_wrapper.
        source.validate_pkgname_matches_pkgbuild(pkgbuild, upstream_pkgname or pkgname)

        # A4: warn + confirm if the *installed* kernel name shadows a pacman repo
        # package (would overwrite the official package on install). For an FDO
        # use-build that is the -sysforge name, which never collides; for
        # record/no-FDO it is the stock pkgname.
        source.check_pkgname_repo_collision(fdo_eff_pkgname, options)

        # Compiler resolution: CLI > kernel.toml > pipeline state from toolchain.
        compiler, cc, cxx = config.resolve_compiler(kernel_cfg, options, state)
        if getattr(options, "compiler", None):
            compiler_origin = "--compiler"
        elif kernel_cfg.compiler:
            compiler_origin = "kernel.toml"
        elif cc:
            compiler_origin = "toolchain pipeline state"
        else:
            compiler_origin = "profile default"
        if compiler:
            _log.info(f"Kernel compiler override: {compiler}  (cc={cc}  cxx={cxx})")
        elif cc:
            _log.info(f"Toolchain override from pipeline: cc={cc} cxx={cxx or '-'}")
        else:
            _log.info("No kernel compiler override — profile defaults apply")

        # 3.3: surface the inherited toolchain variant when the user hasn't
        # set `compiler` explicitly. The pipeline-state fallback already
        # routes the right cc/cxx through; this just makes the inheritance
        # visible so the operator can persist it in kernel.toml (and survive
        # a future toolchain-stage disable that clears [stages.toolchain.result]).
        if compiler is None and variant == "pgo_llvm":
            _log.warn(
                "Active toolchain variant: pgo_llvm — kernel will inherit PGO "
                "clang via pipeline-state fallback. Set `compiler = \"llvm\"` "
                "in kernel.toml (or pass --compiler=llvm) to make this explicit "
                "and survive future toolchain-stage disable."
            )
        elif compiler is None and variant == "stock_llvm":
            _log.info(
                "Active toolchain variant: stock_llvm — kernel will inherit "
                "clang via pipeline-state fallback. Set `compiler = \"llvm\"` "
                "in kernel.toml to make this explicit."
            )

        # 4a: configured-vs-installed toolchain mismatch. The variant nudge above
        # reflects what the toolchain *stage* registered in pipeline state; this
        # reflects on-disk reality via collect_llvm_state (provenance, not a
        # health probe). It fires even when the toolchain stage never populated
        # pipeline state — e.g. toolchain.toml configures PGO LLVM but a stock
        # repo llvm is installed, so the kernel won't be built with the PGO
        # toolchain the user thinks is active. Surfaced standalone via
        # `sysforge doctor --toolchain`.
        from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch
        for finding in detect_toolchain_config_mismatch(sysforge_config):
            _log.warn(f"{finding.message} — {finding.remediation}")

        # Base .config seeding — written before the fragment so the build order
        # is base → fragment overlay. "pkgbuild" (default) is a no-op; "running"
        # or a path seeds sysforge.base.config for the PKGBUILD to copy to .config.
        base_config_source = kconfig.write_base_config(kernel_cfg, options.dry_run, options)

        # kconfig fragment — requires hardware_profile.toml (hardware stage).
        # Written after the source sync so a --cleansrc re-clone doesn't wipe
        # it, and after compiler resolution so the fragment header can carry
        # the toolchain provenance (C2). Still before the build, which reads
        # sysforge.config in the PKGBUILD's prepare().
        # FDO (record/use) needs CONFIG_AUTOFDO_CLANG (+ CONFIG_PROPELLER_CLANG)
        # in the fragment — and therefore the fragment itself. Refuse the
        # contradictory "FDO requested but kconfig_merge = false" combo rather
        # than silently building an unprofiled kernel.
        fdo_extra_kconfig = (
            kernel_fdo.fdo_kconfig(propeller=fdo_propeller) if fdo_mode else None
        )
        if fdo_extra_kconfig and not kernel_cfg.kconfig_merge:
            raise RuntimeError(
                "[KERNEL] --autofdo needs the kconfig fragment to set "
                f"{', '.join(fdo_extra_kconfig)}, but kernel.toml kconfig_merge = "
                "false disables it. Set kconfig_merge = true to use kernel FDO."
            )

        provenance = f"toolchain variant: {variant}  cc: {cc or '-'}"
        (
            fragment_path, hw_kconfig_count, manual_kconfig_count,
            device_kconfig_count, fdo_kconfig_count,
        ) = kconfig.write_kconfig_fragment(
            kernel_cfg, sysforge_config, options.dry_run, provenance=provenance,
            state_dir=state_dir, extra_kconfig=fdo_extra_kconfig,
        )
        kconfig.write_hotplug_fragment(kernel_cfg, options, options.dry_run)

        if kconfig_targets:
            kconfig_target = f"{' → '.join(kconfig_targets)} (configured)"
        elif interactive:
            kconfig_target = "make nconfig (user-supplied)"
        else:
            kconfig_target = "make olddefconfig (patched)"
        _log.info(f"Kernel kconfig target: {kconfig_target}")

        # C3: standalone interactive runs require the operator to drive the
        # PKGBUILD's kconfig UI. Pipeline / --non-interactive / dry-run paths
        # don't, so only nudge for a real interactive run.
        if interactive and not options.dry_run:
            _log.info(
                "Running interactively (the PKGBUILD's `make nconfig`/etc. runs "
                "as written); pass --non-interactive for unattended builds."
            )

        skip_boot_audit = bool(getattr(options, "skip_boot_audit", False))
        build_headers, build_docs = config.resolve_subpackages(kernel_cfg, options)

        if fdo_mode:
            fdo_label = (
                f"autofdo={fdo_mode}{' +propeller' if fdo_propeller else ''}"
            )
            if fdo_eff_pkgname != pkgname:
                fdo_label += f" → {fdo_eff_pkgname}"
        else:
            fdo_label = "off"

        # B1: consolidated resolution summary — one labelled block instead of
        # decisions scattered across the log. Useful before a multi-hour build
        # and the readable core of a --dry-run preview.
        log_resolution_summary(
            pkgname=pkgname,
            compiler=compiler,
            compiler_origin=compiler_origin,
            cc=cc,
            cxx=cxx,
            variant=variant,
            bootloader=bootloader,
            bootloader_installed=(
                bootloader == "none" or bootloader in source.probe_installed_bootloader()
            ),
            source=src_kind,
            kconfig_target=kconfig_target,
            base_config_source=base_config_source,
            hw_kconfig_count=hw_kconfig_count,
            manual_kconfig_count=manual_kconfig_count,
            device_kconfig_count=device_kconfig_count,
            kernel_cfg=kernel_cfg,
            skip_boot_audit=skip_boot_audit,
            build_headers=build_headers,
            build_docs=build_docs,
            fdo_kconfig_count=fdo_kconfig_count,
            fdo_label=fdo_label,
        )

        # FDO feasibility advisory (record only — a `use` build already has its
        # profile). Surfaces this host's branch-sampling capability before the
        # operator commits to building + booting a profiling kernel: AMD BRS
        # (Zen 3+) is experimental for AutoFDO, and pre-Zen3 AMD has no path.
        if fdo_mode == "record":
            _sampling = kernel_fdo.detect_branch_sampling()
            if _sampling.supported:
                _log.warn(
                    "AutoFDO profiling kernel: after install, reboot into it and "
                    f"run `sysforge run kernel --autofdo=capture`. {_sampling.note}"
                )
            else:
                _log.warn(
                    "AutoFDO profiling kernel requested, but branch sampling is "
                    f"unsupported on this CPU — {_sampling.note} The collected "
                    "profile may be unusable."
                )

        # Gate 1 — cheap preflight (fallback-kernel guarantee, /boot space,
        # root-topology capture, advisory warnings). Hard-fails *before* the
        # build so a missing fallback / full /boot aborts with nothing spent.
        topology = gates.gate1_preflight(
            kernel_cfg, options, pkgname, dry_run=options.dry_run,
        )

        # Advisory lock around the whole build → audit → install window so two
        # concurrent `sysforge run kernel` runs sharing this state dir can't
        # clobber ~/builds/<pkgbase> (the second nconfig/makepkg would step on
        # the first's .config). Scoped to state_dir (not /var/tmp) so test runs
        # with a tmp state dir are isolated. Skipped in dry-run — nothing is
        # built, and the lock file would be a side effect. Shared primitive with
        # the toolchain stage's PGO lock (see primitives/build_lock.py).
        _lock = (
            contextlib.nullcontext()
            if options.dry_run
            else build_lock(state_dir / "kernel-build.lock", label="kernel", noun="build")
        )
        # B5: the kernel build runs as long as the toolchain's PGO sequence and
        # installs the same way (install_built_packages → sudo pacman -U from
        # this process), so credentials authenticated here would be long expired
        # by the time the install runs and the prompt would go stale. Warm them
        # once while the operator is still present, then keep them warm for the
        # whole build → audit → install window. Authenticating *before* starting
        # the daemon is load-bearing: the refresh inherits stdio, so with no
        # cached timestamp it would prompt from a background thread.
        if not options.dry_run:
            sudo_session.authenticate()
        _sudo = sudo_session.keepalive(tag="KERNEL", enabled=not options.dry_run)

        with _lock, _sudo:
            # Build WITHOUT installing, then audit the resolved .config, then
            # install — so a boot-critical kconfig drop (Gate 2) aborts before
            # any mutation and the running kernel stays bootable. The build
            # itself mutates nothing, so it runs outside the install sentinel;
            # a Gate 2 abort therefore leaves no sentinel behind.
            # B7: True when the install below installs a previously built
            # (stale) package rather than this run's fresh build — the
            # install-failure guidance keys on it to break the re-run loop.
            already_built = False
            if options.dry_run:
                _log.ui(f"[dry-run] would build {pkgname} (no install) from {pkgbuild}")
            else:
                # B6: the pre-nconfig pause now lives *inside* the patched
                # PKGBUILD's prepare() (kconfig_plan),
                # right after the base seed + fragment merge assemble the final
                # .config and immediately before `make nconfig`. A stage-level
                # pause here would fire before makepkg runs those in-prepare()
                # merges — the exact "confirm before the config is assembled"
                # defeat B6 describes — so it is deliberately not emitted here.
                _log.info(f"Building kernel (no install): {pkgname} from {pkgbuild}")

                def _kernel_build_options(extra_flags=None):
                    # One builder for both the normal build and the B5
                    # AlreadyBuilt "rebuild to review" retry (which adds -f) —
                    # so the retry can never drift from the real build's options.
                    return make_build_options(
                        "kernel", options,
                        extra_flags=extra_flags,
                        log_dir=options.log_dir,
                        profile_conf=(
                            getattr(options, "profile_conf", None)
                            or sysforge_config.get("profile_conf")
                        ),
                        update=(not options.no_update) and not synced,
                        interactive=interactive,
                        cc_override=cc,
                        cxx_override=cxx,
                        source=src_kind,
                        toolchain_variant=variant if variant != "system" else None,
                        kernel_build_headers=build_headers,
                        kernel_build_docs=build_docs,
                        kconfig_targets=kconfig_targets,
                        # FDO use-build: profile path make-variables (extra_env →
                        # `make`) + the optimization build_mode that earns the
                        # -sysforge coexist rename. Both None for record/no-FDO.
                        extra_env=fdo_env,
                        optimization_build_mode=fdo_opt_build_mode,
                        # Coexist pkgbase rename (patch_pkgbase_rename). For an
                        # --autofdo=record build the target is the distinct
                        # profiling name (F26), so the instrumented kernel never
                        # overwrites the production one. Otherwise it is the F40
                        # local-rename: patch the cloned upstream's pkgbase to the
                        # local pkgname so the build installs alongside the
                        # official package. None when neither applies (names match
                        # or pure-local) → no patch, upstream name.
                        rename_pkgbase_to=(
                            fdo_eff_pkgname
                            if fdo_mode == "record"
                            else (
                                pkgname
                                if upstream_pkgname and pkgname != upstream_pkgname
                                else None
                            )
                        ),
                    )

                try:
                    makepkg_run(pkgbuild, options=_kernel_build_options())
                except AlreadyBuilt:
                    # B5: makepkg exit 13 skipped the build — and with it the
                    # in-prepare() kconfig review an interactive run promised.
                    # Ask the operator (install as-built / rebuild with -f to
                    # review / abort); unattended runs keep the proceed path.
                    if source.resolve_already_built_action(options, interactive) == "rebuild":
                        makepkg_run(
                            pkgbuild,
                            options=_kernel_build_options(extra_flags=["-f"]),
                        )
                    else:
                        already_built = True

                # Gate 2 — resolved-.config audit (raises on brick, pre-install).
                gates.gate2_audit(
                    pkgbuild.parent, topology,
                    skip_boot_audit=skip_boot_audit, state_dir=state_dir,
                )

                # Advisory: warn if any option sysforge merged didn't survive
                # the build's kconfig resolution (nconfig toggle or olddefconfig
                # dep drop). Never raises; no-op when no fragment was written.
                self._kconfig_drift = gates.gate2_kconfig_drift(
                    pkgbuild.parent, fragment_path
                )
                self._reported_kconfig_merge = fragment_path is not None

                # Archive this build's resolved .config so the *next* run can
                # diff against it, and capture the previous one for this run's
                # summary. Best-effort: never raises, never blocks the install.
                self._kconfig_diff = gates.record_and_diff_kconfig(
                    state_dir, pkgname, pkgbuild.parent
                )

            # B4: acquire credentials *before* the sentinel scope. A sudo
            # prompt that times out makes sudo exit non-zero without ever
            # execing pacman — nothing installed, no file touched — but inside
            # the scope that surfaces as a RuntimeError, and by contract any
            # exception leaves the sentinel in place. The operator would then
            # have to run a pointless mkinitcpio to clear a sentinel guarding a
            # mutation that never began. Probing out here keeps the
            # classification narrow: only a *pre-install* auth failure is a
            # clean abort; a pacman -U that actually ran and failed still leaves
            # the sentinel behind, as it must.
            if not options.dry_run and not sudo_session.authenticate():
                raise RuntimeError(
                    "[KERNEL] sudo authentication failed or timed out before "
                    "the install step — nothing was installed and the system is "
                    "unchanged. Re-run when you can answer the prompt: "
                    "sysforge run kernel"
                )

            # Install + boot wiring are the mutation window — wrap them in the
            # sentinel so an interrupted install / mkinitcpio / bootloader regen
            # blocks the next run with a recovery command (the failure mode that
            # leaves the system kernel-installed but initramfs-missing →
            # unbootable).
            with sentinel_scope(
                options.state_dir,
                "kernel",
                recovery_cmd=constants.kernel_recovery_command(),
                retry_cmd="sysforge run kernel",
                pkgname=fdo_eff_pkgname,
                bootloader=bootloader,
                compiler=compiler or "default",
            ):
                if options.dry_run:
                    _log.ui(f"[dry-run] would install {fdo_eff_pkgname} and wire it into boot")
                else:
                    try:
                        install_built_packages(
                            pkgbuild.parent, noconfirm=not interactive)
                    except RuntimeError as e:
                        if already_built:
                            # B7: AlreadyBuilt → install-as-built → install
                            # failure is the state that reproduces itself on
                            # every re-run (the stale package keeps
                            # short-circuiting the build). Point at the exit.
                            raise RuntimeError(
                                f"[KERNEL] {e} — installing a previously built "
                                "(stale) package failed; break the loop with a "
                                "fresh build: bump pkgver/pkgrel, or remove the "
                                "stale package(s) from PKGDEST and re-run."
                            ) from e
                        raise

                install.run_mkinitcpio(options.dry_run)
                install.update_bootloader(bootloader, options.dry_run)

                # Gate 3 — post-install boot-readiness (raises on brick). Inside
                # the sentinel so an unbootable result blocks the next run for
                # recovery. Keyed on the *installed* name: an FDO use-build is
                # renamed to <pkgname>-sysforge, so /boot/vmlinuz-<that> is what
                # must exist.
                if not options.dry_run:
                    gates.gate3_verify(pkgbuild.parent, fdo_eff_pkgname, bootloader)

        _log.info(f"Kernel stage complete: {fdo_eff_pkgname}")
