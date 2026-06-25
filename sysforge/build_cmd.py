# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
from sysforge.primitives.aur import is_repo_package
from sysforge.primitives.config import (
    find_pkgbuild,
    resolve_repo_mode,
    REPO_MODE_SOURCE,
    PKG_KEY_BUILD_FROM_SOURCE,
)
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags
from sysforge.primitives.prompt import is_interactive, prompt_choice
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
    suffix = log.red(" (install FAILED)") if outcome.install_failed else ""
    _log.ui(f"\n[SYSFORGE] {log.bold('Build complete')}: {', '.join(parts)}{suffix}.")
    if outcome.built_pkgs:
        _log.ui(f"  {log.green('Built:')}       {' '.join(outcome.built_pkgs)}")
    if outcome.failed_pkgs:
        _log.ui(f"  {log.red('Failed:')}      {' '.join(outcome.failed_pkgs)}")
    if outcome.review_skipped:
        _log.ui(f"  {log.dim('Skipped:')}     {' '.join(outcome.review_skipped)} (PKGBUILD review)")
    if outcome.pgo_skipped_pkgs:
        _log.ui(
            f"  {log.dim('PGO-skipped:')} {' '.join(outcome.pgo_skipped_pkgs)}"
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


def _load_repo_optin(config) -> tuple[dict, set[str]]:
    """Return (build_cfg, opted_in_names) from packages.toml.

    ``opted_in_names`` is the set of per-package names whose
    ``enable_build_from_source`` is true. Reads through
    ``expand_package_groups`` so the legacy ``pkgbuild_patch`` key is honored.
    A missing/unreadable file yields ``({}, set())``.
    """
    import tomllib

    from sysforge.primitives.config import expand_package_groups
    from sysforge.primitives.paths import resolve_packages_path
    try:
        path = resolve_packages_path(config)
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}, set()
    build_cfg = data.get("build", {}) or {}
    opted_in = {
        e["name"] for e in expand_package_groups(data)
        if e.get("name") and e.get(PKG_KEY_BUILD_FROM_SOURCE)
    }
    return build_cfg, opted_in


def _repo_pkg_opted_in(name: str, build_cfg: dict, opted_in: set[str]) -> bool:
    """True if a repo package is already opted into source builds.

    Opted in when the global ``repo_mode`` is ``build_from_source`` OR the
    per-package ``enable_build_from_source`` key is set.
    """
    if resolve_repo_mode(build_cfg) == REPO_MODE_SOURCE:
        return True
    return name in opted_in


def _write_repo_optin(name: str, config) -> bool:
    """Persist ``enable_build_from_source = true`` for ``name`` in packages.toml.

    Reuses the packages-command writers (single packages.toml mutation home);
    creates the file with a standard header when absent. Returns True on
    success, False (with a warning) on any I/O error — a write failure must
    not abort the build the user already confirmed.
    """
    from sysforge.packages_cmd import _rewrite_packages_toml, entry_toml_block
    from sysforge.primitives.paths import resolve_packages_path
    try:
        path = resolve_packages_path(config)
        entry = {"name": name, PKG_KEY_BUILD_FROM_SOURCE: True}
        block = "\n" + entry_toml_block(entry) + "\n"
        # Replace any existing entry for this name, then append the new one.
        _rewrite_packages_toml(path, drop_name=name)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# packages.toml — managed by sysforge packages\n"
                "\n[build]\n"
                'pkgbuild_src_dir = "~/src"\n'
            )
        _rewrite_packages_toml(path, append=block)
        _log.ui(f"{name}: recorded enable_build_from_source = true in {path}")
        return True
    except Exception as e:
        _log.warn(f"{name}: could not write packages.toml opt-in: {e}")
        return False


class BuildVerb(Verb):
    """Build one or more packages using their matched profiles."""

    name = "build"
    requires_sentinel = True

    def unified_log_basename(self, args) -> str | None:
        """A multi-package ``build`` run leaves one consolidated log next to
        the per-package logs, parallel to ``sysforge-update.log``.

        ``build`` goes through ``build_core`` rather than the pipeline runner,
        so — unlike ``run kernel``/``run toolchain``, which already emit a
        consolidated ``sysforge.log`` via ``run_stage_standalone`` — it would
        otherwise have no run-level log at all. Opened/closed by the verb
        runner; dry runs write nothing."""
        del args
        return "sysforge-build.log"

    def pre_check(self, args) -> PreCheckResult:
        if args.no_pkg_log and args.log_dir:
            print(
                "[SYSFORGE] Warning: --log-dir has no effect when --no-pkg-log is set.",
                file=sys.stderr,
            )
        _log.info(f"Invocation: {' '.join(sys.argv)}")
        config = load_config_with_overrides(args)
        # Optimization gate (one home: profile.is_llvm_toolchain + LLVM_REQUIRED_HINT):
        # instrumentation PGO instruments with clang and merges with llvm-profdata,
        # so it has no gcc path. Block cleanly before any build work.
        if getattr(args, "pgo_mode", None):
            from sysforge.primitives.config import (
                load_sysforge_toml,
                pgo_warns_for,
            )
            from sysforge.primitives.profile import (
                LLVM_REQUIRED_HINT,
                is_llvm_toolchain,
            )
            toolchain = config.get("defaults", {}).get("toolchain")
            if not is_llvm_toolchain(toolchain):
                return PreCheckResult(
                    blocker=f"PGO (--pgo) requires the LLVM toolchain. "
                            f"{LLVM_REQUIRED_HINT}"
                )
            # PGO works on any package (F5) but is rarely worth the doubled build
            # + manual workload outside a hot, long-lived library. Warn once per
            # un-allow-listed target; mesa-family + sysforge.toml [pgo] allow are
            # quiet. Warning only — the build proceeds.
            sysforge_cfg = load_sysforge_toml()
            for pkg in args.pkgbuilds:
                name = _pkg_to_name(pkg)
                if pgo_warns_for(name, sysforge_cfg):
                    _log.warn(
                        f"--pgo on {name!r}: PGO is not recommended for most "
                        "packages (doubled build + a manual record/use workload). "
                        "Add it to sysforge.toml [pgo] allow to silence this."
                    )
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
        force = getattr(args, "force", False)

        # Repo-package opt-in gate: `build` source-builds AUR/git/local targets
        # unconditionally (that's their only path), but a *repo* package is
        # installed from pacman by default. Source-building one is opt-in — the
        # global repo_mode or the per-package enable_build_from_source key. With
        # --force we build every argument this run without prompting or touching
        # packages.toml; otherwise an un-opted-in repo target prompts (TTY) or
        # aborts with a hint (non-TTY). Loaded once, before the loop.
        build_cfg, opted_in = ({}, set()) if force else _load_repo_optin(config)

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
            target = build_core.target_from_pkgbuild(pkgbuild)
            # Record origin so build_state is self-describing: a repo package
            # built from source is stamped source="repo" so `sysforge update`
            # classifies it repo_class="source" (rebuild-from-source) outright
            # rather than leaning on the non-foreign→repo inference. Non-repo
            # packages keep source=None — their aur/git/local origin is
            # recovered from pacman -Qm foreign-ness at update time, and
            # guessing here risks mis-routing a local-only PKGBUILD that
            # shadows a repo name.
            if target.source is None and is_repo_package(target.pkgbase):
                target.source = "repo"

            # Apply the repo-package gate (skipped entirely under --force).
            if not force and target.source == "repo" \
                    and not _repo_pkg_opted_in(target.pkgbase, build_cfg, opted_in):
                if is_interactive():
                    ans = prompt_choice(
                        f"{target.pkgbase} is a repo package — build from source? "
                        "[y/N]: ",
                        choices=("y", "n"),
                        default="n",
                        retry_on_invalid=False,
                        tag="BUILD",
                    )
                    if ans != "y":
                        _log.ui(f"{target.pkgbase}: skipped (kept as a pacman package).")
                        continue
                    _write_repo_optin(target.pkgbase, config)
                else:
                    _log.error(
                        f"{target.pkgbase} is a repo package not opted into source "
                        "builds — set enable_build_from_source=true in packages.toml "
                        "(or run `sysforge packages add "
                        f"{target.pkgbase} --enable-build-from-source`), or pass "
                        "--force to build it this run only."
                    )
                    continue

            targets.append(target)

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
            pgo_mode=getattr(args, "pgo_mode", None),
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
