"""
cli.py — SysForge command-line interface

Implemented commands:
    sysforge build <PKGBUILD>   Build a single package using its matched profile
    sysforge manifest           Generate a packages.toml stub from a list of names
    sysforge update             Check for and rebuild outdated sysforge-managed packages

Stubbed commands (not yet implemented):
    sysforge pipeline           Full install pipeline (stub — not yet implemented)
    sysforge converge           Rebuild packages whose profile, flags, or version have drifted
    sysforge resolve <pkg>      Show which profile would be applied to a package and why
"""
import argparse
import sys
from pathlib import Path
import sysforge.log as _log

from sysforge.manifest import cmd_manifest
from sysforge.resolve import cmd_resolve
from sysforge.update import cmd_update

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


def _cmd_packages(args):
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


def _cmd_reconfigure(args):
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


def _cmd_toolchain(args):
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


def _cmd_kernel(args):
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


def _cmd_pipeline(args):
    # TODO: implement full pipeline stages (partition, base_install, hardware,
    # toolchain, packages, kernel, configure).
    print("[SYSFORGE] 'pipeline' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_completions(args):
    import subprocess as _sp
    config = load_config()
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


def _cmd_converge(args):
    # TODO: compare installed state in /var/lib/sysforge/build_state.toml
    # against current manifest and flag profiles; rebuild drifted packages.
    # --dry-run should show what would be rebuilt without doing it.
    print("[SYSFORGE] 'converge' is not yet implemented.", file=sys.stderr)
    sys.exit(1)


def _cmd_update(args):
    try:
        cmd_update(args)
    except RuntimeError as e:
        print(f"[SYSFORGE] Fatal: {e}", file=sys.stderr)
        sys.exit(1)




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


def main():
    sys.argv[1:] = _hoist_verbosity_flags(_patch_makepkg_argv(sys.argv[1:]))
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="All-in-one Arch Linux helper for system setup and package management with compiler-optimized builds.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help=(
            "Verbosity level. Default: errors only. "
            "-v adds warnings. -vv adds informational messages. "
            "-vvv adds debug output (full config, profile, and conf file dumps)."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # build
    p_build = sub.add_parser("build", help="Build a package from a PKGBUILD.")
    p_build.add_argument(
        "pkgbuilds",
        nargs="+",
        metavar="PKGBUILD",
        help="One or more packages to build (path, directory, or bare package name).",
    )
    p_build.add_argument(
        "--makepkg", "-m",
        metavar="FLAGS",
        help=(
            "Additional makepkg flags, appended after profile makepkg_flags. "
            "Combined short flags are expanded: -sfci becomes -s -f -c -i. "
            "Example: sysforge build PKGBUILD -m '-sfci'"
        ),
    )
    p_build.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Strip --noconfirm from profile makepkg_flags. "
            "Useful during development to review makepkg prompts."
        ),
    )
    p_build.add_argument(
        "--persist-log",
        action="store_true",
        dest="persist_log",
        help="Keep the per-package log file after a successful build (default: truncate on success).",
    )
    p_build.add_argument(
        "--no-pkg-log",
        action="store_true",
        dest="no_pkg_log",
        help="Disable the per-package log file.",
    )
    p_build.add_argument(
        "--log-dir",
        metavar="DIR",
        dest="log_dir",
        help="Directory for the per-package log file (default: alongside the PKGBUILD).",
    )
    p_build.add_argument(
        "--profile-conf",
        metavar="FILE",
        dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default user/system config.",
    )
    p_build.add_argument(
        "--cc",
        metavar="COMPILER",
        dest="cc",
        help="Override CC (C compiler) for this build, e.g. --cc clang.",
    )
    p_build.add_argument(
        "--cxx",
        metavar="COMPILER",
        dest="cxx",
        help="Override CXX (C++ compiler) for this build, e.g. --cxx clang++.",
    )
    p_build.add_argument(
        "--ld",
        metavar="LINKER",
        dest="ld",
        help="Override linker for this build. Injects -fuse-ld=LINKER into LDFLAGS, e.g. --ld lld.",
    )
    p_build.add_argument(
        "--cache-report",
        action="store_true",
        dest="cache_report",
        help="Print a structured cache summary (ccache/sccache hit rates) after the build.",
    )
    p_build.add_argument(
        "--no-update",
        action="store_true",
        dest="no_update",
        help="Skip git pull --rebase before building (default: update if a tracking branch exists).",
    )
    p_build.add_argument(
        "--state-dir",
        metavar="DIR",
        dest="state_dir",
        help=(
            "Override state directory for build_state.toml "
            "(default: /var/lib/sysforge or SYSFORGE_STATE_DIR env var)."
        ),
    )
    p_build.set_defaults(func=_cmd_build)

    # pipeline
    # pipeline
    p_pipeline = sub.add_parser(
        "pipeline",
        help="[planned] Run the full install pipeline.",
    )
    p_pipeline.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last checkpoint.",
    )
    p_pipeline.add_argument(
        "--start-from",
        metavar="STAGE",
        dest="start_from",
        help=(
            "Start from this stage, marking all prior stages as skipped. "
            "Useful for skipping bootstrap stages on a live system: "
            "--start-from packages"
        ),
    )
    p_pipeline.add_argument(
        "--force-retry",
        action="store_true",
        dest="force_retry",
        help="Retry all failed packages without prompting.",
    )
    p_pipeline.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would run without executing anything.",
    )
    p_pipeline.add_argument(
        "--packages",
        metavar="FILE",
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).",
    )
    p_pipeline.add_argument(
        "--state-dir",
        metavar="DIR",
        dest="state_dir",
        help=(
            "Override state directory (default: /var/lib/sysforge or "
            "SYSFORGE_STATE_DIR env var)."
        ),
    )
    p_pipeline.add_argument(
        "--no-unified-log",
        action="store_true",
        dest="no_unified_log",
        help="Disable the unified log file.",
    )
    p_pipeline.add_argument(
        "--no-pkg-logs",
        action="store_true",
        dest="no_pkg_logs",
        help="Disable per-package log files.",
    )
    p_pipeline.add_argument(
        "--log-dir",
        metavar="DIR",
        dest="log_dir",
        help="Directory for log files (default: state directory).",
    )
    p_pipeline.add_argument(
        "--purge-log",
        action="store_true",
        dest="purge_log",
        help="Truncate the unified log before this run.",
    )
    p_pipeline.add_argument(
        "--persist-log",
        action="store_true",
        dest="persist_log",
        help="Keep log files after successful completion (default: truncate on success).",
    )
    p_pipeline.add_argument(
        "--profile-conf",
        metavar="FILE",
        dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default user/system config.",
    )
    p_pipeline.add_argument(
        "--cache-report",
        action="store_true",
        dest="cache_report",
        help="Print a structured cache summary (ccache/sccache hit rates) after the pipeline completes.",
    )
    p_pipeline.add_argument(
        "--no-update",
        action="store_true",
        dest="no_update",
        help="Skip git pull --rebase before each build (default: update if a tracking branch exists).",
    )
    p_pipeline.set_defaults(func=_cmd_pipeline)

    # reconfigure — run ReconfigureStage directly
    p_reconfigure = sub.add_parser(
        "reconfigure",
        help="Interactive pre-build checkpoint: review configs, disk, network, GPG, build preview.",
    )
    p_reconfigure.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Run all steps non-interactively without writing any changes.",
    )
    p_reconfigure.add_argument(
        "--packages", metavar="FILE",
        help="Path to packages.toml (used by disk and preview steps).",
    )
    p_reconfigure.add_argument(
        "--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory (used for pipeline progress display).",
    )
    p_reconfigure.set_defaults(func=_cmd_reconfigure)

    # packages — run PackagesStage directly
    p_packages = sub.add_parser(
        "packages",
        help="Build and install non-kernel packages from packages.toml.",
    )
    p_packages.add_argument(
        "--packages",
        metavar="FILE",
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).",
    )
    p_packages.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.",
    )
    p_packages.add_argument(
        "--force-retry", action="store_true", dest="force_retry",
        help="Retry all failed packages without prompting.",
    )
    p_packages.add_argument(
        "--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.",
    )
    p_packages.add_argument(
        "--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.",
    )
    p_packages.add_argument(
        "--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.",
    )
    p_packages.add_argument(
        "--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.",
    )
    p_packages.add_argument(
        "--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.",
    )
    p_packages.add_argument(
        "--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.",
    )
    p_packages.add_argument(
        "--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.",
    )
    p_packages.set_defaults(func=_cmd_packages)

    # toolchain — run ToolchainStage directly
    p_toolchain = sub.add_parser(
        "toolchain",
        help="Build and install the LLVM/GCC toolchain from toolchain.toml.",
    )
    p_toolchain.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.",
    )
    p_toolchain.add_argument(
        "--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.",
    )
    p_toolchain.add_argument(
        "--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.",
    )
    p_toolchain.add_argument(
        "--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.",
    )
    p_toolchain.add_argument(
        "--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory (default: /var/lib/sysforge).",
    )
    p_toolchain.set_defaults(func=_cmd_toolchain)

    # kernel — run KernelStage directly
    p_kernel = sub.add_parser(
        "kernel",
        help="Build and install the custom kernel configured in kernel.toml.",
    )
    p_kernel.add_argument(
        "--packages",
        metavar="FILE",
        help="Path to packages.toml (default: /etc/sysforge/packages.toml).",
    )
    p_kernel.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Show what would run without executing anything.",
    )
    p_kernel.add_argument(
        "--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.",
    )
    p_kernel.add_argument(
        "--no-pkg-logs", action="store_true", dest="no_pkg_logs",
        help="Disable per-package log files.",
    )
    p_kernel.add_argument(
        "--persist-log", action="store_true", dest="persist_log",
        help="Keep log files after successful completion.",
    )
    p_kernel.add_argument(
        "--log-dir", metavar="DIR", dest="log_dir",
        help="Directory for log files.",
    )
    p_kernel.add_argument(
        "--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the run.",
    )
    p_kernel.add_argument(
        "--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.",
    )
    p_kernel.set_defaults(func=_cmd_kernel)

    # manifest
    p_manifest = sub.add_parser(
        "manifest",
        help="Generate a packages.toml stub from a list of package names.",
    )
    p_manifest.add_argument(
        "packages",
        nargs="*",
        metavar="PKG",
        help="Package names to include.",
    )
    p_manifest.add_argument(
        "--file", "-f",
        metavar="FILE",
        help="Text file with one package name per line (can be combined with inline names).",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    # update
    p_update = sub.add_parser(
        "update",
        help="Check for and rebuild outdated sysforge-managed packages.",
    )
    p_update.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show what would be rebuilt without doing it.",
    )
    p_update.add_argument(
        "--devel",
        action="store_true",
        dest="devel",
        help="Include VCS packages (-git, -svn, -hg, -bzr) in the rebuild.",
    )
    p_update.add_argument(
        "--no-update",
        action="store_true",
        dest="no_update",
        help="Skip git pull --rebase before checking versions.",
    )
    p_update.add_argument(
        "--state-dir",
        metavar="DIR",
        dest="state_dir",
        help=(
            "Override state directory (default: /var/lib/sysforge or "
            "SYSFORGE_STATE_DIR env var)."
        ),
    )
    p_update.add_argument(
        "--profile-conf",
        metavar="FILE",
        dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default user/system config.",
    )
    p_update.add_argument(
        "--cache-report",
        action="store_true",
        dest="cache_report",
        help="Print a structured cache summary (ccache/sccache hit rates) after the run.",
    )
    p_update.add_argument(
        "--no-pkg-log",
        action="store_true",
        dest="no_pkg_log",
        help="Disable per-package log files.",
    )
    p_update.add_argument(
        "--persist-log",
        action="store_true",
        dest="persist_log",
        help="Keep log files after successful completion.",
    )
    p_update.add_argument(
        "--log-dir",
        metavar="DIR",
        dest="log_dir",
        help="Directory for per-package log files.",
    )
    p_update.set_defaults(func=_cmd_update)

    # converge
    p_converge = sub.add_parser(
        "converge",
        help="[planned] Rebuild packages that have drifted from their profile.",
    )
    p_converge.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be rebuilt without doing it.",
    )
    p_converge.set_defaults(func=_cmd_converge)

    # resolve
    p_resolve = sub.add_parser(
        "resolve",
        help="Show which profile would be applied to a package and why.",
    )
    p_resolve.add_argument(
        "pkg",
        metavar="PKG",
        help="Path to a PKGBUILD file, or bare package name (looks for <cwd>/<name>/PKGBUILD).",
    )
    p_resolve.add_argument(
        "--show-flags",
        action="store_true",
        dest="show_flags",
        help="Print the full resolved flag set.",
    )
    p_resolve.add_argument(
        "--profile-conf",
        metavar="FILE",
        dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default user/system config.",
    )
    p_resolve.set_defaults(func=cmd_resolve)

    # completions (used by shell completion scripts; not user-facing)
    p_completions = sub.add_parser("completions", help=argparse.SUPPRESS)
    p_completions.add_argument(
        "resource",
        choices=["packages"],
    )
    p_completions.set_defaults(func=_cmd_completions)

    args = parser.parse_args()
    _log.set_verbosity(args.verbose)
    args.func(args)
