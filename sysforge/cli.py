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
"""
import argparse
import sys
from pathlib import Path
from sysforge import log
_log = log.get_logger("CLI")

from sysforge.resolve import cmd_resolve
from sysforge.fetch import cmd_fetch
from sysforge.update import cmd_update
from sysforge.setup_cmd import cmd_setup
from sysforge.packages_cmd import (
    cmd_packages_list,
    cmd_packages_add,
    cmd_packages_remove,
    cmd_packages_sync,
    cmd_packages_repair_state,
)

from sysforge.primitives.makepkg_wrapper import run, expand_makepkg_flags, BuildOptions
from sysforge.primitives.config import find_pkgbuild, load_config
from sysforge.primitives.paths import PACKAGES_PATH, resolve_packages_path

_PACKAGES_HELP = f"Path to packages.toml (default: {PACKAGES_PATH})."


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


def _cmd_build(args):
    extra_flags = expand_makepkg_flags(args.makepkg) if args.makepkg else None
    if args.no_pkg_log and args.log_dir:
        print("[SYSFORGE] Warning: --log-dir has no effect when --no-pkg-log is set.", file=sys.stderr)
    _log.info(f"Invocation: {' '.join(sys.argv)}")
    packages = args.pkgbuilds
    config = _load_config_with_overrides(args)
    try:
        for i, pkg in enumerate(packages):
            if getattr(args, "cleansrc", False):
                from sysforge.primitives.aur import purge_src
                target_dir = _cleansrc_target_dir(pkg, config)
                if target_dir is not None:
                    try:
                        purge_src(target_dir)
                    except RuntimeError as e:
                        _log.error(f"--cleansrc {pkg!r}: {e}")
                        continue

            pkgbuild = find_pkgbuild(pkg, config)

            # Resolve and build AUR deps before the main package
            from sysforge.primitives.aur_resolve import (
                resolve_aur_deps,
                build_resolved_deps,
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
                if args.track_deps:
                    from sysforge.packages_cmd import append_dependency_entries
                    append_dependency_entries(
                        [d.name for d in aur_deps],
                        packages_file=getattr(args, "packages", None),
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
    except RuntimeError as e:
        _log.fatal(str(e))


def _cmd_update(args):
    try:
        cmd_update(args)
    except RuntimeError as e:
        _log.fatal(str(e))


def _cmd_converge(args):
    args.extra_flags = expand_makepkg_flags(args.makepkg) if getattr(args, "makepkg", None) else []
    from sysforge.converge import cmd_converge
    try:
        cmd_converge(args)
    except RuntimeError as e:
        _log.fatal(str(e))


def _cmd_doctor(args):
    args.config = load_config() or {}
    from sysforge.doctor import cmd_doctor
    try:
        rc = cmd_doctor(args)
    except RuntimeError as e:
        _log.fatal(str(e))
    if rc:
        sys.exit(rc)


def _cmd_completions(args):
    import subprocess as _sp
    config = load_config() or {}

    if args.resource == "makepkg-flags":
        # Parse makepkg --help to extract flag/description pairs for completion
        r = _sp.run(["makepkg", "--help"], capture_output=True, text=True)
        text = r.stdout or r.stderr or ""
        # Flags sysforge handles itself — exclude from passthrough completions
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
        return

    if args.resource == "state":
        # Names tracked in build_state.toml — used by `update` completion
        from sysforge.primitives.build_state import BuildState
        from sysforge.pipeline.state import resolve_state_dir
        state_dir, _ = resolve_state_dir(None)
        bs = BuildState(state_dir)
        for name in sorted(bs.all_packages()):
            print(name)
        return

    if args.resource == "manifest":
        # Names already in packages.toml — used by `packages remove` completion
        import tomllib as _tomllib
        pkg_path = resolve_packages_path(config)
        if pkg_path.exists():
            with open(pkg_path, "rb") as _f:
                data = _tomllib.load(_f)
            for entry in data.get("package", []):
                name = entry.get("name")
                if name:
                    print(name)
        return

    if args.resource == "local":
        # Only locally-cloned packages from pkgbuild_src_dir — used by `resolve` completion
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            d = Path(raw).expanduser()
            if d.is_dir():
                for sub in sorted(d.iterdir()):
                    if sub.is_dir() and (sub / "PKGBUILD").exists():
                        print(sub.name)
        return

    seen: set[str] = set()

    # Local packages from pkgbuild_src_dir
    raw = config.get("paths", {}).get("pkgbuild_src_dir")
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

    # AUR name cache (populated by `sysforge update` as a side effect)
    from sysforge.primitives.aur import AUR_CACHE_PATH
    aur_cache = AUR_CACHE_PATH.expanduser()
    if aur_cache.exists():
        for name in aur_cache.read_text().splitlines():
            if name and name not in seen:
                seen.add(name)
                print(name)


# ---------------------------------------------------------------------------
# run namespace handlers
# ---------------------------------------------------------------------------

def _cmd_run_pipeline(args):
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


def _cmd_run_hardware(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.hardware import HardwareStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}

    options = RunOptions(
        dry_run=args.dry_run,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        no_unified_log=True,
        no_pkg_logs=True,
    )
    run_stage_standalone(HardwareStage(), config, options)


def _cmd_run_reconfigure(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.reconfigure import ReconfigureStage
    from sysforge.pipeline.stages.base import RunOptions

    config = _load_config_with_overrides(args)

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
        abi_check=args.abi_check,
        state_dir=Path(args.state_dir) if args.state_dir else None,
        persist_log=args.persist_log,
        makepkg_flags=expand_makepkg_flags(args.makepkg) if args.makepkg else [],
        rebuild_profdata=args.rebuild_profdata,
    )
    run_stage_standalone(ToolchainStage(), config, options)


def _cmd_run_packages(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.packages import PackagesStage
    from sysforge.pipeline.stages.base import RunOptions

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


def _cmd_run_kernel(args):
    from sysforge.pipeline.runner import run_stage_standalone
    from sysforge.pipeline.stages.kernel import KernelStage
    from sysforge.pipeline.stages.base import RunOptions

    config = load_config() or {}

    options = RunOptions(
        dry_run=args.dry_run,
        no_update=args.no_update,
        no_pkg_logs=args.no_pkg_logs,
        persist_log=args.persist_log,
        log_dir=Path(args.log_dir) if args.log_dir else None,
        cache_report=args.cache_report,
        abi_check=args.abi_check,
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
    # Find the subcommand position.
    sub_idx = None
    for i, tok in enumerate(argv):
        if tok in _MAKEPKG_SUBCOMMANDS:
            sub_idx = i
            break
    if sub_idx is None:
        return argv

    # Collect implicit flags from after the subcommand.
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

    # If -m / --makepkg is already present, merge into its value.
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
    p.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before building.")
    p.add_argument("--cleansrc", action="store_true", dest="cleansrc",
        help="Purge the package src dir and re-clone before building. "
             "Refuses (per package) if the existing clone has uncommitted changes, "
             "unpushed commits, or no upstream tracking branch.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.add_argument("--track-deps", action="store_true", dest="track_deps",
        help="Auto-add resolved AUR dependencies to packages.toml with reason='dependency'.")
    p.set_defaults(func=_cmd_build)


def _add_fetch_parser(sub):
    p = sub.add_parser("fetch",
        help="Download PKGBUILD(s) into pkgbuild_src_dir without building.")
    p.add_argument(
        "pkgs", nargs="+", metavar="PKG",
        help="One or more package names to download.",
    )
    p.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase for packages that are already cloned.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
    p.set_defaults(func=cmd_fetch)


def _add_update_parser(sub):
    p = sub.add_parser("update",
        help="Check for and rebuild outdated sysforge-managed packages.")
    p.add_argument("--all", action="store_true", dest="all",
        help="Also discover foreign packages (pacman -Qm) not yet tracked; "
             "add to packages.toml and rebuild if outdated.")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would be rebuilt without doing it.")
    p.add_argument("--devel", action="store_true", dest="devel",
        help="Include VCS packages (-git, -svn, -hg, -bzr) in the rebuild.")
    p.add_argument("--offline", action="store_true", dest="offline",
        help="No network: skip git pulls, clones, and AUR RPC. Pure local version check.")
    p.add_argument("--packages", metavar="FILE", dest="packages",
        help=f"Path to packages.toml for --all discovery (default: {PACKAGES_PATH}).")
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
             "Per-package fatal if the clone has uncommitted changes, unpushed commits, "
             "or no upstream — that package is reported failed and the run continues.")
    p.add_argument("pkgnames", metavar="PKG", nargs="*",
        help="Limit update to these package names (default: all sysforge-managed packages).")
    p.set_defaults(func=_cmd_update)


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
        help="Path to a flag_profiles.toml to use instead of the default.")
    p.set_defaults(func=cmd_resolve)



def _add_converge_parser(sub):
    p = sub.add_parser("converge",
        help="Detect and repair packages whose build flags have drifted from the current profile.")
    p.add_argument("--apply", action="store_true",
        help="Rebuild all DRIFTED packages with the current profile.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.add_argument("--profile-conf", metavar="FILE", dest="profile_conf",
        help="Path to a flag_profiles.toml to use instead of the default.")
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
    p.set_defaults(func=_cmd_converge)


def _add_doctor_parser(sub):
    p = sub.add_parser("doctor",
        help="Health-check installed package depends + shared-library linkage.")
    p.add_argument("packages", nargs="*", metavar="PKG",
        help="One or more installed package names to verify. "
             "Without any PKG/--graphics/--all, the command exits with usage.")
    p.add_argument("--graphics", action="store_true",
        help="Expand to the graphics stack (mesa, vulkan, libglvnd, egl-wayland, "
             "xwayland, plus per-vendor drivers from the hardware overlay's gpu_vendors).")
    p.add_argument("--all", action="store_true", dest="all",
        help="Verify every installed package (pacman -Q). Slow but comprehensive.")
    p.add_argument("--shallow", action="store_true",
        help="Do not recurse into transitive dependencies of each target.")
    p.add_argument("--quiet", "-q", action="store_true",
        help="Suppress clean lines; print only packages with issues.")
    p.set_defaults(func=_cmd_doctor)


def _add_packages_parser(sub):
    """packages namespace: list (default) / add / remove / sync"""
    p = sub.add_parser("packages",
        help="Manage packages.toml (list, add, remove, sync).")
    # --packages on the parent so bare 'sysforge packages' and
    # 'sysforge packages --packages foo.toml' both work
    p.add_argument("--packages", metavar="FILE", dest="packages",
        help=_PACKAGES_HELP)
    p.add_argument("--state", action="store_true", dest="state",
        help="List build_state.toml entries instead of packages.toml.")
    p.add_argument("--diagnose", action="store_true", dest="diagnose",
        help="Per-package directory/git status as `sysforge update` would see it.")
    p.add_argument("--problems-only", action="store_true", dest="problems_only",
        help="With --diagnose: show only packages that would silently fail.")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p.set_defaults(func=cmd_packages_list)

    pkg_sub = p.add_subparsers(dest="packages_cmd")

    # list
    p_list = pkg_sub.add_parser("list", help="Show packages in packages.toml.")
    p_list.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_list.add_argument("--state", action="store_true", dest="state",
        help="List build_state.toml entries instead of packages.toml.")
    p_list.add_argument("--diagnose", action="store_true", dest="diagnose",
        help="Per-package directory/git status as `sysforge update` would see it.")
    p_list.add_argument("--problems-only", action="store_true", dest="problems_only",
        help="With --diagnose: show only packages that would silently fail.")
    p_list.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
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

    # repair-state
    p_repair = pkg_sub.add_parser("repair-state",
        help="Re-parse PKGBUILDs to rewrite build_state.toml entries that contain "
             "unexpanded shell variables (e.g. '$_pkgname-git').")
    p_repair.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory for build_state.toml.")
    p_repair.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show the planned repair without writing.")
    p_repair.set_defaults(func=cmd_packages_repair_state)


def _add_setup_parser(sub):
    p = sub.add_parser("setup",
        help="Configure system integration (pacman IgnoreGroup for sf-build).")
    p.add_argument("--pacman-conf", metavar="FILE", dest="pacman_conf",
        help="Path to pacman.conf (default: /etc/pacman.conf).")
    p.set_defaults(func=cmd_setup)


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
        help="Path to a flag_profiles.toml to use instead of the default.")
    p_pipeline.add_argument("--cache-report", action="store_true", dest="cache_report",
        help="Print a structured cache summary after the pipeline completes.")
    p_pipeline.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_pipeline.add_argument("--no-update", action="store_true", dest="no_update",
        help="Skip git pull --rebase before each build.")
    p_pipeline.set_defaults(func=_cmd_run_pipeline)

    # run hardware
    p_hw = run_sub.add_parser("hardware",
        help="Re-run hardware detection and refresh hardware_profile.toml.")
    p_hw.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show what would be written without writing.")
    p_hw.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_hw.set_defaults(func=_cmd_run_hardware)

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
    p_toolchain.set_defaults(func=_cmd_run_toolchain)

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
        help="Path to a flag_profiles.toml to use instead of the default.")
    p_pkgs.set_defaults(func=_cmd_run_packages)

    # run kernel
    p_kernel = run_sub.add_parser("kernel",
        help="Build and install the custom kernel configured in kernel.toml.")
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
    p_kernel.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries.")
    p_kernel.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_kernel.set_defaults(func=_cmd_run_kernel)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Return the top-level ArgumentParser. Called by main() and by argparse-manpage."""
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
    _add_fetch_parser(sub)
    _add_update_parser(sub)
    _add_resolve_parser(sub)
    _add_converge_parser(sub)
    _add_doctor_parser(sub)
    _add_packages_parser(sub)
    _add_run_parser(sub)
    _add_setup_parser(sub)

    # completions (used by shell completion scripts; not user-facing)
    p_completions = sub.add_parser("completions", help=argparse.SUPPRESS)
    p_completions.add_argument("resource", choices=["packages", "manifest", "local", "state", "makepkg-flags"])
    p_completions.set_defaults(func=_cmd_completions)

    return parser


def main():
    from sysforge.primitives.resource_guard import install as _install_resource_guard
    _install_resource_guard()
    sys.argv[1:] = _patch_makepkg_argv(
        _extract_implicit_makepkg_flags(_hoist_verbosity_flags(sys.argv[1:]))
    )
    parser = _build_parser()
    args = parser.parse_args()
    log.set_verbosity(args.verbose)
    if getattr(args, "dry_run", False):
        log.set_dry_run_mode()
    args.func(args)
