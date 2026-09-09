# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/stage.py — the ``ToolchainStage`` entry point.

Deliberately thin. This module holds the stage's *sequencing* — the order the
gates, passes, verification and registration run in, and the branch between the
GCC (register-only) and LLVM (build) paths — and delegates every step to the
sibling module that owns it.

Read this file to learn what the toolchain stage does; read the siblings to
learn how each step works.
"""
from pathlib import Path
import contextlib

from sysforge.pipeline.stages.base import Stage
from sysforge.primitives import build_fingerprint
from sysforge.primitives import toolchain_safety
from sysforge.primitives.makepkg_pgo import resolve_pgo_store
from sysforge.primitives.stage_sentinel import sentinel_scope

from sysforge.pipeline.stages.toolchain import (
    bolt,
    config,
    gates,
    identity,
    pgo,
    pkgbuilds,
    profdata,
    verify,
)

from sysforge.primitives import pacman
from sysforge.primitives import config as prim_config
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class ToolchainStage(Stage):
    name = "toolchain"
    description = "LLVM/GCC toolchain build"
    depends_on = ["reconfigure"]
    makepkg_bearing = True
    reports_changes = True

    # "Before" side of the F26 identity block. A class attribute, not a
    # constructor field: stages/__init__.py instantiates every stage once at
    # import, so this instance is effectively a module-level singleton.
    _identity_before: identity.ToolchainIdentity | None = None

    def change_extras(self, sysforge_config, state, options):
        """Toolchain identity + flag delta (2.6.1-F26). Advisory; never raises."""
        from sysforge.primitives.change_report import ExtraBlock

        if self._identity_before is None:
            return []
        lines = identity.toolchain_identity_lines(
            self._identity_before, identity.probe_toolchain_identity(state, options)
        )
        return [ExtraBlock(label="Toolchain:", lines=lines)] if lines else []

    def run(self, sysforge_config, state, options):
        self._identity_before = identity.probe_toolchain_identity(state, options)
        # Parsed once, here, into a frozen dataclass (3.2.0-F6): defaults
        # applied in one place, so no downstream read can invent its own.
        tcfg = config.load()
        if tcfg is None or not tcfg.enabled:
            _log.ui(
                "toolchain.toml absent or disabled — stage is a no-op"
            )
            # Clear any prior cc/cxx/ld result so downstream stages
            # (packages, kernel) don't keep using stale toolchain
            # overrides from a previous enabled run.
            if state.get_stage_result("toolchain"):
                state.set_stage_result("toolchain", {})
                try:
                    state.save()
                except PermissionError:
                    _log.warn(
                        "Cannot write state — stale toolchain result will persist",
                    )
            return

        compiler = tcfg.compiler
        pgo_enabled = tcfg.pgo_enabled
        staging1, staging, staging3 = tcfg.staging1, tcfg.staging, tcfg.staging3
        # resolve_pgo_store owns its own precedence and takes the whole table.
        pgo_store = resolve_pgo_store(tcfg.raw)

        # GCC path: never build from source. Register system gcc paths so
        # downstream stages (packages, kernel) pick them up; stock gcc-libs
        # from pacman/base-devel provides the runtime. This is intentionally
        # the only behaviour for compiler="gcc" — sysforge does not own a
        # GCC build path (no meaningful performance gain, error-prone, and
        # base-devel already covers it).
        if compiler == "gcc":
            cc, cxx, ld = identity.compiler_paths("gcc")
            _log.ui(
                f"Compiler: gcc — registering system paths (no build): cc={cc}  cxx={cxx}",
            )
            state.set_stage_result(
                "toolchain", {"cc": cc, "cxx": cxx, "variant": "gcc"}
            )
            try:
                state.save()
            except PermissionError:
                _log.warn(
                    "Cannot write state — toolchain results will not be checkpointed",
                )
            identity.propagate_default_toolchain(compiler, options)
            return

        # skip_build (LLVM only): register clang paths without building.
        # Variant reflects on-disk clang provenance (pgo_llvm if a profdata
        # + version sidecar pair is present, else stock_llvm) so downstream
        # conditionals see the actual installed compiler, not just the
        # stage's current action.
        if tcfg.skip_build:
            cc, cxx, ld = identity.compiler_paths(compiler)
            variant = profdata.resolve_skip_build_variant(pgo_store)
            _log.ui(
                f"skip_build=true — skipping build, registering {compiler} "
                f"(variant={variant}): cc={cc}  cxx={cxx}",
            )
            result = {"cc": cc, "cxx": cxx, "variant": variant}
            if ld is not None:
                result["ld"] = ld
            state.set_stage_result("toolchain", result)
            try:
                state.save()
            except PermissionError:
                _log.warn(
                    "Cannot write state — toolchain results will not be checkpointed",
                )
            identity.propagate_default_toolchain(compiler, options)
            return

        # Repo-install path (LLVM, PGO off): when packages.toml [build]
        # repo_mode is "pacman", honor the user's package-sourcing preference
        # and pull the stock LLVM suite from the repos instead of compiling it.
        # PGO is deliberately excluded — a profiled toolchain is the point of
        # enabling PGO and has no repo artifact, so PGO always builds from
        # source regardless of repo_mode.
        if not pgo_enabled and (
            config.resolve_packages_repo_mode(sysforge_config) == prim_config.REPO_MODE_PACMAN
        ):
            pgo_pkgs, non_pgo_pkgs, lib32_pkgs = (
            list(tcfg.pgo_pkgs), list(tcfg.non_pgo_pkgs), list(tcfg.lib32_pkgs)
        )
            suite = pgo_pkgs + non_pgo_pkgs + lib32_pkgs
            cc, cxx, ld = identity.compiler_paths(compiler)
            _log.ui(
                f"repo_mode=pacman, PGO off — installing {len(suite)} LLVM "
                f"package(s) from repo (no build): {' '.join(suite)}",
            )
            if options.dry_run:
                _log.ui(
                    f"[dry-run] would install from repo: {' '.join(suite)}"
                )
            else:
                # Install is the mutation window — wrap in the sentinel so an
                # interrupted/failed install blocks the next run with a recovery
                # command.
                with sentinel_scope(
                    options.state_dir,
                    "toolchain",
                    recovery_cmd="sudo pacman -S " + " ".join(suite),
                    retry_cmd="sysforge run toolchain",
                    compiler=compiler,
                    pgo=pgo_enabled,
                ):
                    pacman.install_repo_pkgs(suite)
            result = {"cc": cc, "cxx": cxx, "variant": "stock_llvm"}
            if ld is not None:
                result["ld"] = ld
            state.set_stage_result("toolchain", result)
            try:
                state.save()
            except PermissionError:
                _log.warn(
                    "Cannot write state — toolchain results will not be checkpointed",
                )
            identity.propagate_default_toolchain(compiler, options)
            return

        pgo_pkgs, non_pgo_pkgs, lib32_pkgs = (
            list(tcfg.pgo_pkgs), list(tcfg.non_pgo_pkgs), list(tcfg.lib32_pkgs)
        )

        all_names = pgo_pkgs + non_pgo_pkgs + lib32_pkgs
        total = len(all_names)
        if pgo_enabled:
            parts = [f"{len(pgo_pkgs)} pgo", f"{len(non_pgo_pkgs)} non-pgo"]
        else:
            parts = [f"{len(pgo_pkgs) + len(non_pgo_pkgs)} packages"]
        if lib32_pkgs:
            parts.append(f"{len(lib32_pkgs)} lib32")
        pkg_summary = f"{total} total  ({' / '.join(parts)})"
        _log.ui(
            f"Compiler: {compiler}  |  PGO: {pgo_enabled}  |  Packages: {pkg_summary}",
        )

        # Resolve PKGBUILDs for all packages. Sync through SourceSyncScheduler
        # unless --no-update was passed; --cleansrc[/-force] forces the sync
        # path even with --no-update so an explicit purge isn't silently
        # skipped.
        pkgbuild_map = pkgbuilds.resolve_all_pkgbuilds(
            all_names, sysforge_config,
            update=not options.no_update,
            cleansrc=getattr(options, "cleansrc", False),
            cleansrc_force=getattr(options, "cleansrc_force", False),
        )

        # LLVM safety pre-flight: refuse-by-default on dirty/diverged trees.
        # Strict mode is rule-driven from sysforge.toml [safety]; the CLI
        # --allow-dirty-llvm flag bypasses dirty/diverged blockers (a stale
        # PGO profdata mismatch is never suppressible).
        pkgbuilds.run_llvm_preflight(all_names, sysforge_config, options)

        pgo_map = {n: pkgbuild_map[n] for n in pgo_pkgs}
        non_pgo_map = {n: pkgbuild_map[n] for n in non_pgo_pkgs}
        lib32_map = {n: pkgbuild_map[n] for n in lib32_pkgs}

        # Training-corpus extras (e.g. mesa): compiled by the instrumented
        # stage1 clang in Pass 3 purely to enrich clang.profdata with non-LLVM
        # (graphics/C-heavy) codegen; never installed. Resolved through the same
        # scheduler-synced PKGBUILD path as the toolchain packages. Best-effort
        # — a resolution miss degrades to an LLVM-only corpus with a warning,
        # never blocks the toolchain build. PGO path only (a non-PGO single-pass
        # build generates no profraw, so the corpus is meaningless there).
        corpus_map: dict[str, Path] = {}
        corpus_extras = list(tcfg.training_corpus) if pgo_enabled else []
        if corpus_extras:
            try:
                corpus_resolved = pkgbuilds.resolve_all_pkgbuilds(
                    corpus_extras, sysforge_config,
                    update=not options.no_update,
                    cleansrc=getattr(options, "cleansrc", False),
                    cleansrc_force=getattr(options, "cleansrc_force", False),
                )
                corpus_map = {n: corpus_resolved[n] for n in corpus_extras}
                _log.ui(
                    f"[PGO] Training-corpus extras: {', '.join(corpus_extras)} "
                    "(compiled in Pass 3 for profile enrichment; not installed)",
                )
            except RuntimeError as e:
                _log.warn(
                    f"[PGO] Could not resolve training-corpus extras "
                    f"{corpus_extras}: {e} — proceeding with LLVM-only corpus",
                )

        role_map = (
            (
                {n: "pgo" for n in pgo_pkgs}
                | {n: "non-pgo" for n in non_pgo_pkgs}
                | {n: "lib32" for n in lib32_pkgs}
            )
            if pgo_enabled
            else {n: "lib32" for n in lib32_pkgs}
        )
        pkgbuilds.show_resolution_table(pkgbuild_map, role_map=role_map or None)

        # Capture the prior-good install set BEFORE any mutation so it can be
        # restored offline if Gate 3 (post-install verify) fails. Cheap pacman
        # cache lookup; safe in dry-run (read-only).
        snapshot = gates.snapshot_suite({**pgo_map, **non_pgo_map, **lib32_map})

        # B1: consolidated resolution summary — one labelled block plus the
        # readable core of a --dry-run preview (kernel parity).
        variant = "pgo_llvm" if pgo_enabled else "stock_llvm"
        gates.log_toolchain_resolution_summary(
            compiler=compiler, pgo=pgo_enabled, variant=variant,
            pgo_pkgs=pgo_pkgs, non_pgo_pkgs=non_pgo_pkgs, lib32_pkgs=lib32_pkgs,
            staging1=staging1, staging=staging, staging3=staging3, pgo_store=pgo_store,
            tcfg=tcfg, options=options, snapshot=snapshot,
        )

        # Gate 1 — cheap pre-build preflight. Hard-fails (overridable) on
        # definite-failure conditions BEFORE any build time is spent; dry-run
        # downgrades bricks to warnings. Runs for both PGO and non-PGO.
        gates.gate1_preflight(
            lib32_pkgs, staging1, staging, pgo_store,
            pkgbuild_map, options, tcfg, snapshot=snapshot,
        )

        # Pre-build soname gate: if the about-to-be-built libLLVM bumps the
        # soname, warn + list the installed consumers that would break and
        # (per gates.rebuild_soname_consumers mode) require approval up front. The
        # captured consumers are rebuilt after Gate 3 so there is no
        # post-install shock. Returns [] when nothing changes / dry-run / off.
        soname_consumers = gates.gate_soname_consumers(
            pkgbuild_map, all_names, options, tcfg,
        )

        # Prompt for confirmation (interactive only)
        try:
            import sys as _sys

            if _sys.stdin.isatty() and not options.dry_run:
                pkgbuilds.confirm_or_abort(options.state_dir)
        except RuntimeError:
            raise

        # Advisory lock around the whole build → audit → snapshot → install
        # window, mirroring the kernel stage's kernel-build.lock. Guards the
        # PGO /var/tmp staging dirs + pgo_store cache (and the non-PGO build area) so
        # two concurrent `sysforge run toolchain` runs can't clobber each
        # other. Skipped in dry-run (the lock file would be a side effect).
        _lock = (
            contextlib.nullcontext()
            if options.dry_run
            else pgo.pgo_lock(pgo.pgo_lock_path(staging1))
        )
        with _lock:
            # Build WITHOUT installing (both paths). The build mutates nothing,
            # so it runs OUTSIDE the sentinel; a build-pass failure leaves no
            # sentinel behind (matches kernel).
            if pgo_enabled:
                # Opt-in input-fingerprint reuse (Pass 4): CLI --reuse-built >
                # toolchain.toml reuse_unchanged > off. config_digest folds the
                # flag-relevant config (profiles/rules) + the toolchain settings
                # (e.g. [llvm] targets, which drive the LLVM_TARGETS_TO_BUILD
                # cmake patch the upstream-PKGBUILD hash can't see) so a config
                # edit between runs invalidates the cache.
                reuse_built = bool(getattr(options, "reuse_built", False)) or bool(
                    tcfg.reuse_unchanged
                )
                config_digest = build_fingerprint.hash_obj({
                    "profiles": sysforge_config.get("profiles"),
                    "rules": sysforge_config.get("rules"),
                    "toolchain": tcfg.raw,
                })
                built_map, cc, cxx, ld, variant = pgo.build_llvm_pgo_inner(
                    pgo_map, non_pgo_map, lib32_map,
                    staging1, staging, staging3, pgo_store, options,
                    config_digest=config_digest,
                    reuse_built=reuse_built,
                    corpus_map=corpus_map,
                    # BOLT Pass 5 (opt-in) needs the shipped clang/libLLVM linked
                    # with -Wl,--emit-relocs so llvm-bolt can rewrite them.
                    bolt_relocs=tcfg.bolt.enabled,
                )
            else:
                built_map, cc, cxx, ld, variant = pgo.build_llvm_single(
                    pgo_map, non_pgo_map, lib32_map, options
                )

            # Gate 2 — pre-install ABI-hazard audit (both paths), OUTSIDE the
            # sentinel: an unhealable brick aborts here leaving nothing installed
            # and no sentinel, keeping the live toolchain intact. A healable
            # std:: re-export drift returns the libLLVM consumers to rebuild
            # after Gate 3 (same machinery as a soname bump).
            abi_consumers = gates.gate2_audit(
                built_map, all_names, options, tcfg, dry_run=options.dry_run,
            )

            # Install + post-install verify are the mutation window — wrap them
            # in the sentinel so an interrupted/failed install blocks the next
            # run with a recovery command. CleanExitRequested → RuntimeError
            # translation (with retry_cmd + recovery_cmd) happens inside
            # sentinel_scope. See primitives/stage_sentinel.py.
            with sentinel_scope(
                options.state_dir,
                "toolchain",
                recovery_cmd=gates.snapshot_recovery_cmd(snapshot),
                retry_cmd="sysforge run toolchain",
                compiler=compiler,
                pgo=pgo_enabled,
            ) as sentinel:
                if options.dry_run:
                    _log.ui("[dry-run] would install built toolchain and verify")
                else:
                    label = "PGO optimize" if pgo_enabled else "LLVM build (single pass, no PGO)"
                    profdata.pgo_install(label, built_map, options.dry_run)

                # Gate 3 — post-install verify (H). On failure, auto-restore the
                # prior-good toolchain from the snapshot (offline pacman -U):
                #   restore OK   → live /usr is whole again → clear sentinel + raise.
                #   restore FAIL → keep sentinel (recovery_cmd = snapshot restore).
                #
                # expected_targets is the *actually resolved* LLVM_TARGETS_TO_BUILD
                # the build patched in (resolve_or_detect_llvm_targets — the same
                # value makepkg_wrapper used), NOT just toolchain.toml [llvm]
                # targets. On an autodetect host that key is unset, so the old
                # `tcfg[...]` form resolved to None and skipped check #3 entirely —
                # the gap that let a target-reduced libLLVM install unverified.
                from sysforge.pipeline.state import resolve_state_dir
                from sysforge.primitives.llvm_targets import (
                    resolve_or_detect_llvm_targets,
                )
                # Resolve the state dir the same way the build's patcher did
                # (makepkg_wrapper._maybe_patch_llvm_targets → resolve_state_dir),
                # so Gate 3 reads the same hardware_profile.toml. options.state_dir
                # may be None (env/default fallback); resolve_state_dir handles it.
                _gate3_state_dir, _ = resolve_state_dir(options.state_dir)
                hw_profile = _gate3_state_dir / "hardware_profile.toml"
                expected_targets = resolve_or_detect_llvm_targets(
                    config.TOOLCHAIN_PATH, hw_profile,
                )
                if not options.dry_run:
                    issues = list(
                        verify.verify_llvm_install(expected_targets=expected_targets)
                    )
                    # Graphics-consumer sufficiency vs the NOW-INSTALLED libLLVM.
                    # An *unhealable* dropped backend an installed mesa consumer
                    # imports is brick-class — folded into `issues` so the same
                    # snapshot auto-restore fires while rollback is still armed
                    # (inside the sentinel). *Healable* std:: re-export misses are
                    # EXPECTED here (mesa is not rebuilt until after Gate 3): they
                    # must NOT trip rollback, or we would revert the very libLLVM
                    # the post-Gate-3 consumer rebuild is about to make coherent.
                    for f in toolchain_safety.check_installed_consumer_symbols():
                        if f.healable:
                            _log.ui(
                                "Gate 3: std:: re-export drift (healable by the "
                                f"queued consumer rebuild): {f.message}"
                            )
                        else:
                            issues.append(f.message)
                    if issues:
                        evidence_path = (
                            verify.dump_stage_dynsym_evidence(staging3, state.path.parent)
                            if pgo_enabled
                            else None
                        )
                        _log.warn("Gate 3: post-install LLVM verification failed:")
                        for issue in issues:
                            _log.warn(f"  - {issue}")
                        if evidence_path is not None:
                            _log.warn(f"Diagnostic evidence written to: {evidence_path}")
                        _log.warn("Attempting automatic rollback to the prior-good toolchain…")
                        if gates.rollback_to_snapshot(snapshot):
                            # Live /usr restored — the system is whole, so clear
                            # the sentinel and raise (nothing to recover at next run).
                            sentinel.clear()
                            raise RuntimeError(
                                "[TOOLCHAIN] Built toolchain failed Gate-3 verification; "
                                "the prior toolchain was restored from the pacman cache. "
                                "Investigate the build (see findings above) before retrying."
                            )
                        # Restore failed / snapshot incomplete — leave the
                        # sentinel in place with the snapshot recovery command.
                        raise RuntimeError(
                            "[TOOLCHAIN] Built toolchain failed Gate-3 verification AND "
                            "automatic rollback could not complete. The live toolchain may "
                            "be inconsistent. Stage sentinel left in place — restore with: "
                            f"{gates.snapshot_recovery_cmd(snapshot)}"
                        )
                    # Verify passed — safe to wipe the stage2/stage3 prefixes
                    # (PGO only). stage3 held the optimized libLLVM that the
                    # non-pgo sub-pass linked against; it is no longer needed.
                    if pgo_enabled:
                        profdata.remove_staging(staging)
                        profdata.remove_staging(staging3)
                        # Pass 5 — BOLT the verified PGO clang (opt-in, gated on
                        # [bolt] enabled). 4a builds the BOLT tools (not in the
                        # Arch repos), 4b rewrites clang. Best-effort and
                        # smoke-tested before it replaces /usr/bin/clang; runs
                        # inside the sentinel so a mishap stays covered by the
                        # snapshot rollback.
                        bolt.run_bolt(tcfg, sysforge_config, options, variant)

        # Toolchain is installed and Gate-3-verified. Rebuild the libLLVM
        # consumers so the live system is left coherent: the pre-build soname
        # gate's set (re-link the new soname) merged with Gate 2's same-soname
        # std:: re-export drift set (re-link std:: to libstdc++). OUTSIDE the
        # sentinel — a consumer rebuild failure must not roll back the intended
        # toolchain bump; it surfaces as an actionable error instead.
        rebuild_consumers = sorted(set(soname_consumers) | set(abi_consumers))
        if rebuild_consumers and not options.dry_run:
            gates.rebuild_soname_consumers(rebuild_consumers, sysforge_config, options, state)

        # Write compiler paths + variant to pipeline state for downstream
        # stages. ``variant`` is the canonical signal consumers read via
        # ``state.get_toolchain_variant`` — do not derive it from the cc path.
        result = {"cc": cc, "cxx": cxx, "variant": variant}
        if ld is not None:
            result["ld"] = ld
        state.set_stage_result("toolchain", result)
        try:
            state.save()
        except PermissionError:
            _log.warn(
                "Cannot write state — toolchain results will not be checkpointed",
            )

        # Installed + Gate-3-verified: sync the profile default to the configured
        # compiler (outside the sentinel, on success only — a failed build above
        # returns/raises before here, so the default never flips to an
        # uninstalled llvm).
        identity.propagate_default_toolchain(compiler, options)

        _log.ui(
            f"Toolchain stage complete. cc={cc}  cxx={cxx}"
            + (f"  ld={ld}" if ld else ""),
        )
