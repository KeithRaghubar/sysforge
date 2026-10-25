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
framework; the argparse surface is ``BuildVerb.add_parser``.
"""
import sys
from pathlib import Path

from sysforge import build_core, log
from sysforge.primitives import journal
from sysforge.primitives.aur import is_repo_package
from sysforge.primitives.config import (
    find_pkgbuild,
    resolve_repo_mode,
    resolve_flag_default,
    REPO_MODE_SOURCE,
    PKG_KEY_BUILD_FROM_SOURCE,
)
from sysforge.build.makepkg_wrapper import expand_makepkg_flags
from sysforge.primitives.prompt import is_interactive, prompt_choice
from sysforge.primitives.pkgbuild_review import resolve_review_policy
from sysforge.primitives.profile import TOOLCHAIN_CHOICES, toolchain_for_compilers
from sysforge.verbs import ExecResult, PreCheckResult, Verb
from sysforge.verbs.helpers import CLEANSRC_FORCE_HELP, load_config_with_overrides

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
        _log.ui(render_preflight(report))


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
    if outcome.review_refused:
        parts.append(f"{len(outcome.review_refused)} refused at review")
    if outcome.pgo_skipped_pkgs:
        parts.append(f"{len(outcome.pgo_skipped_pkgs)} pgo-skipped")
    suffix = log.red(" (install FAILED)") if outcome.install_failed else ""
    _log.ui(f"\n[SYSFORGE] {log.bold('Build complete')}: {', '.join(parts)}{suffix}.")
    landed = [pb for pb in outcome.built_pkgs if pb not in outcome.not_installed]
    if landed:
        _log.ui(f"  {log.green('Built:')}       {' '.join(landed)}")
    for pb, reason in outcome.not_installed.items():
        _log.ui(f"  {log.red('Not installed:')} {pb} ({reason})")
    if outcome.failed_pkgs:
        _log.ui(f"  {log.red('Failed:')}      {' '.join(outcome.failed_pkgs)}")
    if outcome.review_skipped:
        _log.ui(f"  {log.dim('Skipped:')}     {' '.join(outcome.review_skipped)} (PKGBUILD review)")
    for pb, why in outcome.review_refused.items():
        _log.ui(f"  {log.red('Refused:')}     {pb}: {why}")
    if outcome.pgo_skipped_pkgs:
        _log.ui(
            f"  {log.dim('PGO-skipped:')} {' '.join(outcome.pgo_skipped_pkgs)}"
            " (run 'sysforge run toolchain' to rebuild profdata)"
        )


def _print_layout_findings(outcome) -> None:
    """Payload-layout findings (3.2.0-F15), printed for single-package runs
    too: the multi-package totals are skippable narration, this is not — the
    mid-run warning is -v only, and a PAM file nested where PAM never looks
    has already cost a desktop outage."""
    if not outcome.layout_findings:
        return
    _log.ui(f"  {log.red('Payload layout:')} installed, but never read by its consumer:")
    for line in outcome.layout_findings:
        _log.ui(f"    {line}")


def _load_repo_optin(config) -> tuple[dict, set[str]]:
    """Return (build_cfg, opted_in_names) from packages.toml.

    ``opted_in_names`` is the set of per-package names whose
    ``enable_build_from_source`` is true. Reads through
    ``expand_package_groups``. The legacy ``pkgbuild_patch`` key is no longer
    honored on read as of 3.0.0 — only the current key counts.
    A missing/unreadable file yields ``({}, set())``.
    """
    import tomllib

    from sysforge.primitives.config import expand_package_groups
    from sysforge.primitives.paths import resolve_packages_path
    try:
        path = resolve_packages_path(config)
        with path.open("rb") as f:
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
    from sysforge.verbs.shared import entry_toml_block, rewrite_packages_toml
    from sysforge.primitives.paths import resolve_packages_path
    try:
        path = resolve_packages_path(config)
        entry = {"name": name, PKG_KEY_BUILD_FROM_SOURCE: True}
        block = "\n" + entry_toml_block(entry) + "\n"
        # Replace any existing entry for this name in one write; a missing
        # file starts from the standard header (3.5.0-B48).
        rewrite_packages_toml(path, append=block, drop_name=name)
        _log.ui(f"{name}: recorded enable_build_from_source = true in {path}")
        return True
    except Exception as e:
        _log.warn(f"{name}: could not write packages.toml opt-in: {e}")
        return False


def _cli_toolchain(args) -> str | None:
    """The whole-bundle toolchain this build asked for (3.5.0-F14): ``--toolchain``,
    else a ``--cc``/``--cxx`` pair that is exactly a bundle's compilers (gcc/g++,
    clang/clang++). Any other pair stays a raw CC/CXX override."""
    if getattr(args, "toolchain", None):
        return args.toolchain
    cc, cxx = getattr(args, "cc", None), getattr(args, "cxx", None)
    return toolchain_for_compilers(cc, cxx) if cc and cxx else None


class BuildVerb(Verb):
    """Build one or more packages using their matched profiles."""

    name = "build"
    wants_run_log = True
    requires_sentinel = True

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("build", help="Build a package from a PKGBUILD.")
        p.add_argument(
            "pkgbuilds", nargs="+", metavar="PKGBUILD",
            help="One or more packages to build (path, directory, or bare package name).",
        )
        p.add_argument("--makepkg", "-m", metavar="FLAGS",
            help="Additional makepkg flags, appended after profile makepkg_flags. "
                 "Usually optional: bare makepkg short flags are forwarded "
                 "implicitly (sysforge build PKGBUILD -sfc works without -m). "
                 "-m is only needed for makepkg long flags (e.g. --skippgpcheck), "
                 "flags that take a value (-p, -D), or flags sysforge claims for "
                 "itself (-h, -V).",
        )
        p.add_argument("--interactive", action="store_true",
            help="Run makepkg without --noconfirm and hand stdout/stderr "
                 "to makepkg's terminal so pacman conflict prompts and other "
                 "unbuffered interactive output appear immediately (disables "
                 "line-based output classification while set).")
        p.add_argument("--persist-log", action="store_true", dest="persist_log",
            help="Keep the per-package log file after a successful build. "
                 "Also settable via [build] persist_log = true in packages.toml.")
        p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
            help="Disable per-package log files.")
        p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
            help="Directory for the per-package log file (default: alongside the PKGBUILD).")
        p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
            help="Path to a profiles.toml to use instead of the default.")
        p.add_argument("--toolchain", choices=TOOLCHAIN_CHOICES, dest="toolchain",
            help="Build the named packages with this compiler toolchain as a whole "
                 "(compilers, binutils and linker) instead of their profile's, for "
                 "this build. Dependencies keep their own.")
        p.add_argument("--save-toolchain", action="store_true", dest="save_toolchain",
            help="With --toolchain: also save it for these packages in "
                 "profiles.toml [package_compiler_overrides], so later builds and "
                 "updates use it too.")
        p.add_argument("--reset-toolchain", action="store_true", dest="reset_toolchain",
            help="Remove these packages' saved toolchain from profiles.toml, so they "
                 "build with their profile's again.")
        p.add_argument("--cc", metavar="COMPILER", dest="cc",
            help="Override only CC (C compiler) for this build, e.g. --cc clang-19. "
                 "A gcc/g++ or clang/clang++ pair with --cxx acts as --toolchain.")
        p.add_argument("--cxx", metavar="COMPILER", dest="cxx",
            help="Override only CXX (C++ compiler) for this build, e.g. --cxx clang++-19.")
        p.add_argument("--ld", metavar="LINKER", dest="ld",
            help="Override linker for this build, e.g. --ld lld.")
        p.add_argument("--cache-report", action="store_true", dest="cache_report",
            help="Print a structured cache summary (ccache/sccache hit rates) after the build. "
                 "Also settable via [build] cache_report = true in packages.toml. Builds run "
                 "in the build sandbox show n/a: they never use the host caches.")
        p.add_argument("--abi-check", action="store_true", dest="abi_check",
            help="Run a post-build ABI compatibility check on built shared libraries. "
                 "Also settable via [build] abi_check = true in packages.toml.")
        p.add_argument("--no-update", action="store_true", dest="no_update",
            help="Skip the source sync (fetch and fast-forward) and build the "
                 "PKGBUILD as checked out. --cleansrc still syncs.")
        p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
            help="Purge the package src dir and re-clone before building. "
                 "Refuses (per package) if the existing clone has uncommitted changes, "
                 "ahead-of-upstream commits, or no upstream tracking branch.")
        p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
            help=CLEANSRC_FORCE_HELP)
        p.add_argument("--no-llvm-preflight", action="store_true", dest="no_llvm_preflight",
            help="Suppress the LLVM source pre-flight summary.")
        review_group = p.add_mutually_exclusive_group()
        review_group.add_argument("--review", action="store_true", dest="review",
            help="Review changed sources of every type, including those trusted by "
                 "[build] review_trusted_sources (by default Arch's own packaging repos).")
        review_group.add_argument("--no-review", action="store_true", dest="no_review",
            help="Skip the PKGBUILD review gate (full source-tree diff prompt for "
                 "packages whose source changed since the last accepted review). "
                 "Also configurable via [build] review = false in packages.toml.")
        p.add_argument("--force", action="store_true", dest="force",
            help="Unconditionally build all arguments from source for this run "
                 "only, including repo packages not yet opted in. Never prompts "
                 "for or modifies packages.toml opt-in keys.")
        p.add_argument("--rebuild", action="store_true", dest="rebuild",
            help="Force makepkg to rebuild even when a matching package already "
                 "exists in PKGDEST (passes -f). Use after a toolchain or build-flag "
                 "change, where the version is unchanged so makepkg would otherwise "
                 "skip the build and reinstall the stale artifact. Note --force is "
                 "unrelated: it only waives the repo-package opt-in gate.")
        p.add_argument("--pgo", choices=("record", "use"), dest="pgo_mode",
            help="Instrumentation PGO (LLVM toolchain only). "
                 "--pgo=record builds+installs an instrumented package that writes "
                 "profile data to the sysforge store as you run its workloads; "
                 "--pgo=use merges the collected profiles and rebuilds an optimized "
                 "<pkg>-sysforge (conflicts/replaces the stock package). Aimed at "
                 "mesa; any other target warns unless listed in sysforge.toml "
                 "[pgo] allow.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.set_defaults(verb_cls=cls)
        return p

    @classmethod
    def run_log_name(cls) -> str | None:
        """A multi-package ``build`` run leaves one consolidated log next to
        the per-package logs, parallel to ``sysforge-update.log``.

        ``build`` goes through ``build_core`` rather than the pipeline runner,
        so — unlike ``run kernel``/``run toolchain``, which already emit a
        consolidated log via ``run_stage_standalone`` — it would otherwise have
        no run-level log at all. Opened/closed by the verb runner; dry runs
        write nothing."""
        return "build"

    def journal_target(self, args) -> str | None:
        return journal.pkg_target(getattr(args, "packages", None) or [])

    def pre_check(self, args) -> PreCheckResult:
        if args.no_pkg_log and args.log_dir:
            print(
                "[SYSFORGE] Warning: --log-dir has no effect when --no-pkg-log is set.",
                file=sys.stderr,
            )
        _log.info(f"Invocation: {' '.join(sys.argv)}")
        if getattr(args, "save_toolchain", False):
            if getattr(args, "reset_toolchain", False):
                return PreCheckResult(
                    blocker="--save-toolchain and --reset-toolchain contradict each other",
                    exit_code=2)
            if not _cli_toolchain(args):
                return PreCheckResult(
                    blocker="--save-toolchain needs --toolchain (or a gcc/g++ or "
                            "clang/clang++ --cc/--cxx pair) to save",
                    exit_code=2)
        config = load_config_with_overrides(args)
        # AUR posture (3.1.0-F3): refuse an AUR-tracked target up front rather
        # than failing mid-sync. The checkout's origin decides (C3), not the
        # recorded label alone: a checkout from elsewhere never contacts the AUR.
        from sysforge.primitives.net_policy import get_policy
        if not get_policy().aur_enabled:
            from sysforge.pipeline.state import resolve_state_dir
            from sysforge.primitives.aur import is_aur_checkout
            from sysforge.primitives.build_state import BuildState
            bs = BuildState(resolve_state_dir(getattr(args, "state_dir", None))[0])
            for pkg in args.pkgbuilds:
                name = _pkg_to_name(pkg)
                entry = bs.get(name) or {}
                if entry.get("source") == "aur" and is_aur_checkout(
                        entry.get("pkgbuild_dir") or Path(pkg)):
                    return PreCheckResult(
                        blocker=f"{name} is an AUR package and [aur] enabled = false",
                        exit_code=2)
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
            toolchain = (_cli_toolchain(args)
                         or config.get("defaults", {}).get("toolchain"))
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
        from sysforge.primitives.llvm_state import preflight_suppressed
        if not preflight_suppressed(args):
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
                        f"{target.pkgbase} is a repo package; build from source? "
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
                        "builds: set enable_build_from_source=true in packages.toml "
                        "(or run `sysforge packages add "
                        f"{target.pkgbase} --enable-build-from-source`), or pass "
                        "--force to build it this run only."
                    )
                    continue

            # Two arguments can resolve to the same pkgbase — split-package
            # members (`build wayland-git wayland-docs-git`) share one
            # PKGBUILD, and makepkg builds every member of a base in one run.
            # Dedup so the base isn't built twice.
            if any(t.pkgbase == target.pkgbase for t in targets):
                _log.info(
                    f"{pkg}: already covered by the {target.pkgbase} build "
                    "(same pkgbase); skipping duplicate."
                )
                continue

            # 3.0.0-B9: --rebuild is a per-target makepkg -f. Without it a
            # rebuild at an unchanged pkgver is skipped by makepkg (exit 13)
            # and the stale artifact reinstalled. (--force is a different
            # thing entirely — the repo-package opt-in waiver above.)
            target.force_rebuild = getattr(args, "rebuild", False)

            targets.append(target)

        if not targets:
            return ExecResult()

        # `build` is a strict subset of `update`: it routes through the same
        # shared engine (dep pre-install + AUR/local dep build, makepkg with
        # -s/-i stripped so makepkg never resolves deps itself, deferred bulk
        # install). The only difference is sync_source=True — build keeps its
        # inline per-package source sync (update syncs up front in Phase 2).
        from sysforge.pipeline.state import (
            PipelineState, get_toolchain_fingerprint, get_toolchain_variant,
            resolve_state_dir,
        )
        state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
        # 3.4.0-F4: building a package is an explicit adoption — lift any
        # revert/forget opt-out so the next `update` maintains it again.
        if not getattr(args, "dry_run", False):
            from sysforge.primitives.opt_out import OptOutError, OptOutList
            try:
                cleared = OptOutList(state_dir).clear(
                    {t.pkgbase for t in targets} | set(packages))
            except OptOutError as e:
                _log.error(str(e))
                return ExecResult(exit_code=1)
            for base in cleared:
                _log.ui(f"{base}: maintained again (opt-out lifted by `build`)")
        _pstate = PipelineState(state_dir)
        active_variant = get_toolchain_variant(_pstate)
        active_fingerprint = get_toolchain_fingerprint(_pstate)

        # One resolver for both verbs (3.0.0-F3). --force skips the opt-in
        # read, but the review settings still come from packages.toml.
        review_policy = resolve_review_policy(
            args, _load_repo_optin(config)[0] if force else build_cfg)
        outcome = build_core.build_and_install(
            targets,
            config=config,
            sync_source=not args.no_update,
            interactive=args.interactive,
            profile_conf=args.profile_conf,
            cc=args.cc,
            cxx=args.cxx,
            ld=args.ld,
            toolchain=_cli_toolchain(args),
            save_toolchain=getattr(args, "save_toolchain", False),
            reset_toolchain=getattr(args, "reset_toolchain", False),
            state_dir=state_dir,
            pkg_log=not args.no_pkg_log,
            persist_log=resolve_flag_default(args, "persist_log", build_cfg, "persist_log"),
            log_dir=Path(args.log_dir) if args.log_dir else None,
            cache_report=resolve_flag_default(args, "cache_report", build_cfg, "cache_report"),
            abi_check=resolve_flag_default(args, "abi_check", build_cfg, "abi_check"),
            extra_flags=extra_flags,
            active_variant=active_variant,
            toolchain_fingerprint=active_fingerprint,
            pgo_mode=getattr(args, "pgo_mode", None),
            review=review_policy.mode,
            review_trusted=review_policy.trusted,
        )
        if len(targets) > 1 and not outcome.aborted:
            _print_build_summary(outcome)
        _print_layout_findings(outcome)
        if len(targets) == 1:
            for pb, why in outcome.review_refused.items():
                _log.ui(f"[SYSFORGE] {pb}: refused at review: {why}; "
                        "run at a terminal to review it")
        _report_timings(outcome, args)
        if outcome.aborted:
            # User aborted at the PKGBUILD review gate; build_core already
            # printed the abort line. Exit 2 mirrors the sentinel-refusal code.
            return ExecResult(exit_code=2)
        if outcome.failed_pkgs or outcome.install_failed or outcome.review_refused:
            return ExecResult(exit_code=1)
        return ExecResult()
