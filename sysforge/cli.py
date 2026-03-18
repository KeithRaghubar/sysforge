"""
cli.py — SysForge command-line interface

Top-level commands:
    sysforge build <pkg>    Build a package using its matched profile
    sysforge update         Check for and rebuild outdated sysforge-managed packages
    sysforge resolve <pkg>  Show which profile would be applied to a package
    sysforge manifest       Generate a packages.toml stub from a list of names
    sysforge converge       [planned] Rebuild packages that have drifted from their profile

Namespaces:
    sysforge packages       Manage packages.toml (list / add / remove / sync)
    sysforge run            Execute pipeline stages (pipeline / reconfigure / toolchain / packages / kernel)
"""
import argparse
import sys
from pathlib import Path
import sysforge.log as _log

from sysforge.manifest import cmd_manifest
from sysforge.resolve import cmd_resolve
from sysforge.update import cmd_update
from sysforge.packages_cmd import (
    cmd_packages_list,
    cmd_packages_add,
    cmd_packages_remove,
    cmd_packages_sync,
)

from sysforge.primitives.makepkg_wrapper import run
from sysforge.primitives.config import load_config


def _expand_makepkg_flags(flags_str):
    """
    Split a makepkg flags string into a list of individual flags,
    expanding combined short flags like '-sfci' into ['-s', '-f', '-c', '-i'].
    Long flags (--noconfirm) and flags with values are passed through as-is.
    """
    if not flags_str:
        return []
    result = []
    for token in flags_str.split():
        if token.startswith("--"):
            result.append(token)
        elif token.startswith("-") and len(token) > 2:
            # Combined short flags e.g. -sfci → -s -f -c -i
            result.extend(f"-{ch}" for ch in token[1:])
        else:
            result.append(token)
    return result


def _cmd_build(args):
    extra_flags = _expand_makepkg_flags(args.makepkg) if args.makepkg else None
    if args.no_pkg_log and args.log_dir:
        print("[SYSFORGE] Warning: --log-dir has no effect when --no-pkg-log is set.", file=sys.stderr)
    packages = args.pkgbuilds
    try:
        for i, pkg in enumerate(packages):
            run(pkg,
                extra_flags=extra_flags,
                interactive=args.interactive,
                pkg_log=not args.no_pkg_log,
                persist_log=args.persist_log,
                log_dir=Path(args.log_dir) if args.log_dir else None,
                profile_conf=args.profile_conf,
                cc_override=args.cc,
                cxx_override=args.cxx,
                ld_override=args.ld,
                init_session=(i == 0),
                cache_report=(args.cache_report and i == len(packages) - 1),
                update=not args.no_update,
                state_dir=Path(args.state_dir) if args.state_dir else None)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_update(args):
    try:
        cmd_update(args)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_converge(args):
    # TODO: compare installed state in /var/lib/sysforge/build_state.toml
    # against current manifest and flag profiles; rebuild drifted packages.
    print("[SYSFORGE] 'converge' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_completions(args):
    import subprocess as _sp
    config = load_config() or {}
    seen: set[str] = set()

    # Local packages from pkgbuild_dir
    raw = config.get("paths", {}).get("pkgbuild_dir")
    if raw:
        d = Path(raw).expanduser()
        if d.is_dir():
            for sub in sorted(d.iterdir()):
                if sub.is_dir() and (sub / "PKGBUILD").exists():
                    if sub.name not in seen:
                        seen.add(sub.name)
                        print(sub.name)

    # Pacman sync DB packages
    r = _sp.run(["pacman", "-Ssq"], capture_output=True, text=True)
    if r.returncode == 0:
        for name in r.stdout.splitlines():
            if name and name not in seen:
                seen.add(name)
                print(name)


# ---------------------------------------------------------------------------
# run namespace handlers
# ---------------------------------------------------------------------------

def _cmd_run_pipeline(args):
    from sysforge.pipeline.runner import run_pipeline
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}
    if args.packages:
        config["packages_file"] = args.packages
    if getattr(args, "profile_conf", None):
        config["profile_conf"] = args.profile_conf

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
        no_update=args.no_update,
    )
    run_pipeline(config, options)


def _cmd_run_reconfigure(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.reconfigure import ReconfigureStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages

    options = RunOptions(
        dry_run=args.dry_run,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        no_unified_log=True,
        no_pkg_logs=True,
    )
    run_stage_standalone(ReconfigureStage(), config, options)


def _cmd_run_toolchain(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.toolchain import ToolchainStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}

    options = RunOptions(
        dry_run=args.dry_run,
        no_update=args.no_update,
        cache_report=args.cache_report,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        persist_log=args.persist_log,
    )
    run_stage_standalone(ToolchainStage(), config, options)


def _cmd_run_packages(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.packages import PackagesStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}
    if args.packages:
        config["packages_file"] = args.packages
    if getattr(args, "profile_conf", None):
        config["profile_conf"] = args.profile_conf

    options = RunOptions(
        dry_run=args.dry_run,
        force_retry=args.force_retry,
        no_update=args.no_update,
        no_pkg_logs=args.no_pkg_logs,
        persist_log=args.persist_log,
        log_dir=Path(args.log_dir) if args.log_dir else None,
        cache_report=args.cache_report,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    run_stage_standalone(PackagesStage(), config, options)


def _cmd_run_kernel(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.kernel import KernelStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}
    if args.packages:
        config["packages_file"] = args.packages

    options = RunOptions(
        dry_run=args.dry_run,
        no_update=args.no_update,
        no_pkg_logs=args.no_pkg_logs,
        persist_log=args.persist_log,
        log_dir=Path(args.log_dir) if args.log_dir else None,
        cache_report=args.cache_report,
        state_dir=Path(args.state_dir) if args.state_dir else None,
    )
    run_stage_standalone(KernelStage(), config, options)


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
        help="Strip --noconfirm from profile makepkg_flags.")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep the per-package log file after a successful build.")
    p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
        help="Disable the per-package log file.")
    p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for the per-package log file (default: alongside the PKGBUILD).")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
    p.add_argument("--cc", metavar="COMPILER", dest="cc",
        help="Override CC (C compiler) for this build, e.g. --cc clang.")
    p.add_argument("--cxx", metavar="COMPILER", dest="cxx",
        help="Override CXX (C++ compiler) for this build, e.g. --cxx clang++.")
    p.add_argument("--ld", metavar="LINKER", dest="ld",
        help="Override linker for this build, e.g. --ld lld.")
    p.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary (ccache/sccache hit rates) after the build.")
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before building.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.set_defaults(func=_cmd_build)


def _add_update_parser(sub):
    p = sub.add_parser("update",
        help="Check for and rebuild outdated sysforge-managed packages.")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would be rebuilt without doing it.")
    p.add_argument("--devel", action="store_true", dest="devel",
        help="Include VCS packages (-git, -svn, -hg, -bzr) in the rebuild.")
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before checking versions.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
    p.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p.add_argument("--no-pkg-log", action="store_true", dest="no_pkg_log",
        help="Disable per-package log files.")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for per-package log files.")
    p.set_defaults(func=_cmd_update)


def _add_resolve_parser(sub):
    p = sub.add_parser("resolve",
        help="Show which profile would be applied to a package and why.")
    p.add_argument("pkg", metavar="PKG",
        help="Path to a PKGBUILD file, or bare package name.")
    p.add_argument("--show-flags", action="store_true", dest="show_flags",
        help="Print the full resolved flag set.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
    p.set_defaults(func=cmd_resolve)


def _add_manifest_parser(sub):
    p = sub.add_parser("manifest",
        help="Generate a packages.toml stub from a list of package names.")
    p.add_argument("packages", nargs="*", metavar="PKG",
        help="Package names to include.")
    p.add_argument("--file", "-f", metavar="FILE",
        help="Text file with one package name per line.")
    p.set_defaults(func=cmd_manifest)


def _add_converge_parser(sub):
    p = sub.add_parser("converge",
        help="[planned] Rebuild packages that have drifted from their profile.")
    p.add_argument("--dry-run", action="store_true",
        help="Show what would be rebuilt without doing it.")
    p.set_defaults(func=_cmd_converge)


def _add_packages_parser(sub):
    """packages namespace: list (default) / add / remove / sync"""
    p = sub.add_parser("packages",
        help="Manage packages.toml (list, add, remove, sync).")
    # --packages on the parent so bare 'sysforge packages' and
    # 'sysforge packages --packages foo.toml' both work
    p.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).")
    p.set_defaults(func=cmd_packages_list)

    pkg_sub = p.add_subparsers(dest="packages_cmd")

    # list
    p_list = pkg_sub.add_parser("list", help="Show packages in packages.toml.")
    p_list.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_list.set_defaults(func=cmd_packages_list)

    # add
    p_add = pkg_sub.add_parser("add",
        help="Add a package: classify source, infer pkgbuild_patch, append entry.")
    p_add.add_argument("pkgs", nargs="+", metavar="PKG", help="One or more package names to add.")
    p_add.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_add.set_defaults(func=cmd_packages_add)

    # remove
    p_remove = pkg_sub.add_parser("remove", help="Remove a package entry.")
    p_remove.add_argument("pkg", metavar="PKG", help="Package name to remove.")
    p_remove.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_remove.set_defaults(func=cmd_packages_remove)

    # sync
    p_sync = pkg_sub.add_parser("sync",
        help="Re-validate inferable fields (source, pkgbuild_patch) for all entries.")
    p_sync.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_sync.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would change without writing.")
    p_sync.set_defaults(func=cmd_packages_sync)


def _add_run_parser(sub):
    """run namespace: pipeline / reconfigure / toolchain / packages / kernel"""
    p = sub.add_parser("run",
        help="Execute a pipeline stage (pipeline, reconfigure, toolchain, packages, kernel).")
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
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).")
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
        help="Path to a flag_profiles.toml to use instead of the default.")
    p_pipeline.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the pipeline completes.")
    p_pipeline.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_pipeline.set_defaults(func=_cmd_run_pipeline)

    # run reconfigure
    p_reconf = run_sub.add_parser("reconfigure",
        help="Pre-build checkpoint: review configs, disk, network, GPG, build preview.")
    p_reconf.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Run all steps non-interactively without writing any changes.")
    p_reconf.add_argument("--packages", metavar="FILE",
        help="Path to packages.toml (used by disk and preview steps).")
    p_reconf.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_reconf.set_defaults(func=_cmd_run_reconfigure)

    # run toolchain
    p_toolchain = run_sub.add_parser("toolchain",
        help="Build and install the LLVM/GCC toolchain from toolchain.toml.")
    p_toolchain.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_toolchain.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_toolchain.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_toolchain.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p_toolchain.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_toolchain.set_defaults(func=_cmd_run_toolchain)

    # run packages
    p_pkgs = run_sub.add_parser("packages",
        help="Build and install non-kernel packages from packages.toml.")
    p_pkgs.add_argument("--packages", metavar="FILE",
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).")
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
    p_pkgs.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_pkgs.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
    p_pkgs.set_defaults(func=_cmd_run_packages)

    # run kernel
    p_kernel = run_sub.add_parser("kernel",
        help="Build and install the custom kernel configured in kernel.toml.")
    p_kernel.add_argument("--packages", metavar="FILE",
        help="Path to packages.toml.")
    p_kernel.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.")
    p_kernel.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_kernel.add_argument("--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.")
    p_kernel.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.")
    p_kernel.add_argument("--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.")
    p_kernel.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.")
    p_kernel.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_kernel.set_defaults(func=_cmd_run_kernel)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    sys.argv[1:] = _hoist_verbosity_flags(_patch_makepkg_argv(sys.argv[1:]))
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Arch Linux AUR helper with compiler-optimized builds.",
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
    _add_update_parser(sub)
    _add_resolve_parser(sub)
    _add_manifest_parser(sub)
    _add_converge_parser(sub)
    _add_packages_parser(sub)
    _add_run_parser(sub)

    # completions (used by shell completion scripts; not user-facing)
    p_completions = sub.add_parser("completions", help=argparse.SUPPRESS)
    p_completions.add_argument("resource", choices=["packages"])
    p_completions.set_defaults(func=_cmd_completions)

    args = parser.parse_args()
    _log.set_verbosity(args.verbose)
    args.func(args)
