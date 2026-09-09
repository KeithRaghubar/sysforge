# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/gates.py — refuse-before-damage checks around the LLVM build.

Mirrors the kernel stage's shape: Gate 1 is a cheap preflight that hard-fails
before any build time is spent, Gate 2 audits the built artifacts before they
are installed. Between them sits soname-consumer handling — when the new
libLLVM changes soname, everything linked against the old one must be rebuilt
or it breaks on next use.

The GCC path skips all of it: it installs nothing and builds nothing, so there
is nothing to gate. Snapshot/rollback is here rather than in ``pgo.py`` because
its purpose is the same as the gates' — the suite is installed as a set, and a
half-installed LLVM is worse than no new LLVM.
"""
from pathlib import Path

from sysforge.primitives import toolchain_safety
from sysforge.primitives.stage_sentinel import sentinel_scope
from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE

from sysforge.pipeline.stages.toolchain import config, profdata, verify

from sysforge.primitives import prompt
from sysforge.primitives import pacman
from sysforge.primitives import config as prim_config
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Build-safety gates (LLVM path only — the GCC path is register-only and skips
# all gates). Mirrors the kernel stage: a cheap pre-build preflight (Gate 1,
# hard-fails before any build time is spent), a pre-install artifact audit
# (Gate 2, outside the sentinel so an abort leaves nothing installed), and a
# post-install verify (Gate 3, inside the sentinel). The pure facts live in
# primitives/toolchain_safety.py; the abort/warn policy lives here.
# See DESIGN.md §Toolchain stage boot-safety.
# ---------------------------------------------------------------------------

# Lockstep-suite members for the install-time snapshot. The snapshot also
# captures whatever the build produced (by built-package name), but seeding it
# with the installed suite guarantees the prior-good libLLVM/clang/lld set is
# captured even for members the current build doesn't touch.
_SNAPSHOT_SUITE: tuple[str, ...] = LLVM_LOCKSTEP_SUITE


# Gate-1 smoke findings that a plain repo install can actually fix, mapped to
# the package providing the binary. Deliberately excludes ``smoke:clang_broken``
# — that is a *mismatched* lockstep suite from an aborted run, whose remediation
# is reinstalling the whole suite under the user's eye, not a blind `-S clang`.
_BOOTSTRAP_PKG_FOR: dict[str, str] = {
    "smoke:clang_missing": "clang",
    "smoke:lld_missing": "lld",
}


def install_bootstrap_compilers(findings, options, tcfg: "config.ToolchainConfig"):
    """Install any missing Pass-1 bootstrap compiler, then re-probe.

    The LLVM path is a 4-pass PGO bootstrap: Pass 1 must be compiled by an
    already-installed clang, and lld links every pass. Both are needed *before*
    ``build_core.batch_install_makedeps`` runs, and neither appears in the llvm
    PKGBUILD's makedepends (upstream builds with gcc) — so on a clean machine
    nothing in the pipeline would ever install them and the stage bricked with
    a manual `pacman -S clang` hint.

    Returns the findings the caller should still act on: ``[]`` (or whatever a
    re-probe surfaces) after a successful install, the originals untouched when
    nothing is installable or in dry-run.
    """
    pkgs = sorted({
        _BOOTSTRAP_PKG_FOR[f.check_id]
        for f in findings if f.check_id in _BOOTSTRAP_PKG_FOR
    })
    if not pkgs:
        return findings

    if options.dry_run:
        _log.ui(f"[dry-run] would install bootstrap compiler(s): {' '.join(pkgs)}")
        return findings

    _log.ui(
        f"Gate 1: installing {len(pkgs)} missing bootstrap compiler "
        f"package(s) from repo: {' '.join(pkgs)}",
    )
    # Install is a mutation window — sentinel it so an interrupted install
    # blocks the next run with a recovery command (parity with the repo-mode
    # install path below). Sequential with, never nested in, the build sentinel.
    with sentinel_scope(
        options.state_dir,
        "toolchain",
        recovery_cmd="sudo pacman -S " + " ".join(pkgs),
        retry_cmd="sysforge run toolchain",
        compiler=tcfg.compiler,
    ):
        pacman.install_repo_pkgs(pkgs)

    # Re-probe: the install may still leave a brick (e.g. the freshly installed
    # clang is itself broken), and that one must still abort the run.
    return toolchain_safety.smoke_test_compilers()


def gate1_preflight(
    lib32_pkgs, staging1, staging, pgo_store,
    pkgbuild_map, options, tcfg: "config.ToolchainConfig", *, snapshot,
) -> None:
    """Cheap pre-build safety checks. Hard-fails before anything is built.

    Brick (abort, overridable): PKGBUILD pkgver skew across the lockstep suite
    (``--allow-version-skew``); a non-functional clang / missing lld
    (``smoke_test_compilers``); insufficient build-filesystem space
    (``--skip-build-space-check`` / ``min_build_free_gb``); [multilib] disabled
    while a lib32-* is in scope (``require_multilib``). In dry-run every brick
    is downgraded to a warning so the run can still preview. Advisory (warn):
    residual instrumentation; incomplete rollback snapshot. Runs for BOTH the
    PGO and non-PGO paths.
    """
    dry_run = options.dry_run
    allow_skew = bool(getattr(options, "allow_version_skew", False))
    skip_space = bool(getattr(options, "skip_build_space_check", False))
    require_multilib = tcfg.require_multilib
    min_free_gb = tcfg.min_build_free_gb
    lib32_in_scope = bool(lib32_pkgs)

    def _abort_or_warn(finding) -> None:
        if dry_run:
            # Dry-run verdict, not narration: the real run raises here, so
            # these lines ARE the answer at the shipped default (3.1.0-B6).
            _log.ui(f"[dry-run] Gate 1 [{finding.severity.upper()}] "
                    f"{finding.check_id}: {finding.message}")
            if finding.remediation:
                _log.ui(f"  → {finding.remediation}")
        else:
            raise RuntimeError(
                f"[TOOLCHAIN] Gate 1 ({finding.check_id}): {finding.message} "
                f"{finding.remediation}".rstrip()
            )

    # Brick: PKGBUILD pkgver skew across lockstep members (spirv + lib32 excluded).
    if not allow_skew:
        pkgvers = parse_pkgbuild_pkgvers(pkgbuild_map)
        skew = toolchain_safety.check_pkgver_lockstep(pkgvers)
        if skew is not None:
            _abort_or_warn(skew)
    else:
        _log.warn("--allow-version-skew: skipping the PKGBUILD pkgver lockstep check")

    # Brick: clang must compile + lld must be present (both paths now).
    # A *missing* bootstrap compiler is self-healing: unlike every other build
    # prerequisite it is needed before build_core's makedep installer runs, and
    # upstream's llvm PKGBUILD doesn't list it (upstream builds with gcc), so
    # nothing else would ever install it. Install it here, then re-probe.
    findings = install_bootstrap_compilers(
        toolchain_safety.smoke_test_compilers(), options, tcfg,
    )
    for finding in findings:
        _abort_or_warn(finding)

    # Brick: build filesystems must have headroom.
    if not skip_space:
        space = toolchain_safety.check_build_space(
            [staging1, staging, pgo_store, *(p.parent for p in pkgbuild_map.values())],
            min_free_gb,
        )
        if space is not None:
            _abort_or_warn(space)
    else:
        _log.warn("--skip-build-space-check: skipping the build-space headroom check")

    # Brick: [multilib] must be enabled when lib32 is in scope.
    if require_multilib:
        ml = toolchain_safety.check_multilib_enabled(lib32_in_scope)
        if ml is not None:
            _abort_or_warn(ml)

    # Advisory: residual instrumentation from a prior aborted Pass 1.
    for finding in toolchain_safety.detect_residual_instrumentation():
        _log.warn(f"Gate 1 [{finding.severity.upper()}] {finding.check_id}: "
                  f"{finding.message}")
        if finding.remediation:
            _log.info(f"  → {finding.remediation}")

    # Advisory: rollback-snapshot completeness. Warn up front when auto-undo
    # won't be able to fully restore (a suite member's cached .pkg.tar is gone).
    missing = [name for name, path in snapshot.items() if path is None]
    if missing:
        _log.warn(
            f"Rollback snapshot incomplete: {len(missing)} package(s) have no "
            f"cached .pkg.tar for offline restore ({', '.join(sorted(missing))}). "
            "If post-install verification fails, auto-undo will fall back to "
            "`pacman -S` (network) for those."
        )


def parse_pkgbuild_pkgvers(pkgbuild_map: dict[str, Path]) -> dict[str, str]:
    """Parse each resolved PKGBUILD's pkgver, keyed by package name.

    One parse per unique PKGBUILD directory (split packages share a dir).
    Feeds ``toolchain_safety.check_pkgver_lockstep`` — only lockstep-suite
    members are actually compared there, so spirv-llvm-translator's own version
    scheme can't raise a false skew.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    dir_pkgver: dict[Path, str] = {}
    out: dict[str, str] = {}
    for name, path in pkgbuild_map.items():
        d = path.parent
        if d not in dir_pkgver:
            try:
                meta = parse_pkgbuild(path)
                dir_pkgver[d] = meta.get("globals", {}).get("pkgver", "") or ""
            except Exception as e:
                _log.info(f"  pkgver lockstep: parse failed for {path}: {e}")
                dir_pkgver[d] = ""
        if dir_pkgver[d]:
            out[name] = dir_pkgver[d]
    return out


def gate_soname_consumers(
    pkgbuild_map: dict[str, Path], all_names: list[str], options,
    tcfg: "config.ToolchainConfig",
) -> list[str]:
    """Pre-build libLLVM soname gate. Returns consumer pkgbases to rebuild.

    Sits between Gate 1 and the build (the approval point — the soname is known
    from the resolved PKGBUILD pkgver before any makepkg runs). When the
    about-to-be-built libLLVM changes the soname, it warns + lists the installed
    packages that link the *old* soname (mesa et al.) and applies the
    ``rebuild_soname_consumers`` mode (CLI flag > toolchain.toml > ``prompt``):

      - ``prompt`` (default): TTY → y/N prompt; approving returns the consumers
        for rebuild after Gate 3, declining is a clean abort. Non-TTY → abort
        (never silently break the system; points at ``=auto``/``=off``).
      - ``auto``: no prompt, returns the consumers for rebuild.
      - ``off``: warns loudly + prints the manual ``sysforge build`` command,
        proceeds with the toolchain build but rebuilds nothing (returns []).

    Dry-run previews the impact without prompting or rebuilding (returns []).
    Returns [] whenever there is no soname change or no affected consumer.
    """
    # CLI flag > toolchain.toml. The "prompt" default lives in ToolchainConfig
    # (3.2.0-F6), so there is no third fallback here to drift from it.
    mode = getattr(options, "rebuild_soname_consumers", None) or tcfg.rebuild_soname_consumers
    pkgvers = parse_pkgbuild_pkgvers(pkgbuild_map)
    target_ver = pkgvers.get("llvm") or next(
        (v for n, v in pkgvers.items() if n in LLVM_LOCKSTEP_SUITE), ""
    )
    if not target_ver:
        return []

    impact = toolchain_safety.assess_libllvm_soname_impact(
        target_ver, exclude=set(LLVM_LOCKSTEP_SUITE) | set(all_names),
    )
    if impact is None:
        return []

    _log.warn(
        f"libLLVM soname will change: {impact.old_soname} → {impact.new_soname}. "
        f"{len(impact.consumers)} installed package(s) link the old soname and "
        "would break until rebuilt:"
    )
    for name in impact.consumers:
        _log.warn(f"  - {name}")

    manual_cmd = "sysforge build " + " ".join(impact.consumers)

    if options.dry_run:
        _log.ui(
            f"[dry-run] would rebuild {len(impact.consumers)} consumer(s) "
            "after the toolchain install"
        )
        return []

    if mode == "off":
        _log.warn(
            "rebuild_soname_consumers=off — building the new toolchain WITHOUT "
            f"rebuilding these consumers. Rebuild them yourself afterwards: {manual_cmd}"
        )
        return []

    if mode == "auto":
        _log.ui(
            f"rebuild_soname_consumers=auto — will rebuild {len(impact.consumers)} "
            "consumer(s) after the toolchain install"
        )
        return impact.consumers

    # mode == "prompt"
    if not prompt.is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] Building this libLLVM would change its soname and break "
            f"{len(impact.consumers)} installed package(s), and this is a "
            "non-interactive run. Re-run with --rebuild-soname-consumers=auto to "
            "approve the rebuild, or =off to proceed without it."
        )
    choice = prompt.prompt_choice(
        f"Proceed with the toolchain build and rebuild {len(impact.consumers)} "
        "affected package(s) afterwards? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        tag="TOOLCHAIN",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            "[TOOLCHAIN] Aborted — libLLVM soname change not approved. Nothing "
            "was built or installed."
        )
    return impact.consumers


def rebuild_soname_consumers(consumers: list[str], config, options, state) -> None:
    """Rebuild installed libLLVM consumers after a soname-bumping install.

    Runs OUTSIDE the toolchain sentinel, after Gate 3 passed — a consumer
    rebuild failure must NOT roll back the (intended) toolchain bump. Resolves
    each consumer pkgbase to a build target (``prim_config.find_pkgbuild`` auto-clones repo
    packages like mesa into ``pkgbuild_src_dir``) and routes them through the
    shared build engine with the user's normal profile so they re-link against
    the just-installed libLLVM. On any failure, raises with the manual rebuild
    command; the toolchain itself stays healthy.
    """
    from sysforge import build_core
    from sysforge.pipeline.state import (
        get_toolchain_fingerprint,
        get_toolchain_variant,
    )

    manual_cmd = "sysforge build " + " ".join(consumers)
    _log.ui(
        f"Rebuilding {len(consumers)} libLLVM consumer(s) against the new "
        f"soname: {', '.join(consumers)}"
    )

    targets = []
    unresolved: list[str] = []
    for pkg in consumers:
        try:
            pkgbuild = prim_config.find_pkgbuild(pkg, config)
        except Exception as e:
            _log.warn(f"  could not resolve PKGBUILD for {pkg}: {e}")
            unresolved.append(pkg)
            continue
        targets.append(build_core.target_from_pkgbuild(pkgbuild))

    if not targets:
        raise RuntimeError(
            "[TOOLCHAIN] The new toolchain is installed and healthy, but none of "
            f"the {len(consumers)} libLLVM consumer(s) could be resolved for "
            f"rebuild. Rebuild them manually: {manual_cmd}"
        )

    outcome = build_core.build_and_install(
        targets,
        config=config,
        sync_source=True,
        state_dir=options.state_dir,
        active_variant=get_toolchain_variant(state),
        toolchain_fingerprint=get_toolchain_fingerprint(state),
        abi_check=True,
        review="auto",
    )
    failed = list(outcome.failed_pkgs) + unresolved
    if outcome.install_failed or outcome.aborted or failed:
        detail = ", ".join(failed) if failed else "the install step"
        raise RuntimeError(
            "[TOOLCHAIN] The new toolchain installed cleanly, but rebuilding its "
            f"consumers did not fully succeed ({detail}). Finish the rebuild so "
            f"installed packages link the new libLLVM: {manual_cmd}"
        )
    _log.ui(
        f"Rebuilt {len(outcome.built_pkgs)} consumer(s) against the new "
        "libLLVM soname."
    )


def gate2_audit(
    built_map: dict[str, Path], all_names: list[str], options,
    tcfg: "config.ToolchainConfig",
    *, dry_run: bool,
) -> list[str]:
    """Scan the built ``.pkg.tar*`` for ABI hazards before any install.

    Runs *outside* the install sentinel, between build and install, for BOTH the
    PGO and non-PGO paths (previously only PGO's ``pgo_install`` scanned). Two
    arms:

      1. ``scan_abi_hazards`` — the built suite's own std::-bound-to-LLVM hazard
         (any ``_ZNSt*@LLVM_*``): always a hard abort (the live toolchain could
         not resolve ``std::string`` at runtime).
      2. ``check_system_consumer_symbols`` — graphics consumers (mesa) the
         freshly-built libLLVM would strand. *Unhealable* findings (a dropped
         LLVM backend / target-init symbol) hard-abort here, before any
         ``pacman -U`` — the live ``/usr`` is untouched and no sentinel is left
         behind. *Healable* findings (the same-soname std:: re-export drift —
         the ``-fprofile-use`` libLLVM inlined away the weak libstdc++ copies the
         stock build re-exported) do NOT abort: the installed libLLVM consumers
         are captured (per ``rebuild_soname_consumers`` mode) and returned for
         rebuild after Gate 3, exactly like a soname bump.

    Returns the consumer pkgbases to rebuild post-install (``[]`` when none,
    dry-run, or mode ``off``).
    """
    if dry_run:
        _log.ui("[dry-run] would audit built packages for ABI hazards (Gate 2)")
        return []
    pkgs = profdata.collect_pgo_packages(built_map)
    if not pkgs:
        # Nothing to audit (e.g. AlreadyBuilt with PKGDEST cleared) — the
        # install step will surface a missing-package error of its own.
        return []
    _log.info(f"Gate 2: auditing {len(pkgs)} built package(s) for ABI hazards")
    findings = toolchain_safety.scan_abi_hazards(pkgs)
    if findings:
        joined = "\n".join(f"  - {f.message}" for f in findings)
        raise RuntimeError(
            "[TOOLCHAIN] Gate 2: built packages contain C++ stdlib symbols "
            "bound to the LLVM version namespace — refusing to install (the "
            "live toolchain would be unable to resolve std::string methods at "
            f"runtime). Nothing was installed.\n{joined}\n"
            "Restart with: sysforge run toolchain --rebuild-profdata"
        )

    # Graphics-consumer symbol sufficiency vs the freshly-built libLLVM.
    # Pre-install, outside the sentinel — an abort here leaves the live graphics
    # stack untouched, vs. a post-reboot black screen.
    consumer_findings = toolchain_safety.check_system_consumer_symbols(pkgs)
    if not consumer_findings:
        return []

    # Unhealable: a dropped LLVM backend (e.g. a reduced LLVM_TARGETS_TO_BUILD
    # without AMDGPU). Rebuilding the consumer cannot recover a symbol that no
    # longer exists — hard abort, nothing installed.
    unhealable = [f for f in consumer_findings if not f.healable]
    if unhealable:
        joined = "\n".join(f"  - {f.message}" for f in unhealable)
        raise RuntimeError(
            "[TOOLCHAIN] Gate 2: the freshly-built libLLVM would strand an "
            "installed graphics consumer (mesa) by dropping LLVM target-init "
            "symbols it imports — refusing to install (the desktop would "
            f"black-screen on next session). Nothing was installed.\n{joined}\n"
            f"{unhealable[0].remediation}"
        )

    # All healable: the same-soname std:: re-export drift. Capture the installed
    # libLLVM consumers for rebuild after Gate 3 (reusing the soname-consumer
    # machinery), gated by rebuild_soname_consumers mode.
    joined = "\n".join(f"  - {f.message}" for f in consumer_findings)
    _log.warn(
        "Gate 2: the freshly-built libLLVM no longer re-exports libstdc++ "
        "symbols an installed graphics consumer imports from the LLVM version "
        f"namespace (same-soname PGO re-export drift):\n{joined}"
    )
    return resolve_abi_consumers_to_rebuild(all_names, options, tcfg)


def resolve_abi_consumers_to_rebuild(
    all_names, options, tcfg: "config.ToolchainConfig",
) -> list[str]:
    """Apply ``rebuild_soname_consumers`` mode to the same-soname ABI-drift case.

    Enumerates the installed libLLVM consumers (``libllvm_abi_consumers`` — the
    reverse-dep walk shared with the soname-bump gate) and, per the mode (CLI >
    toolchain.toml > ``prompt``), returns them for post-Gate-3 rebuild, aborts,
    or proceeds without rebuilding. Mirrors :func:`gate_soname_consumers`.
    """
    consumers = toolchain_safety.libllvm_abi_consumers(
        exclude=set(LLVM_LOCKSTEP_SUITE) | set(all_names),
    )
    if not consumers:
        _log.warn(
            "Gate 2: no installed libLLVM consumer resolved for rebuild — "
            "proceeding with the install; rebuild affected packages manually "
            "if the desktop misbehaves."
        )
        return []

    # CLI flag > toolchain.toml. The "prompt" default lives in ToolchainConfig
    # (3.2.0-F6), so there is no third fallback here to drift from it.
    mode = getattr(options, "rebuild_soname_consumers", None) or tcfg.rebuild_soname_consumers
    manual_cmd = "sysforge build " + " ".join(consumers)
    _log.warn(
        f"{len(consumers)} installed package(s) link libLLVM and must be "
        "rebuilt against the new libLLVM:"
    )
    for name in consumers:
        _log.warn(f"  - {name}")

    if mode == "off":
        _log.warn(
            "rebuild_soname_consumers=off — installing the new libLLVM WITHOUT "
            f"rebuilding these consumers. Rebuild them yourself afterwards: {manual_cmd}"
        )
        return []

    if mode == "auto":
        _log.ui(
            f"rebuild_soname_consumers=auto — will rebuild {len(consumers)} "
            "consumer(s) after the toolchain install"
        )
        return consumers

    # mode == "prompt"
    if not prompt.is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] Gate 2: the freshly-built libLLVM strands "
            f"{len(consumers)} installed consumer(s) via std:: re-export drift, "
            "and this is a non-interactive run. Nothing was installed. Re-run "
            "with --rebuild-soname-consumers=auto to install + rebuild them, or "
            "=off to install without rebuilding."
        )
    choice = prompt.prompt_choice(
        f"Install the new libLLVM and rebuild {len(consumers)} affected "
        "package(s) afterwards? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        tag="TOOLCHAIN",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            "[TOOLCHAIN] Gate 2: aborted — libLLVM consumer rebuild not "
            "approved. Nothing was installed."
        )
    return consumers


def snapshot_suite(built_map: dict[str, Path]) -> dict[str, "Path | None"]:
    """Capture the current-install ``.pkg.tar*`` for the suite + built packages.

    The keys are the lockstep suite ∪ the names this build produced; each maps
    to the cached archive for that package's currently-installed version (or
    None when not installed / not in the cache). Used as the offline-undo
    source: on Gate-3 failure the stage reinstalls the present paths in one
    ``pacman -U`` transaction to put the prior-good toolchain back.
    """
    names = set(_SNAPSHOT_SUITE)
    for path in built_map.values():
        names.add(path.parent.name)  # pkgbase dir name; harmless if not a pkg
    names.update(built_map.keys())
    return pacman.cached_pkg_files_for(sorted(names))


def rollback_to_snapshot(snapshot: dict[str, "Path | None"]) -> bool:
    """Reinstall the snapshot's cached packages in one ``pacman -U``.

    Returns True when every captured package was reinstalled (live ``/usr`` is
    back to the prior-good set), False when the snapshot is incomplete (a member
    had no cached file) or the ``pacman -U`` itself failed — in which case the
    caller keeps the sentinel set with a recovery command.
    """
    files = [p for p in snapshot.values() if p is not None]
    missing = [n for n, p in snapshot.items() if p is None]
    if missing:
        _log.error(
            f"Cannot fully roll back: {len(missing)} package(s) have no cached "
            f".pkg.tar ({', '.join(sorted(missing))}). Not attempting a partial "
            "offline restore."
        )
        return False
    if not files:
        return False
    _log.warn(f"Rolling back to {len(files)} cached package(s) via pacman -U")
    return pacman.batch_install_pkgs(files)


def snapshot_recovery_cmd(snapshot: dict[str, "Path | None"]) -> str:
    """Recovery command stored in the sentinel when rollback can't run cleanly.

    Prefers an offline ``pacman -U <cached files>`` when every member is cached;
    otherwise falls back to the online ``pacman -S <suite>``. Stored in the
    sentinel so the next-run recovery prompt has a copy-pasteable restore.
    """
    files = [p for p in snapshot.values() if p is not None]
    if files and all(p is not None for p in snapshot.values()):
        return "sudo pacman -U --noconfirm " + " ".join(str(p) for p in files)
    return verify.llvm_recovery_command()


def log_toolchain_resolution_summary(
    *, compiler, pgo, variant, pgo_pkgs, non_pgo_pkgs, lib32_pkgs,
    staging1, staging, staging3, pgo_store, tcfg: "config.ToolchainConfig",
    options, snapshot,
) -> None:
    """Emit one labelled block of the resolved toolchain-build plan.

    Kernel-parity: consolidates compiler/pgo/variant, package counts, staging
    + pgo_store paths, the Gate-1 settings (min_build_free_gb, skew/space
    overrides, require_multilib), and rollback-snapshot availability so the
    operator can eyeball it before a multi-hour build — and so ``--dry-run`` has
    a readable summary rather than only scattered ``[dry-run] would …`` lines.
    """
    n_total = len(pgo_pkgs) + len(non_pgo_pkgs) + len(lib32_pkgs)
    cached = sum(1 for p in snapshot.values() if p is not None)
    overrides = []
    if getattr(options, "allow_version_skew", False):
        overrides.append("allow-version-skew")
    if getattr(options, "skip_build_space_check", False):
        overrides.append("skip-build-space-check")
    gates = (
        f"min_build_free={tcfg.min_build_free_gb:g}GiB "
        f"require_multilib={'on' if tcfg.require_multilib else 'off'}"
        + (f" overrides={','.join(overrides)}" if overrides else "")
    )
    _log.ui("Toolchain build plan:")
    _log.ui(f"  compiler:   {compiler}  pgo={pgo}  variant={variant}")
    _log.ui(f"  packages:   {n_total} total "
            f"({len(pgo_pkgs)} pgo / {len(non_pgo_pkgs)} non-pgo / {len(lib32_pkgs)} lib32)")
    if pgo:
        _log.ui(f"  staging1:   {staging1}")
        _log.ui(f"  staging2:   {staging}")
        _log.ui(f"  staging3:   {staging3}")
        _log.ui(f"  pgo_store:  {pgo_store}")
    _log.ui(f"  gates:      {gates}")
    _log.ui(f"  snapshot:   {cached}/{len(snapshot)} suite package(s) cached for offline rollback")
