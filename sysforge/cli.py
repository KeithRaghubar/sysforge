"""
cli.py — SysForge command-line interface

Top-level commands:
    sysforge build <pkg>    Build a package using its matched profile
    sysforge update         Check for and rebuild outdated sysforge-managed packages
    sysforge resolve <pkg>  Show which profile would be applied to a package
    sysforge converge       Rebuild packages that have drifted from their profile
    sysforge doctor [PKG]   Health-check installed package depends + linkage

Namespaces:
    sysforge packages       Manage packages.toml (list / add / remove / sync)
    sysforge run            Execute pipeline stages (pipeline / hardware / reconfigure / toolchain / packages / kernel)

Every verb is a ``Verb`` subclass dispatched through
:func:`sysforge.verbs.runner.run_verb`. The pre_check / execute /
post_validate split is documented in DESIGN.md §CLI Verb Framework.
"""
import argparse
import sys
from pathlib import Path

from sysforge import log

_log = log.get_logger("CLI")

from sysforge.converge import ConvergeVerb
from sysforge.doctor import DoctorVerb
from sysforge.fetch import FetchVerb
from sysforge.log_cmd import LogVerb
from sysforge.packages_cmd import (
    PackagesAddVerb,
    PackagesListVerb,
    PackagesRemoveVerb,
)
from sysforge.primitives.config import find_pkgbuild, load_config
from sysforge.primitives.makepkg_wrapper import BuildOptions, expand_makepkg_flags, run
from sysforge.primitives.paths import PACKAGES_PATH, resolve_packages_path
from sysforge.resolve import ResolveVerb
from sysforge.setup_cmd import SetupVerb
from sysforge.state_cmd import StateListVerb, StateOrphansVerb, StateRepairVerb
from sysforge.update import UpdateVerb
from sysforge.verbs import ExecResult, PreCheckResult, Verb, run_verb

_PACKAGES_HELP = f"Path to packages.toml (default: {PACKAGES_PATH})."


# ---------------------------------------------------------------------------
# Helpers shared between verbs
# ---------------------------------------------------------------------------

def _load_config_with_overrides(args) -> dict:
    """Load flag_profiles config and apply CLI overrides (--packages, --profile-conf)."""
    config = load_config() or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages
    if getattr(args, "profile_conf", None):
        config["profile_conf"] = args.profile_conf
    return config


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
    """Best-effort extraction of a pkgname from a build/converge positional.

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


# ---------------------------------------------------------------------------
# Verb classes defined here (the ones whose work is inline rather than in a
# dedicated module). The others (UpdateVerb, FetchVerb, ConvergeVerb,
# DoctorVerb, ResolveVerb, SetupVerb, PackagesXVerb, StateXVerb) are imported
# above from their respective modules.
# ---------------------------------------------------------------------------

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
        config = _load_config_with_overrides(args)
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
        for i, pkg in enumerate(packages):
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

            # Resolve and build AUR deps before the main package
            from sysforge.primitives.aur_resolve import (
                build_resolved_deps,
                resolve_aur_deps,
            )
            aur_deps = resolve_aur_deps(pkgbuild, config, fetch=True)
            if aur_deps:
                build_resolved_deps(
                    aur_deps,
                    profile_conf=args.profile_conf,
                    cc_override=args.cc,
                    cxx_override=args.cxx,
                    ld_override=args.ld,
                    state_dir=Path(args.state_dir) if args.state_dir else None,
                )
            run(pkgbuild, options=BuildOptions(
                extra_flags=extra_flags,
                interactive=args.interactive,
                pkg_log=not args.no_pkg_log,
                persist_log=args.persist_log,
                log_dir=Path(args.log_dir) if args.log_dir else None,
                profile_conf=args.profile_conf,
                cc_override=args.cc,
                cxx_override=args.cxx,
                ld_override=args.ld,
                init_session=(i == 0 and not aur_deps),
                cache_report=(args.cache_report and i == len(packages) - 1),
                update=not args.no_update,
                abi_check=args.abi_check,
                state_dir=Path(args.state_dir) if args.state_dir else None,
            ))
        return ExecResult()


class EnvVerb(Verb):
    """Read-only: print the inherited env chain and divergences."""

    name = "env"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.primitives.env_chain import collect_env_chain, format_env_chain
        print(format_env_chain(collect_env_chain(), verbosity=log.get_verbosity()))
        return ExecResult()


class CompletionsVerb(Verb):
    """Shell-completion data sink. Not user-facing; called from _sysforge."""

    name = "completions"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        import subprocess as _sp
        config = load_config() or {}

        if args.resource == "makepkg-flags":
            r = _sp.run(["makepkg", "--help"], capture_output=True, text=True)
            text = r.stdout or r.stderr or ""
            _exclude = {"-h", "--help", "-V", "--version", "-p", "-m", "--nocolor"}
            import re
            for line in text.splitlines():
                m = re.match(r"^\s+(-\w),\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
                if m:
                    short, long, desc = m.group(1), m.group(2), m.group(3).strip()
                    if short not in _exclude:
                        print(f"{short}:{desc}")
                    if long not in _exclude:
                        print(f"{long}:{desc}")
                    continue
                m = re.match(r"^\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
                if m:
                    long, desc = m.group(1), m.group(2).strip()
                    if long not in _exclude:
                        print(f"{long}:{desc}")
            return ExecResult()

        if args.resource == "state":
            from sysforge.pipeline.state import resolve_state_dir
            from sysforge.primitives.build_state import BuildState
            state_dir, _ = resolve_state_dir(None)
            bs = BuildState(state_dir)
            for name in sorted(bs.all_packages()):
                print(name)
            return ExecResult()

        if args.resource == "manifest":
            import tomllib as _tomllib
            pkg_path = resolve_packages_path(config)
            if pkg_path.exists():
                with open(pkg_path, "rb") as _f:
                    data = _tomllib.load(_f)
                for entry in data.get("package", []):
                    name = entry.get("name")
                    if name:
                        print(name)
            return ExecResult()

        if args.resource == "local":
            raw = config.get("paths", {}).get("pkgbuild_src_dir")
            if raw:
                d = Path(raw).expanduser()
                if d.is_dir():
                    for sub in sorted(d.iterdir()):
                        if sub.is_dir() and (sub / "PKGBUILD").exists():
                            print(sub.name)
            return ExecResult()

        seen: set[str] = set()
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            d = Path(raw).expanduser()
            if d.is_dir():
                for sub in sorted(d.iterdir()):
                    if sub.is_dir() and (sub / "PKGBUILD").exists():
                        if sub.name not in seen:
                            seen.add(sub.name)
                            print(sub.name)

        r = _sp.run(["pacman", "-Ssq"], capture_output=True, text=True)
        if r.returncode == 0:
            for name in r.stdout.splitlines():
                if name and name not in seen:
                    seen.add(name)
                    print(name)

        from sysforge.primitives.aur import AUR_CACHE_PATH
        aur_cache = AUR_CACHE_PATH.expanduser()
        if aur_cache.exists():
            for name in aur_cache.read_text().splitlines():
                if name and name not in seen:
                    seen.add(name)
                    print(name)
        return ExecResult()


# ---------------------------------------------------------------------------
# run namespace verbs — thin shims onto the pipeline runner.
#
# These verbs do NOT install a verb-level sentinel: the pipeline framework
# (and the stages themselves, e.g. toolchain via sentinel_scope) owns its
# sentinel coverage. Wrapping the verb in another sentinel_scope would race
# with the inner stage's sentinel against the same stage_in_progress.toml.
# ---------------------------------------------------------------------------

class _RunVerbBase(Verb):
    """Common scaffolding for ``sysforge run <stage>`` verbs."""

    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()


class RunPipelineVerb(_RunVerbBase):
    name = "run-pipeline"

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_pipeline
        from sysforge.pipeline.stages.base import RunOptions
        config = _load_config_with_overrides(args)
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
        config = _load_config_with_overrides(args)
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
        )
        run_stage_standalone(ToolchainStage(), config, options)
        return ExecResult()


class RunPackagesVerb(_RunVerbBase):
    name = "run-packages"

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        from sysforge.pipeline.runner import run_stage_standalone
        from sysforge.pipeline.stages.base import RunOptions
        from sysforge.pipeline.stages.packages import PackagesStage
        config = _load_config_with_overrides(args)
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
        )
        run_stage_standalone(KernelStage(), config, options)
        return ExecResult()


# ---------------------------------------------------------------------------
# Argv preprocessing
# ---------------------------------------------------------------------------

def _hoist_verbosity_flags(argv):
    """
    Move any -v / -vv / --verbose flags to before the subcommand so argparse
    sees them as global flags regardless of where the user placed them.

    sysforge build PKGBUILD -vv  →  sysforge -vv build PKGBUILD
    sysforge build PKGBUILD -v --interactive  →  sysforge -v build PKGBUILD --interactive
    """
    verbose_tokens = []
    rest = []
    for tok in argv:
        if tok in ("-v", "-vv", "-vvv", "--verbose"):
            verbose_tokens.append(tok)
        else:
            rest.append(tok)
    return verbose_tokens + rest


# Flags that sysforge handles or that take a value arg — exclude from implicit
# passthrough.  -v is already stripped by _hoist_verbosity_flags.
_PASSTHROUGH_EXCLUDE = frozenset("hVpmD")

# Subcommands that accept makepkg flag passthrough.
_MAKEPKG_SUBCOMMANDS = frozenset({"build", "update", "converge"})


def _extract_implicit_makepkg_flags(argv):
    """
    Detect bare makepkg-style short flags on build/update/converge and rewrite
    them into explicit ``-m`` form so the rest of the pipeline handles them
    uniformly.

    sysforge build ventoy -sfCci  →  sysforge build ventoy -m -sfCci

    A token qualifies when it starts with ``-`` (not ``--``), is longer than
    one character, and every letter after the dash is a valid makepkg short
    flag not in _PASSTHROUGH_EXCLUDE.  If ``-m`` / ``--makepkg`` is already
    present the implicit flags are still collected and merged.
    """
    sub_idx = None
    for i, tok in enumerate(argv):
        if tok in _MAKEPKG_SUBCOMMANDS:
            sub_idx = i
            break
    if sub_idx is None:
        return argv

    before = list(argv[:sub_idx + 1])
    implicit = []
    rest = []
    for tok in argv[sub_idx + 1:]:
        if (
            tok.startswith("-")
            and not tok.startswith("--")
            and len(tok) > 1
            and all(ch not in _PASSTHROUGH_EXCLUDE for ch in tok[1:])
        ):
            implicit.append(tok)
        else:
            rest.append(tok)

    if not implicit:
        return argv

    merged = "-" + "".join(tok[1:] for tok in implicit)

    for i, tok in enumerate(rest):
        if tok in ("-m", "--makepkg") and i + 1 < len(rest):
            rest[i + 1] = rest[i + 1] + " " + merged
            return before + rest
        if tok.startswith("--makepkg="):
            rest[i] = tok + " " + merged
            return before + rest

    return before + rest + ["-m", merged]


def _patch_makepkg_argv(argv):
    """
    Rewrite -m/-makepkg <value> to --makepkg=<value> when <value> starts with
    '-', so argparse doesn't misinterpret it as a new flag.

    argparse cannot accept option values that start with '-' unless they are
    expressed as --flag=value. This preprocessing step keeps the documented
    UX (sysforge build PKGBUILD -m '-sfci') working as intended.
    """
    result = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-m", "--makepkg") and i + 1 < len(argv):
            val = argv[i + 1]
            if val.startswith("-"):
                result.append(f"--makepkg={val}")
                i += 2
                continue
        result.append(tok)
        i += 1
    return result


# ---------------------------------------------------------------------------
# Subparser factories (keep main() readable)
# ---------------------------------------------------------------------------

def _add_build_parser(sub):
    p = sub.add_parser("build", help="Build a package from a PKGBUILD.")
    p.add_argument(
        "pkgbuilds", nargs="+", metavar="PKGBUILD",
        help="One or more packages to build (path, directory, or bare package name).",
    )
    p.add_argument("--makepkg", "-m", metavar="FLAGS",
        help="Additional makepkg flags, appended after profile makepkg_flags. "
             "Combined short flags are expanded: -sfci becomes -s -f -c -i. "
             "Example: sysforge build PKGBUILD -m '-sfci'",
    )
    p.add_argument("--interactive", action="store_true",
        help="Strip --noconfirm from profile makepkg_flags and hand stdout/stderr "
             "to makepkg's terminal so pacman conflict prompts and other "
             "unbuffered interactive output appear immediately (disables "
             "line-based output classification while set).")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep the per-package log file after a successful build.")
    p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
        help="Disable the per-package log file.")
    p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for the per-package log file (default: alongside the PKGBUILD).")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p.add_argument("--cc", metavar="COMPILER", dest="cc",
        help="Override CC (C compiler) for this build, e.g. --cc clang.")
    p.add_argument("--cxx", metavar="COMPILER", dest="cxx",
        help="Override CXX (C++ compiler) for this build, e.g. --cxx clang++.")
    p.add_argument("--ld", metavar="LINKER", dest="ld",
        help="Override linker for this build, e.g. --ld lld.")
    p.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary (ccache/sccache hit rates) after the build.")
    p.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before building.")
    p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge the package src dir and re-clone before building. "
             "Refuses (per package) if the existing clone has uncommitted changes, "
             "ahead-of-upstream commits, or no upstream tracking branch.")
    p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
        help="Like --cleansrc but bypasses the dirty/diverged guard and "
             "overwrites the local tree unconditionally. Use when the upstream "
             "rewrote history (e.g. Arch packaging repos force-push every release) "
             "and the local commits have no value to preserve.")
    p.add_argument("--no-llvm-preflight", action="store_true", dest="no_llvm_preflight",
        help="Suppress the LLVM source pre-flight summary.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.set_defaults(verb_cls=BuildVerb)


def _add_fetch_parser(sub):
    p = sub.add_parser("fetch",
        help="Download PKGBUILD(s) into pkgbuild_src_dir without building.")
    p.add_argument(
        "pkgs", nargs="+", metavar="PKG",
        help="One or more package names to download.",
    )
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase for packages that are already cloned.")
    p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge each package's src dir and re-clone. Refuses (per package) "
             "if the existing clone has uncommitted changes, ahead-of-upstream "
             "commits, or no upstream tracking branch.")
    p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
        help="Like --cleansrc but bypasses the dirty/diverged guard and "
             "overwrites the local tree unconditionally.")
    p.add_argument("--no-llvm-preflight", action="store_true", dest="no_llvm_preflight",
        help="Suppress the LLVM source pre-flight summary.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p.set_defaults(verb_cls=FetchVerb)


def _add_update_parser(sub):
    p = sub.add_parser("update",
        help="Check for and rebuild outdated sysforge-managed packages.")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would be rebuilt without doing it.")
    p.add_argument("--devel", action="store_true", dest="devel",
        help="Resolve and rebuild VCS packages (-git, -svn, -hg, -bzr) whose "
             "upstream HEAD has advanced past the installed version. "
             "Resolution runs makepkg --nobuild per VCS package; up-to-date "
             "packages are skipped, not rebuilt. Per-package upstream commits "
             "recorded in build_state are reused on subsequent runs to "
             "short-circuit the resolve via git ls-remote.")
    p.add_argument("--offline", action="store_true", dest="offline",
        help="No network: skip git pulls, clones, and AUR RPC. Pure local version check.")
    p.add_argument("--install-only", action="store_true", dest="install_only",
        help="Skip rebuild; install only those locally-built artifacts in PKGDEST that are "
             "newer than the installed version. Implies --offline. Mutually exclusive with "
             "--makepkg, --no-cleanbuild, --cleansrc, --cleansrc-force, --interactive, and "
             "--cache-report.")
    p.add_argument("--packages", metavar="FILE", dest="packages",
        help=f"Path to packages.toml for override rules (default: {PACKAGES_PATH}).")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
        help="Disable per-package log files.")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for per-package log files.")
    p.add_argument("--makepkg", "-m", metavar="FLAGS",
        help="Extra flags passed verbatim to makepkg (e.g. -m '-f' to force rebuild). "
             "Combined short flags are expanded: -sfci becomes -s -f -c -i.")
    p.add_argument("--interactive", action="store_true",
        help="Pause on build failures to allow manual correction (default: log failure and continue).")
    p.add_argument("--no-cleanbuild", action="store_true", dest="no_cleanbuild",
        help="Skip the automatic --cleanbuild (-C) added for update runs. "
             "Useful when packages are already built and you only need to re-run the install step.")
    p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge each package's src dir and re-clone before building. "
             "Per-package fatal if the clone has uncommitted changes, "
             "ahead-of-upstream commits, or no upstream — that package is "
             "reported failed and the run continues.")
    p.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
        help="Like --cleansrc but bypasses the dirty/diverged guard and "
             "overwrites every local tree unconditionally. Use when the "
             "upstream rewrote history (e.g. Arch packaging repos force-push "
             "every release) and local commits have no value to preserve.")
    p.add_argument("--no-llvm-preflight", action="store_true", dest="no_llvm_preflight",
        help="Suppress the LLVM source pre-flight summary.")
    p.add_argument("--no-toolchain-preflight", action="store_true", dest="no_toolchain_preflight",
        help="Skip the toolchain pre-flight (rust/cmake/meson availability + "
             "lib32 cross targets) that normally runs before the build loop.")
    p.add_argument("--include-stage-owned", action="store_true", dest="include_stage_owned",
        help="Include packages owned by a pipeline stage (e.g. the kernel "
             "stage's `linux-custom`). Skipped by default; the owning stage "
             "(`sysforge run kernel`) is the canonical update path.")
    p.add_argument("--explain-drift", action="store_true", dest="explain_drift",
        help="List packages whose recorded toolchain_variant differs from "
             "the currently active toolchain (gcc / stock_llvm / pgo_llvm) "
             "and exit. Informational; no source sync, no rebuild.")
    p.add_argument("--rebuild-on-toolchain-drift", action="store_true",
        dest="rebuild_on_toolchain_drift",
        help="Treat toolchain-variant drift as an upgrade trigger: packages "
             "built under a different toolchain than is active now are added "
             "to the rebuild queue. Off by default — drift is reported but "
             "not acted on, since most C/C++ packages don't measurably "
             "benefit from a re-stamp.")
    p.add_argument("pkgnames", metavar="PKG", nargs="*",
        help="Limit update to these package names (default: all sysforge-managed packages).")
    p.set_defaults(verb_cls=UpdateVerb)


def _add_resolve_parser(sub):
    p = sub.add_parser("resolve",
        help="Show which profile would be applied to a package and why.")
    p.add_argument("pkg", metavar="PKG",
        help="Path to a PKGBUILD file, or bare package name.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--show-flags", action="store_true", dest="show_flags",
        help="Print the full resolved flag set.")
    mode.add_argument("--deps", action="store_true",
        help="Show transitive dependency tree with build order instead of profile info.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p.set_defaults(verb_cls=ResolveVerb)


def _add_converge_parser(sub):
    p = sub.add_parser("converge",
        help="Detect and repair packages whose build flags have drifted from the current profile.")
    p.add_argument("--apply", action="store_true",
        help="Rebuild all DRIFTED packages with the current profile.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
        help="Disable per-package log files (only relevant with --apply).")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion (only relevant with --apply).")
    p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for per-package log files (only relevant with --apply).")
    p.add_argument("--makepkg", "-m", metavar="FLAGS",
        help="Extra flags passed to makepkg during --apply rebuilds (e.g. -m '-C' to cleanbuild). "
             "-f is always injected automatically.")
    p.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after --apply runs.")
    p.add_argument("--no-llvm-preflight", action="store_true", dest="no_llvm_preflight",
        help="Suppress the LLVM source pre-flight summary.")
    p.add_argument("pkgnames", metavar="PKG", nargs="*",
        help="Limit drift check to these package names (default: all build_state-recorded packages).")
    p.set_defaults(verb_cls=ConvergeVerb)


def _add_doctor_parser(sub):
    p = sub.add_parser("doctor",
        help="Health-check installed package depends + shared-library linkage.")
    p.add_argument("packages", nargs="*", metavar="PKG",
        help="One or more installed package names to verify. "
             "Without any PKG/--graphics/--all, the command exits with usage.")
    p.add_argument("--graphics", action="store_true",
        help="Expand to the graphics stack (mesa, vulkan, libglvnd, wayland, "
             "libdrm, libva, libvdpau, egl-wayland, xwayland, gamescope, plus "
             "per-vendor drivers from the hardware overlay's gpu_vendors) AND "
             "run system-state probes (nvidia-drm modeset, driver version skew, "
             "Wayland explicit-sync protocol, Steam GPU accel, session type).")
    p.add_argument("--all", action="store_true", dest="all",
        help="Verify every installed package — foreign and non-foreign (pacman -Q). "
             "Slow but comprehensive.")
    p.add_argument("--repo", action="store_true", dest="repo",
        help="Verify every non-foreign (native repo) package. Narrower than --all.")
    p.add_argument("--shallow", action="store_true",
        help="Do not recurse into transitive dependencies of each target.")
    p.add_argument("--quiet", "-q", action="store_true",
        help="Suppress clean lines; print only packages with issues.")
    p.add_argument("--suggest", "-s", action="store_true",
        help="For each unsatisfied soname, look up candidate packages "
             "via `pacman -Fq`. Findings split into 'install candidates' "
             "(missing from disk — install the package), 'rebuild "
             "candidates' (installed; rebuild against the current system), "
             "and 'ABI-drift candidates' (present but one of their "
             "versioned symbols no longer resolves — rebuild or upgrade, "
             "not reinstall). Requires a synced files db (`sudo pacman -Fy`).")
    p.add_argument("--apply", action="store_true",
        help="Hand the rebuild candidates from --suggest off to `sysforge "
             "update` and rebuild them. Implies --suggest. Drift-rebuild only "
             "in v1.x — install candidates are printed but not invoked.")
    p.add_argument("--no-confirm", action="store_true", dest="no_confirm",
        help="Skip the y/N prompt before --apply rebuilds.")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Report what --apply would rebuild without invoking the build.")
    p.set_defaults(verb_cls=DoctorVerb)


def _add_packages_parser(sub):
    """packages namespace: list (default) / add / remove."""
    p = sub.add_parser("packages",
        help="Manage packages.toml override entries (list, add, remove).")
    # --packages on the parent so bare 'sysforge packages' and
    # 'sysforge packages --packages foo.toml' both work
    p.add_argument("--packages", metavar="FILE", dest="packages",
        help=_PACKAGES_HELP)
    p.add_argument("--orphans", action="store_true", dest="orphans",
        help="With list: show only entries whose package is not currently installed.")
    p.set_defaults(verb_cls=PackagesListVerb)

    pkg_sub = p.add_subparsers(dest="packages_cmd")

    # list
    p_list = pkg_sub.add_parser("list", help="Show override entries in packages.toml.")
    p_list.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_list.add_argument("--orphans", action="store_true", dest="orphans",
        help="Show only entries whose package is not currently installed.")
    p_list.set_defaults(verb_cls=PackagesListVerb)

    # add
    p_add = pkg_sub.add_parser("add",
        help="Add or update an override entry. Requires at least one of "
             "--pkgbuild-patch / --no-cache / --reason.")
    p_add.add_argument("pkg", metavar="PKG", help="Package name to add or update.")
    p_add.add_argument("--source", choices=("repo", "aur", "local"), dest="source",
        help="Pin routing (metadata; doesn't satisfy validation on its own). "
             "`local` marks a hand-maintained PKGBUILD with no remote to sync from.")
    p_add.add_argument("--pkgbuild-patch", action="store_true", dest="pkgbuild_patch",
        help="Patch PKGBUILD flags before build.")
    p_add.add_argument("--no-cache", action="store_true", dest="no_cache",
        help="Disable ccache/sccache for this package (required for PGO).")
    p_add.add_argument("--reason", metavar="TEXT", dest="reason",
        help="Free-form note attached to the entry.")
    p_add.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_add.set_defaults(verb_cls=PackagesAddVerb)

    # remove
    p_remove = pkg_sub.add_parser("remove", help="Remove an override entry.")
    p_remove.add_argument("pkg", metavar="PKG", help="Package name to remove.")
    p_remove.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_remove.set_defaults(verb_cls=PackagesRemoveVerb)


def _add_state_parser(sub):
    """state namespace: list / repair — operates on build_state.toml."""
    p = sub.add_parser("state",
        help="Inspect or repair build_state.toml (live install-state mirror).")
    state_sub = p.add_subparsers(dest="state_cmd")
    state_sub.required = True

    p_list = state_sub.add_parser("list", help="Tabulate build_state.toml entries.")
    p_list.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_list.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
    p_list.set_defaults(verb_cls=StateListVerb)

    p_repair = state_sub.add_parser("repair",
        help="Re-parse PKGBUILDs to rewrite build_state.toml entries that contain "
             "unexpanded shell variables (e.g. '$_pkgname-git').")
    p_repair.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_repair.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show the planned repair without writing.")
    p_repair.set_defaults(verb_cls=StateRepairVerb)

    p_orphans = state_sub.add_parser("orphans",
        help="List (and optionally prune) stale .pkg.tar* artifacts in PKGDEST. "
             "Only surfaces superseded files (pkgname installed AND artifact "
             "older than installed) so --prune is always safe.")
    p_orphans.add_argument("--prune", action="store_true",
        help="Delete the listed superseded artifacts (prompts for confirmation).")
    p_orphans.add_argument("--no-confirm", action="store_true", dest="no_confirm",
        help="Skip the y/N prompt when pruning. Implies --prune.")
    p_orphans.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
    p_orphans.set_defaults(verb_cls=StateOrphansVerb)


def _add_setup_parser(sub):
    p = sub.add_parser("setup",
        help="Configure system integration (pacman IgnoreGroup for sf-build).")
    p.add_argument("--pacman-conf", metavar="FILE", dest="pacman_conf",
        help="Path to pacman.conf (default: /etc/pacman.conf).")
    p.set_defaults(verb_cls=SetupVerb)


def _add_env_parser(sub):
    p = sub.add_parser("env",
        help="Print the inherited env chain, all contributing sources "
             "(shell init files, systemd-user, PAM env, sysforge "
             "[defaults] profile), and a mismatches block when sources "
             "disagree. -vv adds inline per-var divergence annotations.")
    p.set_defaults(verb_cls=EnvVerb)


def _add_log_parser(sub):
    p = sub.add_parser("log",
        help="Page the unified or per-package sysforge log through $PAGER.")
    p.add_argument("pkg", nargs="?", metavar="PKG",
        help="Package name (resolves to <pkgbuild_src_dir>/<pkg>/sysforge_<pkg>.log). "
             "Omit to page the unified log.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory (used to locate the unified log).")
    p.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
    p.set_defaults(verb_cls=LogVerb)


def _add_run_parser(sub):
    """run namespace: pipeline / reconfigure / toolchain / packages / kernel"""
    p = sub.add_parser("run",
        help="Execute a pipeline stage (pipeline, hardware, reconfigure, toolchain, packages, kernel).")
    run_sub = p.add_subparsers(dest="run_stage", metavar="STAGE")
    run_sub.required = True

    # run pipeline
    p_pipeline = run_sub.add_parser("pipeline",
        help="Run the full install pipeline (stages 1–8).")
    p_pipeline.add_argument("--resume", action="store_true",
        help="Resume from the last checkpoint.")
    p_pipeline.add_argument("--start-from", metavar="STAGE", dest="start_from",
        help="Start from this stage, marking all prior stages as skipped. "
             "Useful on a live system: --start-from reconfigure")
    p_pipeline.add_argument("--force-retry", action="store_true", dest="force_retry",
        help="Retry all failed packages without prompting.")
    p_pipeline.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_pipeline.add_argument("--packages", metavar="FILE",
        help=_PACKAGES_HELP)
    p_pipeline.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_pipeline.add_argument("--no-unified-log", action="store_true", dest="no_unified_log",
        help="Disable the unified log file.")
    p_pipeline.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.")
    p_pipeline.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.")
    p_pipeline.add_argument("--purge-log", action="store_true", dest="purge_log",
        help="Truncate the unified log before this run.")
    p_pipeline.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_pipeline.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p_pipeline.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the pipeline completes.")
    p_pipeline.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_pipeline.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_pipeline.set_defaults(verb_cls=RunPipelineVerb)

    # run hardware
    p_hw = run_sub.add_parser("hardware",
        help="Re-run hardware detection and refresh hardware_profile.toml.")
    p_hw.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would be written without writing.")
    p_hw.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_hw.set_defaults(verb_cls=RunHardwareVerb)

    # run reconfigure
    p_reconf = run_sub.add_parser("reconfigure",
        help="Pre-build checkpoint: review configs, disk, network, GPG, build preview.")
    p_reconf.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Run all steps non-interactively without writing any changes.")
    p_reconf.add_argument("--packages", metavar="FILE",
        help="Path to packages.toml (used by disk and preview steps).")
    p_reconf.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_reconf.set_defaults(verb_cls=RunReconfigureVerb)

    # run toolchain
    p_toolchain = run_sub.add_parser("toolchain",
        help="Build and install the LLVM/GCC toolchain from toolchain.toml.")
    p_toolchain.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_toolchain.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_toolchain.add_argument("--makepkg", "-m", metavar="FLAGS",
        help="Additional makepkg flags appended to each build. "
             "Example: -m '-f' to force rebuild of already-built packages. "
             "Install flags (-i/--install) are ignored; the toolchain controls "
             "which passes install to the system.")
    p_toolchain.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_toolchain.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p_toolchain.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_toolchain.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_toolchain.add_argument("--rebuild-profdata", action="store_true", dest="rebuild_profdata",
        help="Force a full 3-pass PGO build even if compatible profdata already exists.")
    p_toolchain.add_argument("--auto-pgo", action="store_true", dest="auto_pgo",
        help="Bypass the PGO confirmation prompts (profdata reuse, staging/pgo_store "
             "purge, 3-pass start, suspicious profdata size). Required for non-interactive "
             "PGO runs; without it, a non-TTY invocation aborts the PGO sub-flow because PGO "
             "is fragile and silent mis-optimisation is the failure mode.")
    p_toolchain.add_argument("--allow-dirty-llvm", action="store_true", dest="allow_dirty_llvm",
        help="Bypass the LLVM safety pre-flight refusal on dirty or diverged "
             "trees. PGO profdata version mismatches cannot be bypassed. "
             "Note: this only suppresses the refusal — it does not modify the "
             "tree. Use --cleansrc-force to actually overwrite the local "
             "trees with upstream.")
    p_toolchain.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge each toolchain package's src dir and re-clone before "
             "building. Refuses (per package) if the existing clone has "
             "uncommitted changes, ahead-of-upstream commits, or no upstream.")
    p_toolchain.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
        help="Like --cleansrc but bypasses the dirty/diverged guard and "
             "overwrites every local toolchain tree unconditionally. Use when "
             "the upstream rewrote history (e.g. Arch packaging repos "
             "force-push every release) and local commits have no value.")
    p_toolchain.set_defaults(verb_cls=RunToolchainVerb)

    # run packages
    p_pkgs = run_sub.add_parser("packages",
        help="Build and install non-kernel packages from packages.toml.")
    p_pkgs.add_argument("--packages", metavar="FILE",
        help=_PACKAGES_HELP)
    p_pkgs.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_pkgs.add_argument("--force-retry", action="store_true", dest="force_retry",
        help="Retry all failed packages without prompting.")
    p_pkgs.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_pkgs.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.")
    p_pkgs.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_pkgs.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.")
    p_pkgs.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p_pkgs.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_pkgs.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_pkgs.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p_pkgs.set_defaults(verb_cls=RunPackagesVerb)

    # run kernel
    p_kernel = run_sub.add_parser("kernel",
        help="Build and install the custom kernel configured in kernel.toml.")
    p_kernel.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_kernel.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_kernel.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge the kernel src dir and re-clone before building. "
             "Refuses if the existing clone has uncommitted changes, "
             "ahead-of-upstream commits, or no upstream.")
    p_kernel.add_argument("--cleansrc-force", action="store_true", dest="cleansrc_force",
        help="Like --cleansrc but bypasses the dirty/diverged guard and "
             "overwrites the local tree unconditionally.")
    p_kernel.add_argument("--non-interactive", action="store_true", dest="non_interactive",
        help="Disable interactive kconfig (default is interactive — the PKGBUILD's "
             "`make nconfig`/`menuconfig`/etc. runs as written). With this flag, "
             "interactive targets are patched to `make olddefconfig` for unattended runs.")
    p_kernel.add_argument("--compiler", choices=["gcc", "llvm"], dest="compiler",
        help="Kernel-stage compiler override (gcc or llvm). Independent of the "
             "global toolchain stage — lets you keep gcc system-wide but build "
             "the kernel with LLVM (or vice versa). Resolution order: this flag > "
             "kernel.toml compiler > toolchain-stage pipeline state.")
    p_kernel.add_argument("--bootloader", choices=["systemd-boot", "grub", "none"], dest="bootloader",
        help="Override kernel.toml bootloader for this invocation (systemd-boot is the default).")
    p_kernel.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.")
    p_kernel.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_kernel.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.")
    p_kernel.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p_kernel.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_kernel.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_kernel.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a profiles.toml to use instead of the default.")
    p_kernel.set_defaults(verb_cls=RunKernelVerb)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Return the top-level ArgumentParser. Called by main() and by argparse-manpage."""
    from sysforge import __version__
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Arch Linux AUR helper with compiler-optimized builds.",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"sysforge {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help=(
            "Verbosity level. Default: errors only. "
            "-v adds warnings. -vv adds informational messages. "
            "-vvv adds debug output."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    _add_build_parser(sub)
    _add_fetch_parser(sub)
    _add_update_parser(sub)
    _add_resolve_parser(sub)
    _add_converge_parser(sub)
    _add_doctor_parser(sub)
    _add_packages_parser(sub)
    _add_state_parser(sub)
    _add_run_parser(sub)
    _add_setup_parser(sub)
    _add_env_parser(sub)
    _add_log_parser(sub)

    # completions (used by shell completion scripts; not user-facing)
    p_completions = sub.add_parser("completions")
    p_completions.add_argument(
        "resource",
        choices=["packages", "manifest", "local", "state", "makepkg-flags"],
    )
    p_completions.set_defaults(verb_cls=CompletionsVerb)

    return parser


_INSTALL_BEARING_COMMANDS = frozenset(
    {"build", "update", "converge", "run", "setup"}
)


def _gate_sentinel_check(args) -> bool:
    """True when ``cli.main`` should call ``check_and_recover_stale_sentinel``.

    Install-bearing commands (``build``/``update``/``converge``/``run``/
    ``setup``) gate on a stale sentinel, except when the invocation is
    explicitly read-only (``--dry-run``). The inner verb-runner sentinel
    scope already opts out under ``--dry-run`` (see ``UpdateVerb.pre_check``);
    keeping the outer CLI gate in lockstep avoids blocking ``sysforge update
    --dry-run`` on a sentinel from an earlier mutating run the user is still
    investigating.
    """
    cmd = getattr(args, "command", None)
    if cmd not in _INSTALL_BEARING_COMMANDS:
        return False
    if getattr(args, "dry_run", False):
        return False
    return True


def main():
    from sysforge.primitives.paths import migrate_legacy_user_dirs
    from sysforge.primitives.resource_guard import install as _install_resource_guard
    _install_resource_guard()
    migrate_legacy_user_dirs()
    sys.argv[1:] = _patch_makepkg_argv(
        _extract_implicit_makepkg_flags(_hoist_verbosity_flags(sys.argv[1:]))
    )
    parser = _build_parser()
    args = parser.parse_args()
    log.set_verbosity(args.verbose)
    if getattr(args, "dry_run", False):
        log.set_dry_run_mode()
    # Snapshot inherited env at startup — DEBUG-only on stderr (-vvv), always
    # written to the file log. Skipped for the `env` verb to avoid duplicating
    # the output it explicitly prints. Best-effort; never fail startup over it.
    if getattr(args, "command", None) != "env":
        try:
            from sysforge.primitives.env_chain import log_env_chain
            log_env_chain("debug")
        except Exception as _e:  # pragma: no cover - defensive
            log.debug("[ENV]", f"env-chain snapshot failed: {_e}")
    from sysforge.ui import progress
    progress.init()
    # Stale stage-in-progress detection: if a previous install-bearing run
    # (toolchain/kernel/packages) was interrupted before clearing its
    # sentinel, block the next mutating command and offer recovery before
    # proceeding. Read-only commands (env, doctor, resolve, fetch, list,
    # completions) skip the check so users can inspect without recovery.
    # Read-only invocations of install-bearing verbs (e.g. `update --dry-run`)
    # also skip — the inner verb has already opted out of its own sentinel
    # scope, so the entry gate matching that semantics keeps the two in sync.
    if _gate_sentinel_check(args):
        from sysforge.primitives.stage_sentinel import check_and_recover_stale_sentinel
        state_dir = getattr(args, "state_dir", None)
        if not check_and_recover_stale_sentinel(state_dir):
            log.error(
                "[SENTINEL]",
                "Stale stage-in-progress sentinel present; refusing to proceed. "
                "Run the recovery command shown above, or remove the sentinel "
                "file once you have manually verified system consistency.",
            )
            sys.exit(2)
    verb_cls = getattr(args, "verb_cls", None)
    if verb_cls is None:
        _log.error("No verb dispatcher set for this command — argparse misconfiguration")
        sys.exit(2)
    sys.exit(run_verb(verb_cls(), args))
