"""
build_cmd.py — ``sysforge build`` verb.

Builds one or more packages using their matched profiles. ``build`` is a strict
subset of ``update``: it routes through the shared ``build_core`` engine (dep
pre-install + AUR/local dep build + deferred bulk install), adding only the
build-specific concerns — the ``--cleansrc`` source purge and inline per-package
source sync (``update`` syncs up front instead). Dispatched through the Verb
framework; the argparse surface lives in ``cli._add_build_parser``.
"""
import sys
from pathlib import Path

from sysforge import build_core, log
from sysforge.primitives.config import find_pkgbuild
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags
from sysforge.verbs import ExecResult, PreCheckResult, Verb
from sysforge.verbs.helpers import load_config_with_overrides

# The build verb logs under its own name, matching every other verb command
# module (update -> [UPDATE], packages -> [PACKAGES])
# and the [BUILD] tag the verb runner derives from verb.name at dispatch.
_log = log.get_logger("BUILD")


def _cleansrc_target_dir(pkg: str, config: dict) -> Path | None:
    """
    Resolve the src dir that --cleansrc should purge for pkg.

    Returns the directory under pkgbuild_src_dir that find_pkgbuild would
    use, or None if pkg is a path to an existing PKGBUILD/dir (in which
    case purging would destroy user-supplied input).
    """
    p = Path(pkg)
    if p.exists():
        return None
    raw = config.get("paths", {}).get("pkgbuild_src_dir") if config else None
    if not raw:
        return None
    return Path(raw).expanduser() / pkg


def _pkg_to_name(p: str) -> str:
    """Best-effort extraction of a pkgname from a build positional.

    Accepts a bare name, a directory, or a path to a PKGBUILD; falls back
    to the input string when no clearer name is available. Used only by the
    LLVM safety pre-flight, which then filters with ``is_llvm_pkgbase``.
    """
    pp = Path(p)
    if pp.name == "PKGBUILD":
        return pp.parent.name
    if pp.is_dir():
        return pp.name
    return p


def _render_llvm_preflight(names: list[str], config: dict) -> None:
    """Render the LLVM safety pre-flight summary, if any names match."""
    from sysforge.primitives.llvm_state import collect_llvm_state, render_preflight
    report = collect_llvm_state(names, config)
    if report.states:
        print(render_preflight(report))


def _report_timings(outcome, args) -> None:
    """Render the phase-timing report from the outcome under [BUILD].

    Always written at info level (lands in the unified log); promoted to UI
    output when --timings is set.
    """
    from sysforge.primitives.timing import PhaseTimer, render_report

    timer = PhaseTimer(records=outcome.phase_records)
    emit = _log.ui if getattr(args, "timings", False) else _log.info
    for line in render_report(timer, title="Build phase timings"):
        emit(line)


def _print_build_summary(outcome) -> None:
    """End-of-run totals for multi-package builds, mirroring ``update``'s
    summary block. Single-package runs skip it — the per-package build/install
    narration already tells the whole story there."""
    parts = [
        f"{len(outcome.built_pkgs)} built",
        f"{len(outcome.failed_pkgs)} failed",
    ]
    if outcome.review_skipped:
        parts.append(f"{len(outcome.review_skipped)} skipped at review")
    if outcome.pgo_skipped_pkgs:
        parts.append(f"{len(outcome.pgo_skipped_pkgs)} pgo-skipped")
    suffix = " (install FAILED)" if outcome.install_failed else ""
    _log.ui(f"\n[SYSFORGE] Build complete: {', '.join(parts)}{suffix}.")
    if outcome.built_pkgs:
        _log.ui(f"  Built:       {' '.join(outcome.built_pkgs)}")
    if outcome.failed_pkgs:
        _log.ui(f"  Failed:      {' '.join(outcome.failed_pkgs)}")
    if outcome.review_skipped:
        _log.ui(f"  Skipped:     {' '.join(outcome.review_skipped)} (PKGBUILD review)")
    if outcome.pgo_skipped_pkgs:
        _log.ui(
            f"  PGO-skipped: {' '.join(outcome.pgo_skipped_pkgs)}"
            " (run 'sysforge run toolchain' to rebuild profdata)"
        )


def _review_config_enabled(config) -> bool:
    """packages.toml ``[build] review`` default for the review gate.

    True unless the file exists and explicitly sets ``review = false`` —
    a missing or unreadable packages.toml must not disable the gate.
    """
    import tomllib

    from sysforge.primitives.paths import resolve_packages_path
    try:
        path = resolve_packages_path(config)
        with open(path, "rb") as f:
            return tomllib.load(f).get("build", {}).get("review", True) is not False
    except Exception:
        return True


class BuildVerb(Verb):
    """Build one or more packages using their matched profiles."""

    name = "build"
    requires_sentinel = True

    def pre_check(self, args) -> PreCheckResult:
        if args.no_pkg_log and args.log_dir:
            print(
                "[SYSFORGE] Warning: --log-dir has no effect when --no-pkg-log is set.",
                file=sys.stderr,
            )
        _log.info(f"Invocation: {' '.join(sys.argv)}")
        config = load_config_with_overrides(args)
        if not getattr(args, "no_llvm_preflight", False):
            _render_llvm_preflight([_pkg_to_name(p) for p in args.pkgbuilds], config)
        if getattr(args, "dry_run", False):
            self.requires_sentinel = False
        return PreCheckResult(ctx={"config": config})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        config = pre.ctx["config"]
        extra_flags = expand_makepkg_flags(args.makepkg) if args.makepkg else None
        packages = args.pkgbuilds
        cleansrc_force = getattr(args, "cleansrc_force", False)
        cleansrc_active = cleansrc_force or getattr(args, "cleansrc", False)

        # Resolve each requested package to a build target. --cleansrc purges
        # the source tree first (build-only concern). A purge refusal skips
        # that package but does not abort the rest of the batch.
        targets = []
        for pkg in packages:
            if cleansrc_active:
                from sysforge.primitives.aur import purge_src
                target_dir = _cleansrc_target_dir(pkg, config)
                if target_dir is not None:
                    try:
                        purge_src(target_dir, force=cleansrc_force)
                    except RuntimeError as e:
                        _log.error(f"--cleansrc {pkg!r}: {e}")
                        continue
            pkgbuild = find_pkgbuild(pkg, config)
            targets.append(build_core.target_from_pkgbuild(pkgbuild))

        if not targets:
            return ExecResult()

        # `build` is a strict subset of `update`: it routes through the same
        # shared engine (dep pre-install + AUR/local dep build, makepkg with
        # -s/-i stripped so makepkg never resolves deps itself, deferred bulk
        # install). The only difference is sync_source=True — build keeps its
        # inline per-package source sync (update syncs up front in Phase 2).
        from sysforge.pipeline.state import (
            PipelineState, get_toolchain_variant, resolve_state_dir,
        )
        state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
        active_variant = get_toolchain_variant(PipelineState(state_dir))

        outcome = build_core.build_and_install(
            targets,
            config=config,
            sync_source=not args.no_update,
            interactive=args.interactive,
            profile_conf=args.profile_conf,
            cc=args.cc,
            cxx=args.cxx,
            ld=args.ld,
            state_dir=state_dir,
            pkg_log=not args.no_pkg_log,
            persist_log=args.persist_log,
            log_dir=Path(args.log_dir) if args.log_dir else None,
            cache_report=args.cache_report,
            abi_check=args.abi_check,
            extra_flags=extra_flags,
            active_variant=active_variant,
            review=(
                "prompt"
                if not getattr(args, "no_review", False)
                and _review_config_enabled(config)
                else "off"
            ),
        )
        if len(targets) > 1 and not outcome.aborted:
            _print_build_summary(outcome)
        _report_timings(outcome, args)
        if outcome.aborted:
            # User aborted at the PKGBUILD review gate; build_core already
            # printed the abort line. Exit 2 mirrors the sentinel-refusal code.
            return ExecResult(exit_code=2)
        if outcome.failed_pkgs or outcome.install_failed:
            return ExecResult(exit_code=1)
        return ExecResult()
