# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
cli.py — SysForge command-line interface

Top-level commands:
    sysforge build <pkg>    Build a package using its matched profile
    sysforge update         Check for and rebuild outdated sysforge-managed packages
                            (also reports flag/toolchain drift; --rebuild-on-*-drift to act)
    sysforge resolve <pkg>  Show which profile would be applied to a package
    sysforge doctor [PKG]   Health-check installed package depends + linkage

Namespaces:
    sysforge packages       Manage packages.toml (list / add / remove / sync)
    sysforge run            Execute pipeline stages (pipeline / hardware / reconfigure /
                             toolchain / packages / kernel)

Every verb is a ``Verb`` subclass dispatched through
:func:`sysforge.verbs.runner.run_verb`. The pre_check / execute /
post_validate split is documented in DESIGN.md §CLI Verb Framework.
"""
import argparse
import os
import sys
from pathlib import Path

from sysforge import log

_log = log.get_logger("CLI")

from sysforge.build_cmd import BuildVerb
from sysforge.completions_cmd import CompletionsVerb
from sysforge.config_cmd import ConfigMergeVerb
from sysforge.doctor import DoctorPkgVerb, DoctorSystemVerb
from sysforge.env_cmd import EnvVerb
from sysforge.fetch import FetchVerb
from sysforge.log_cmd import LogVerb
from sysforge.packages_cmd import (
    PackagesAddGroupVerb,
    PackagesAddVerb,
    PackagesListVerb,
    PackagesRemoveVerb,
)
from sysforge.primitives import artifacts
from sysforge.primitives.pkg_catalog import valid_desktops
from sysforge.resolve import ResolveVerb
from sysforge.run_cmd import (
    RunHardwareVerb,
    RunKernelVerb,
    RunPackagesVerb,
    RunPipelineVerb,
    RunReconfigureVerb,
    RunToolchainVerb,
)
from sysforge.revert_cmd import RevertToStockVerb
from sysforge.search_cmd import SearchVerb
from sysforge.setup_cmd import SetupVerb
from sysforge.state_cmd import (
    StateFailedVerb,
    StateForgetVerb,
    StateListVerb,
    StateOrphansVerb,
    StateRepairVerb,
)
from sysforge.uninstall_cmd import UninstallVerb
from sysforge.update import UpdateVerb
from sysforge.verbs import run_verb
from sysforge.verbs.artifact import (
    ArtifactAdoptVerb,
    ArtifactDeployVerb,
    ArtifactEditVerb,
    ArtifactListVerb,
    ArtifactRemoveVerb,
    ArtifactReviewVerb,
)

_PACKAGES_HELP = (
    "Path to packages.toml (default: /etc/sysforge/packages.toml; "
    "override the dir with $SYSFORGE_CONFIG_DIR)."
)


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
    # The global --quiet is hoisted like -v so users can place it after the
    # subcommand (sysforge update --quiet). It is NOT hoisted when the command
    # is `doctor`, which owns a *local* --quiet/-q with different semantics
    # (suppress clean lines) — hoisting would let the global parser eat it. The
    # short -q is doctor-only and never hoisted.
    hoist_quiet = "doctor" not in argv
    for tok in argv:
        if tok in ("-v", "-vv", "-vvv", "--verbose") or tok == "--quiet" and hoist_quiet:
            verbose_tokens.append(tok)
        else:
            rest.append(tok)
    return verbose_tokens + rest


def _resolve_verbosity(args):
    """Resolve the effective stderr verbosity level (0–3), mirroring
    :func:`_resolve_color_mode`.

    Precedence (highest first):
      1. Global ``--quiet`` (``args.quiet_global``) → 0, wins over everything.
      2. Else any ``-v/-vv/-vvv`` passed (``args.verbose`` count > 0) → that level.
      3. Else ``[log] verbosity`` config value, clamped to 0–3.
      4. Else 0.

    An invalid config value (non-int, out of range) or an unreadable config
    degrades gracefully — clamp or ignore, never abort startup — matching the
    posture of ``set_color_mode``/``set_unicode_mode``.
    """
    if getattr(args, "quiet_global", False):
        return 0
    verbose = int(getattr(args, "verbose", 0) or 0)
    if verbose > 0:
        return min(3, verbose)
    try:
        from sysforge.primitives.config import load_sysforge_toml
        raw = (load_sysforge_toml().get("log", {}) or {}).get("verbosity")
    except Exception:
        raw = None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, min(3, raw))


# Global flags hoisted before the subcommand by _hoist_global_flags.
# Maps flag → whether it consumes a following value token.
_GLOBAL_HOIST_FLAGS = {
    "--py-profile": False,
    "--py-profile-out": True,
    "--timings": False,
    "--color": True,
    "--no-throttle": False,
    "--turbo": False,
}


def _resolve_throttle_override(args):
    """Map the global throttle flags to a build_throttle run-override.

    ``--turbo`` (boost) is the stronger request and wins over ``--no-throttle``
    (bypass); neither present → ``None`` (honour the configured throttle).
    """
    if getattr(args, "turbo", False):
        return "boost"
    if getattr(args, "no_throttle", False):
        return "bypass"
    return None


def _resolve_color_mode(flag_value):
    """Resolve the effective colour mode: ``--color`` flag > ``[ui] color`` config
    > ``"auto"``. Any unexpected config value degrades to ``"auto"`` (log.set_color_mode
    also guards this), and a missing/unreadable config never aborts startup.
    """
    if flag_value:
        return flag_value
    try:
        from sysforge.primitives.config import load_sysforge_toml
        cfg = (load_sysforge_toml().get("ui", {}) or {}).get("color")
    except Exception:
        cfg = None
    return cfg if cfg in ("auto", "always", "never") else "auto"


def _hoist_global_flags(argv):
    """
    Move the global flags in _GLOBAL_HOIST_FLAGS (and their value tokens /
    --flag=value forms) to before the subcommand, mirroring
    _hoist_verbosity_flags so users can place them anywhere.

    sysforge update --py-profile  →  sysforge --py-profile update
    """
    hoisted = []
    rest = []
    toks = iter(argv)
    for tok in toks:
        flag = tok.split("=", 1)[0]
        if flag in _GLOBAL_HOIST_FLAGS:
            hoisted.append(tok)
            if _GLOBAL_HOIST_FLAGS[flag] and "=" not in tok:
                value = next(toks, None)
                if value is not None:
                    hoisted.append(value)
        else:
            rest.append(tok)
    return hoisted + rest


# Flags that sysforge handles or that take a value arg — exclude from implicit
# passthrough.  -v is already stripped by _hoist_verbosity_flags.
_PASSTHROUGH_EXCLUDE = frozenset("hVpmD")

# Subcommands that accept makepkg flag passthrough.
_MAKEPKG_SUBCOMMANDS = frozenset({"build", "update"})


def _extract_implicit_makepkg_flags(argv):
    """
    Detect bare makepkg-style short flags on build/update and rewrite
    them into explicit ``-m`` form so the rest of the pipeline handles them
    uniformly.

    sysforge build ventoy -sfCci  →  sysforge build ventoy -m -sfCci

    A token qualifies when it starts with ``-`` (not ``--``), is longer than
    one character, and no letter after the dash is in _PASSTHROUGH_EXCLUDE
    (letters are not validated against makepkg's flag set — makepkg rejects
    unknown flags itself).  If ``-m`` / ``--makepkg`` is already present the
    implicit flags are still collected and merged.
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
             "Usually optional: bare makepkg short flags are forwarded "
             "implicitly (sysforge build PKGBUILD -sfc works without -m). "
             "-m is only needed for makepkg long flags (e.g. --skippgpcheck), "
             "flags that take a value (-p, -D), or flags sysforge claims for "
             "itself (-h, -V).",
    )
    p.add_argument("--interactive", action="store_true",
        help="Strip --noconfirm from profile makepkg_flags and hand stdout/stderr "
             "to makepkg's terminal so pacman conflict prompts and other "
             "unbuffered interactive output appear immediately (disables "
             "line-based output classification while set).")
    p.add_argument("--persist-log", action="store_true", dest="persist_log",
        help="Keep the per-package log file after a successful build. "
             "Also settable via [build] persist_log = true in packages.toml.")
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
        help="Print a structured cache summary (ccache/sccache hit rates) after the build. "
             "Also settable via [build] cache_report = true in packages.toml.")
    p.add_argument("--abi-check", action="store_true", dest="abi_check",
        help="Run a post-build ABI compatibility check on built shared libraries. "
             "Also settable via [build] abi_check = true in packages.toml.")
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
    p.add_argument("--no-review", action="store_true", dest="no_review",
        help="Skip the PKGBUILD review gate (full source-tree diff prompt for "
             "packages whose source changed since the last accepted build). "
             "Also configurable via [build] review = false in packages.toml.")
    p.add_argument("--force", action="store_true", dest="force",
        help="Unconditionally build all arguments from source for this run "
             "only, including repo packages not yet opted in. Never prompts "
             "for or modifies packages.toml opt-in keys.")
    p.add_argument("--pgo", choices=("record", "use"), dest="pgo_mode",
        help="Mesa instrumentation PGO (LLVM toolchain only). "
             "--pgo=record builds+installs an instrumented mesa that writes "
             "profile data to the sysforge store as you run graphics workloads; "
             "--pgo=use merges the collected profiles and rebuilds an optimized "
             "mesa-sysforge (conflicts/replaces stock mesa). No-op for non-mesa "
             "targets.")
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
        help="Path to packages.toml for override rules "
             "(default: /etc/sysforge/packages.toml; override the dir with "
             "$SYSFORGE_CONFIG_DIR).")
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
        help="Extra flags passed verbatim to makepkg. Usually optional: bare "
             "makepkg short flags are forwarded implicitly (sysforge update -f "
             "works without -m). -m is only needed for makepkg long flags "
             "(e.g. --skippgpcheck), flags that take a value (-p, -D), or "
             "flags sysforge claims for itself (-h, -V).")
    p.add_argument("--interactive", action="store_true",
        help="Pause on build failures to allow manual correction "
             "(default: log failure and continue).")
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
    review_group = p.add_mutually_exclusive_group()
    review_group.add_argument("--review", action="store_true", dest="review",
        help="Prompt to review the full source-tree diff for packages whose "
             "source changed since the last accepted build. By default update "
             "auto-accepts changes with a logged notice so batch runs stay "
             "unattended; use `sysforge build` or this flag to inspect diffs.")
    review_group.add_argument("--no-review", action="store_true", dest="no_review",
        help="Skip the PKGBUILD review gate entirely (no auto-accept notices). "
             "Also configurable via [build] review = false in packages.toml.")
    p.add_argument("--no-toolchain-preflight", action="store_true", dest="no_toolchain_preflight",
        help="Skip the toolchain pre-flight (rust/cmake/meson availability + "
             "lib32 cross targets) that normally runs before the build loop.")
    p.add_argument("--include-stage-owned", action="store_true", dest="include_stage_owned",
        help="Include packages owned by a pipeline stage (e.g. the kernel "
             "stage's `linux-sysforge`). Skipped by default; the owning stage "
             "(`sysforge run kernel`) is the canonical update path.")
    p.add_argument("--explain-drift", action="store_true", dest="explain_drift",
        help="List drifted packages and exit, across both axes: toolchain "
             "drift (recorded toolchain_variant differs from the active "
             "gcc / stock_llvm / pgo_llvm) and flag drift (profiled packages "
             "whose flags now resolve differently than when built, with a "
             "per-key diff). Informational; no source sync, no rebuild.")
    p.add_argument("--rebuild-on-toolchain-drift", action="store_true",
        dest="rebuild_on_toolchain_drift",
        help="Treat toolchain-variant drift as an upgrade trigger: packages "
             "built under a different toolchain than is active now are added "
             "to the rebuild queue. Off by default — drift is reported but "
             "not acted on, since most C/C++ packages don't measurably "
             "benefit from a re-stamp. Also settable via "
             "[update] rebuild_on_toolchain_drift = true in sysforge.toml.")
    p.add_argument("--rebuild-on-flag-drift", action="store_true",
        dest="rebuild_on_flag_drift",
        help="Treat flag drift as an upgrade trigger: profiled packages whose "
             "flags now resolve differently than when built are added to the "
             "rebuild queue. Off by default — flag drift is reported but not "
             "acted on, since one profile edit can drift every profiled "
             "package. Also settable via [update] rebuild_on_flag_drift = true "
             "in sysforge.toml.")
    p.add_argument("--rebuild-on-drift", action="store_true",
        dest="rebuild_on_drift",
        help="Umbrella for both --rebuild-on-toolchain-drift and "
             "--rebuild-on-flag-drift: rebuild anything that has drifted. "
             "Also settable via [update] rebuild_on_drift = true in sysforge.toml.")
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


def _add_doctor_system_flags(p):
    """Register the system-state axis flags.

    Called for BOTH the `doctor system` subparser and the bare `doctor`
    parser, so `sysforge doctor --boot` keeps working alongside
    `sysforge doctor system --boot`. One home for the help text (2.6.1-F1).
    """
    p.add_argument("--graphics", action="store_true",
        help="Run graphics system-state probes: nvidia-drm modeset, driver "
             "version skew, Wayland explicit-sync protocol, Steam GPU accel, "
             "and session type. Does not select packages — for the graphics "
             "package set use `doctor pkg --graphics`.")
    p.add_argument("--gfxperf", action="store_true",
        help="Advisory graphics-performance sweep (stutter/tearing/frame-drop "
             "contributors: video-decode path, GPU power/clocks, CPU governor, "
             "frame pacing, thermal). Opt-in; never fails. Combine with "
             "--graphics for the full graphics picture.")
    p.add_argument("--hardware", action="store_true",
        help="Run system-state hardware probes: inventory all PCI/USB devices, "
             "flag any present device with no kernel driver bound, and audit the "
             "running kernel's .config for boot-critical / device-driver gaps "
             "(the missing-driver class of bug).")
    p.add_argument("--distro", action="store_true",
        help="Report the running distribution and its support tier, read from "
             "os-release(5): Arch is the primary base, an Arch-derived distro "
             "(ID_LIKE=arch) has its packaging/dependency/makepkg.conf "
             "invariants validated but not the bootstrap/kernel/graphics "
             "checks, and anything else warns. Read-only, no subprocess; on "
             "plain Arch the bare sweep stays silent, this flag always prints "
             "the identity.")
    p.add_argument("--toolchain", action="store_true",
        help="Check that the configured toolchain matches what's installed: when "
             "toolchain.toml requests a custom LLVM toolchain (compiler = llvm, "
             "optionally PGO) but stock repo LLVM is installed — or the PGO "
             "profdata is version-skewed — report it.")
    p.add_argument("--rust", action="store_true",
        help="Report which Rust toolchain a build will actually use: the "
             "effective cargo/rustc owner (rustup channel or distro `rust` "
             "package) and a WARN if the active rustup default is non-stable "
             "(nightly/beta/pinned). Advisory, read-only; never fails, never "
             "rewrites a pin. Opt-in. For per-package rust-toolchain.toml pins "
             "use `doctor pkg PKG --rust`.")
    p.add_argument("--cache", action="store_true",
        help="Check compile-cache readiness *before* a build relies on it: "
             "whether ccache/sccache are installed, their cache dir is writable, "
             "and a non-zero size cap is set. Absence is informational, not a "
             "failure. Distinct from build verbs' --cache-report, which measures "
             "per-package hit rates *after* a build.")
    p.add_argument("--pacman", action="store_true",
        help="Check local package-database integrity (read-only, never syncs): "
             "`pacman -Dk` dependency consistency, a stale db.lck, unmerged "
             ".pacnew/.pacsave config files under /etc, and orphan packages.")
    p.add_argument("--state", action="store_true",
        help="Check sysforge's own state integrity: recorded build failures, an "
             "interrupted stage sentinel from a prior run, and build_state drift "
             "vs the live pacman database. Read-only (does not recover).")
    p.add_argument("--boot", action="store_true",
        help="Check running-system boot readiness (reusing the kernel stage's "
             "safety primitives): per-kernel boot artifacts (vmlinuz + initramfs "
             "+ boot entry), a recovery fallback kernel, /boot space, and DKMS "
             "modules for the running kernel.")
    p.add_argument("--restart", action="store_true",
        help="Check whether upgraded packages have actually taken effect: scans "
             "running processes for files replaced on disk (a `(deleted)` "
             "mapping) and reports whether the fix is a unit restart, a "
             "re-login, or a reboot. Read-only; never escalates, so coverage of "
             "other users' processes is reported as incomplete rather than "
             "prompting.")
    p.add_argument("--storage", action="store_true",
        help="Check storage/filesystem health: free space on the build dir "
             "(warns under [doctor] disk_low_gb) and /etc/fstab integrity "
             "(entries whose UUID/label/device no longer resolves). Read-only; "
             "never mounts.")
    p.add_argument("--services", action="store_true",
        help="Check live service/driver runtime health: failed systemd units "
             "(`systemctl --failed`), firmware a driver requested but could "
             "not load this boot, and error-priority journal lines this boot.")
    p.add_argument("--audio", action="store_true",
        help="Check live PipeWire/WirePlumber sound-stack health: failed audio "
             "user services (`systemctl --user --failed`) and a vanished output "
             "sink (only the dummy auto_null device present). User-scoped, so it "
             "degrades to clean under sudo.")
    p.add_argument("--network", action="store_true",
        help="Check network/connectivity configuration: no default route, "
             "connection-manager ownership conflicts (more than one of "
             "NetworkManager/systemd-networkd/dhcpcd/… enabled), and a DNS "
             "provisioner conflict (systemd-resolved active but "
             "/etc/resolv.conf is a static override). Read-only; no live "
             "network calls.")
    p.add_argument("--integrity", action="store_true",
        help="Verify package-owned files against alpm's recorded mtree via "
             "`pacman -Qkk`: files an admin or a misbehaving install script "
             "altered or removed outside a pacman transaction. Opt-in (excluded "
             "from the bare/full sweep because a whole-system scan re-hashes "
             "every packaged file); scope it with `doctor pkg PKG --integrity`. "
             "Read-only; never restores.")
    p.add_argument("--quiet", "-q", action="store_true",
        help="Suppress clean lines; print only findings.")


def _add_doctor_parser(sub):
    """doctor namespace: system / pkg.

    Bare `sysforge doctor` is `doctor system` — the fast full sweep, unchanged
    as the everyday entry point (2.6.1-F1).
    """
    p = sub.add_parser("doctor",
        help="Diagnose system health (`doctor system`) or package health "
             "(`doctor pkg`). Bare `doctor` runs the full system sweep.")
    # Bare `sysforge doctor` is `doctor system` — the everyday full sweep. The
    # flat axis flags are NOT registered here: they were removed in 3.0.0 and
    # `doctor_migration_hint` intercepts them with their replacement before
    # argparse can report a bare "unrecognized arguments". Only `--quiet`
    # survives at this level, since it modifies the sweep rather than selecting
    # one. `packages=[]` keeps the shared target helpers happy at system scope.
    p.add_argument("--quiet", "-q", action="store_true",
        help="Suppress clean lines; print only findings.")
    p.set_defaults(verb_cls=DoctorSystemVerb, packages=[], doctor_cmd="system")

    doctor_sub = p.add_subparsers(dest="doctor_cmd")

    p_system = doctor_sub.add_parser("system",
        help="System-state axes: toolchain, cache, hardware, graphics, pacman, "
             "state, boot, restart, storage, services, audio, network, distro. "
             "With no axis flag, runs all 13 non-opt-in axes.")
    _add_doctor_system_flags(p_system)
    p_system.set_defaults(verb_cls=DoctorSystemVerb, packages=[])

    p_pkg = doctor_sub.add_parser("pkg",
        help="Package-scoped axes over one or more targets: --abi (depends + "
             "ABI linkage), --rust (rust-toolchain.toml pins), --integrity "
             "(pacman -Qkk). With no axis flag, runs all three.")
    p_pkg.add_argument("packages", nargs="*", metavar="PKG",
        help="Installed package names to check.")
    p_pkg.add_argument("--abi", action="store_true",
        help="Walk each target's depends and ABI linkage: unsatisfied "
             "dependencies and unresolved sonames, over the transitive "
             "dependency closure unless --shallow.")
    p_pkg.add_argument("--rust", action="store_true",
        help="Report each target's rust-toolchain.toml pin and whether that "
             "toolchain is installed (uninstalled = mid-build network fetch). "
             "A named target with no PKGBUILD or no pin now says so explicitly "
             "instead of staying silent.")
    p_pkg.add_argument("--integrity", action="store_true",
        help="Verify each target's package-owned files against alpm's recorded "
             "mtree via `pacman -Qkk`. Read-only; never restores.")
    p_pkg.add_argument("--all", action="store_true", dest="all",
        help="Target every installed package, foreign and non-foreign "
             "(pacman -Q). Suppresses the opt-in axes — runs --abi only unless "
             "you name another axis explicitly.")
    p_pkg.add_argument("--repo", action="store_true", dest="repo",
        help="Target every non-foreign (native repo) package. Narrower than "
             "--all; suppresses the opt-in axes the same way.")
    p_pkg.add_argument("--graphics", action="store_true",
        help="Target the installed graphics stack (mesa, vulkan, libglvnd, "
             "wayland, libdrm, libva, libvdpau, egl-wayland, xwayland, "
             "gamescope, plus per-vendor drivers from the hardware overlay's "
             "gpu_vendors). A peer of --all / --repo. For graphics system-state "
             "probes use `doctor system --graphics`.")
    p_pkg.add_argument("--shallow", action="store_true",
        help="Do not recurse into transitive dependencies of each target.")
    p_pkg.add_argument("--quiet", "-q", action="store_true",
        help="Suppress clean lines; print only packages with issues.")
    p_pkg.add_argument("--suggest", "-s", action="store_true",
        help="For each unsatisfied soname, look up candidate packages "
             "via `pacman -Fq`. Findings split into 'install candidates' "
             "(missing from disk — install the package), 'rebuild "
             "candidates' (installed; rebuild against the current system), "
             "and 'ABI-drift candidates' (present but one of their "
             "versioned symbols no longer resolves — rebuild or upgrade, "
             "not reinstall). Requires a synced files db (`sudo pacman -Fy`).")
    p_pkg.add_argument("--apply", action="store_true",
        help="Hand the rebuild candidates from --suggest off to `sysforge "
             "update` and rebuild them. Implies --suggest. Drift-rebuild only "
             "in v1.x — install candidates are printed but not invoked.")
    p_pkg.add_argument("--no-confirm", action="store_true", dest="no_confirm",
        help="Skip the y/N prompt before --apply rebuilds.")
    p_pkg.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Report what --apply would rebuild without invoking the build.")
    p_pkg.set_defaults(verb_cls=DoctorPkgVerb)


def _add_artifact_parser(sub):
    """artifact namespace: list, review, adopt, edit, deploy, remove."""
    p = sub.add_parser("artifact",
        help="inventory user-authored scripts, units, and pacman hooks")
    asub = p.add_subparsers(dest="artifact_cmd")
    # set_defaults() must come after add_subparsers(): argparse's subparsers
    # action carries its own default=None that otherwise wins over an
    # earlier set_defaults() call for the same dest (it updates the action's
    # default in place only if the action already exists).
    p.set_defaults(verb_cls=ArtifactListVerb, artifact_cmd="list")

    a_list = asub.add_parser("list", help="list managed artifacts")
    a_list.add_argument(
        "--unmanaged",
        action="store_true",
        help="also show discovered candidates not yet adopted",
    )
    a_list.set_defaults(verb_cls=ArtifactListVerb)

    a_review = asub.add_parser(
        "review", help="interactively offer discovered candidates for adoption")
    a_review.set_defaults(verb_cls=ArtifactReviewVerb)

    a_adopt = asub.add_parser("adopt", help="bring a live artifact under management")
    a_adopt.add_argument("path", help="path to the artifact to adopt")
    a_adopt.add_argument(
        "--class", dest="cls", choices=list(artifacts.ARTIFACT_CLASSES),
        default=None, help="artifact class (inferred from the scan root if omitted)",
    )
    a_adopt.set_defaults(verb_cls=ArtifactAdoptVerb)

    a_edit = asub.add_parser("edit", help="edit the managed copy of an artifact")
    a_edit.add_argument("name", help="registry name of the artifact")
    a_edit.set_defaults(verb_cls=ArtifactEditVerb)

    a_dep = asub.add_parser("deploy", help="push managed content to the live system")
    grp = a_dep.add_mutually_exclusive_group(required=True)
    grp.add_argument("name", nargs="?", help="registry name of the artifact")
    grp.add_argument("--all", action="store_true", help="deploy every managed artifact")
    res = a_dep.add_mutually_exclusive_group()
    res.add_argument("--force", action="store_true",
        help="managed copy wins, discarding the live edit")
    res.add_argument("--adopt-live", dest="adopt_live", action="store_true",
        help="live file wins, updating the managed copy")
    a_dep.set_defaults(verb_cls=ArtifactDeployVerb)

    a_rm = asub.add_parser("remove", help="remove an artifact from the live system")
    a_rm.add_argument("name", help="registry name of the artifact")
    a_rm.add_argument("--purge", action="store_true",
        help="also delete the managed copy and registry entry")
    a_rm.add_argument("--force", action="store_true",
        help="remove even though the live file changed outside sysforge "
             "(discards those live-only edits)")
    a_rm.set_defaults(verb_cls=ArtifactRemoveVerb)


def _add_packages_parser(sub):
    """packages namespace: list (default) / add / remove."""
    p = sub.add_parser("packages",
        help="Manage packages.toml override entries (list, add, add-group, remove).")
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
             "--enable-build-from-source / --no-cache / --reason.")
    p_add.add_argument("pkg", metavar="PKG", help="Package name to add or update.")
    p_add.add_argument("--source", choices=("repo", "aur", "local"), dest="source",
        help="Pin routing (metadata; doesn't satisfy validation on its own). "
             "`local` marks a hand-maintained PKGBUILD with no remote to sync from.")
    p_add.add_argument("--enable-build-from-source", action="store_true",
        dest="enable_build_from_source",
        help="Build this repo package from source instead of installing the "
             "binary via pacman.")
    p_add.add_argument("--no-cache", action="store_true", dest="no_cache",
        help="Disable ccache/sccache for this package (required for PGO).")
    p_add.add_argument("--reason", metavar="TEXT", dest="reason",
        help="Free-form note attached to the entry.")
    p_add.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_add.set_defaults(verb_cls=PackagesAddVerb)

    # add-group
    p_group = pkg_sub.add_parser("add-group",
        help="Write a curated desktop-environment package group "
             "(installs via 'sysforge run packages').")
    p_group.add_argument("desktop", metavar="DESKTOP", choices=valid_desktops(),
        help=f"Desktop environment to add ({' | '.join(valid_desktops())}).")
    p_group.add_argument("--packages", metavar="FILE", dest="packages",
        help="Path to packages.toml.")
    p_group.set_defaults(verb_cls=PackagesAddGroupVerb)

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
    # set_defaults() must come after add_subparsers(): see the artifact
    # parser above for why the ordering matters.
    p.set_defaults(verb_cls=StateListVerb, state_cmd="list")

    p_list = state_sub.add_parser("list", help="Tabulate build_state.toml entries.")
    p_list.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_list.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
    p_list.set_defaults(verb_cls=StateListVerb)

    p_repair = state_sub.add_parser("repair",
        help="Repair build_state.toml: re-parse PKGBUILDs to rewrite entries with "
             "unexpanded shell variables (e.g. '$_pkgname-git'), and normalize "
             "known legacy build_mode tokens ('profiled' -> 'source_built').")
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

    p_failed = state_sub.add_parser("failed",
        help="List packages whose last build failed (recorded in build_state.toml), "
             "with any diagnosed fix. Entries auto-clear on the next successful build.")
    p_failed.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_failed.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe output through $PAGER (default: paginate when stdout is a TTY).")
    p_failed.add_argument("--clear", metavar="PKGBASE", dest="clear",
        help="Clear the recorded failure for PKGBASE and exit.")
    p_failed.add_argument("--clear-all", action="store_true", dest="clear_all",
        help="Clear all recorded failures and exit.")
    p_failed.set_defaults(verb_cls=StateFailedVerb)

    p_forget = state_sub.add_parser("forget",
        help="Stop maintaining PKG(s): delete their build_state record so "
             "`sysforge update` no longer rebuilds them from source. The installed "
             "package is left in place (still pinned by the sf-build group).")
    p_forget.add_argument("pkgnames", nargs="+", metavar="PKG",
        help="Package name(s) or pkgbase(s) to stop tracking. A pkgbase forgets "
             "every split-package member sharing it.")
    p_forget.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_forget.set_defaults(verb_cls=StateForgetVerb)


def _add_revert_parser(sub):
    p_revert = sub.add_parser(
        "revert-to-stock",
        help="undo a source-built/optimized package back to the repo version",
    )
    p_revert.add_argument("packages", nargs="+", metavar="PKG",
        help="package name(s) to revert (stock or -sysforge name)")
    p_revert.add_argument("--force", action="store_true",
        help="skip the confirmation prompt")
    p_revert.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p_revert.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Show the planned revert without making changes.")
    p_revert.set_defaults(verb_cls=RevertToStockVerb)


def _add_search_parser(sub):
    p = sub.add_parser("search",
        help="search installed, repo, and AUR packages for a term")
    p.add_argument("term", metavar="TERM", help="search term (name + description)")
    p.set_defaults(verb_cls=SearchVerb)


def _add_uninstall_parser(sub):
    p = sub.add_parser("uninstall",
        help="remove package(s); demote any sysforge-tracked ones out of build state")
    p.add_argument("packages", nargs="+", metavar="PKG",
        help="package name(s) to remove (stock or -sysforge name)")
    p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
        help="Override state directory.")
    p.set_defaults(verb_cls=UninstallVerb)


def _add_setup_parser(sub):
    p = sub.add_parser("setup",
        help="Configure system integration (pacman IgnoreGroup for sf-build; "
             "install/refresh sysforge's pacman hooks).")
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


def _add_config_parser(sub):
    """config namespace: merge — adopt .sfnew/.pacnew config companions."""
    p = sub.add_parser("config",
        help="Manage live sysforge config files (adopt shipped-default drift).")
    config_sub = p.add_subparsers(dest="config_cmd")
    config_sub.required = True

    p_merge = config_sub.add_parser("merge",
        help="Interactively adopt or clear .sfnew companions (and pacman's "
             ".pacnew/.pacsave for sysforge config) left by `make sync-config`. "
             "pacdiff-style: view diff, merge in a tool, skip, remove, or "
             "overwrite. Merge tool: SYSFORGE_MERGE > sysforge.toml [ui].merge "
             "> $DIFFPROG > vimdiff.")
    p_merge.add_argument("--config-dir", metavar="DIR", dest="config_dir",
        help="Live config dir to scan (default: $SYSFORGE_CONFIG_DIR, else "
             "/etc/sysforge).")
    p_merge.add_argument("--list", action="store_true", dest="list",
        help="List companion files and their live targets without prompting "
             "(non-interactive; for scripting/CI). Alias: --dry-run.")
    p_merge.add_argument("--dry-run", action="store_true", dest="dry_run",
        help="Same as --list: report companions without merging.")
    p_merge.add_argument("--no-pager", action="store_true", dest="no_pager",
        help="Don't pipe diffs through $PAGER (default: paginate when stdout is a TTY).")
    p_merge.set_defaults(verb_cls=ConfigMergeVerb)


def _add_run_parser(sub):
    """run namespace: pipeline / reconfigure / toolchain / packages / kernel"""
    p = sub.add_parser("run",
        help="Execute a pipeline stage "
             "(pipeline, hardware, reconfigure, toolchain, packages, kernel).")
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
             "Unlike build/update, -m is required here — bare makepkg flags "
             "are not forwarded implicitly on run subcommands. "
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
        help="Force a full 4-pass PGO build even if compatible profdata already exists.")
    p_toolchain.add_argument("--reuse-built", action="store_true", dest="reuse_built",
        help="Skip rebuilding a Pass-3 PGO package whose inputs are unchanged "
             "(PKGBUILD, source commit, profdata, flags, compiler, dep versions) "
             "and whose built artifact is still on disk. Lets a rerun after a "
             "late-package failure reuse the already-optimized llvm/llvm-libs "
             "instead of rebuilding them. Opt-in; overrides toolchain.toml "
             "reuse_unchanged. Any input change or missing artifact rebuilds.")
    p_toolchain.add_argument("--auto-pgo", action="store_true", dest="auto_pgo",
        help="Bypass the PGO confirmation prompts (profdata reuse, staging/pgo_store "
             "purge, 4-pass start, suspicious profdata size). Required for non-interactive "
             "PGO runs; without it, a non-TTY invocation aborts the PGO sub-flow because PGO "
             "is fragile and silent mis-optimisation is the failure mode.")
    p_toolchain.add_argument("--allow-dirty-llvm", action="store_true", dest="allow_dirty_llvm",
        help="Bypass the LLVM safety pre-flight refusal on dirty or diverged "
             "trees. PGO profdata version mismatches cannot be bypassed. "
             "Note: this only suppresses the refusal — it does not modify the "
             "tree. Use --cleansrc-force to actually overwrite the local "
             "trees with upstream.")
    p_toolchain.add_argument("--allow-version-skew", action="store_true",
        dest="allow_version_skew",
        help="Override the pre-build Gate-1 abort when the in-tree LLVM "
             "PKGBUILDs disagree on pkgver across the lockstep suite "
             "(llvm/llvm-libs/clang/lld/compiler-rt/polly/openmp). By default a "
             "skew aborts before building because dependency resolution will "
             "fail; this builds anyway. spirv-llvm-translator and lib32-* are "
             "never part of the skew check.")
    p_toolchain.add_argument("--skip-build-space-check", action="store_true",
        dest="skip_build_space_check",
        help="Override the pre-build Gate-1 abort when a filesystem hosting the "
             "staging dirs / pgo_store / build output lacks min_build_free_gb "
             "free (default 40 GiB). Dangerous — the multi-hour LLVM build may "
             "fail partway with no space left.")
    p_toolchain.add_argument("--rebuild-soname-consumers",
        dest="rebuild_soname_consumers", choices=("prompt", "auto", "off"),
        default=None,
        help="What to do when the built libLLVM changes its soname and would "
             "break installed consumers (mesa, etc.): 'prompt' (default) warns "
             "and asks before building; 'auto' approves and rebuilds them after "
             "install; 'off' builds the toolchain but leaves consumers for you "
             "to rebuild. Overrides toolchain.toml rebuild_soname_consumers.")
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
    p_kernel.add_argument(
        "--bootloader", choices=["systemd-boot", "grub", "none"], dest="bootloader",
        help="Override kernel.toml bootloader for this invocation (systemd-boot is the default).")
    p_kernel.add_argument("--base-config", metavar="SRC", dest="base_config",
        help="Override kernel.toml base_config for this run: the starting .config "
             "before the sysforge.config fragment overlay. One of 'pkgbuild' "
             "(the PKGBUILD's own base), 'running' (the running kernel's config), "
             "or a path to a .config file. Resolution order: this flag > "
             "kernel.toml base_config > 'pkgbuild' default.")
    p_kernel.add_argument("--headers",
        action=argparse.BooleanOptionalAction, default=None, dest="build_headers",
        help="Build the kernel -headers subpackage (default: on, per kernel.toml "
             "build_headers). --no-headers drops it from the build; DKMS modules "
             "(nvidia-open-dkms, virtualbox, …) and any out-of-tree module then "
             "cannot rebuild and will not load on reboot.")
    p_kernel.add_argument("--docs",
        action=argparse.BooleanOptionalAction, default=None, dest="build_docs",
        help="Build the kernel -docs subpackage (default: off, per kernel.toml "
             "build_docs). Pass --docs to build the kernel HTML/man documentation.")
    p_kernel.add_argument("--keep-hotplug-drivers",
        action=argparse.BooleanOptionalAction, default=None, dest="keep_hotplug_drivers",
        help="Re-enable hotplug driver classes (USB, USB4/Thunderbolt, MMC/SD, "
             "hot-plug PCI/CardBus, hot-plug HID) as modules AFTER config "
             "minimization, so devices plugged in later still work (default: off, "
             "per kernel.toml keep_hotplug_drivers). Only meaningful when a "
             "minimizing kconfig_targets sequence (e.g. localmodconfig) is set.")
    p_kernel.add_argument("--autofdo", choices=("record", "capture", "use"),
        dest="kernel_fdo",
        help="Sample-based kernel optimization (AutoFDO; LLVM toolchain only). "
             "Three steps spanning reboots: 'record' builds+installs a profiling "
             "kernel (CONFIG_AUTOFDO_CLANG, stock name); 'capture' prints the "
             "host-tailored perf + create_llvm_prof commands to run on the booted "
             "profiling kernel (no build); 'use' rebuilds consuming the collected "
             "profile and installs it as <pkgname>-sysforge alongside the stock "
             "kernel for bootloader fallback.")
    p_kernel.add_argument("--propeller", action="store_true", dest="kernel_propeller",
        help="Layer Propeller (basic-block layout) on the --autofdo cycle. "
             "Requires --autofdo; adds CONFIG_PROPELLER_CLANG and the Propeller "
             "profile pair. Recommended over BOLT for the kernel.")
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
    p_kernel.add_argument("--allow-no-fallback", action="store_true",
        dest="allow_no_fallback",
        help="Override the boot-safety guarantee that a fallback kernel "
             "(stock linux/linux-lts with a boot image) exists before "
             "installing a custom kernel. Without a fallback, a broken custom "
             "kernel leaves no recovery path short of a live USB.")
    p_kernel.add_argument("--skip-boot-audit", action="store_true",
        dest="skip_boot_audit",
        help="Override the pre-install boot-critical kconfig audit (Gate 2). "
             "By default a built kernel that drops the root filesystem / "
             "storage controller / core boot infra aborts before install; this "
             "flag installs it anyway. Dangerous — can leave the system unbootable.")
    p_kernel.set_defaults(verb_cls=RunKernelVerb)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Top-level COMMAND help tiers (2.5.0-F1). Presentation-only grouping so a new
# user can tell routine verbs from ad-hoc introspection instead of reading one
# flat, registration-ordered block. This tuple is the single source of truth:
# `_TieredHelpFormatter` renders `sysforge --help` from it, and
# tools/gen_options.py orders the man-page COMMANDS sections by
# `tiered_command_order()` — keep every user-facing verb in exactly one tier
# (a `check_completions`-style parity test guards against drift). The internal
# `completions` verb is deliberately absent (it carries no help text and never
# appears in the listing).
_COMMAND_TIERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Everyday", ("build", "update", "fetch", "search")),
    ("Inspect", ("doctor", "resolve", "env", "log", "state", "artifact")),
    ("Maintain",
     ("setup", "config", "packages", "run", "revert-to-stock", "uninstall")),
)


def tiered_command_order() -> list[str]:
    """Flat top-level COMMAND order matching the ``sysforge --help`` tiers
    (2.5.0-F1). Consumed by tools/gen_options.py to keep the man-page COMMANDS
    section in lockstep with the help grouping."""
    return [name for _label, names in _COMMAND_TIERS for name in names]


class _TieredHelpFormatter(argparse.HelpFormatter):
    """Render the top-level COMMAND list grouped into usage tiers (2.5.0-F1)
    rather than one flat block.

    argparse collapses every subparser into a single ``_SubParsersAction``
    pseudo-group, so there is no per-command category hook — we intercept that
    one action here and re-emit its choices under ``_COMMAND_TIERS`` headers.
    Every other action (options, the ``COMMAND`` metavar line, per-verb help)
    formats exactly as the base class would; sub-verb help (``sysforge build
    --help``) is untouched because those parsers use the default formatter."""

    def _format_action(self, action):
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)

        subactions = {sa.dest: sa for sa in action._get_subactions()}
        # Bare ``COMMAND`` metavar line (matches argparse's no-help header).
        parts = ["%*s%s\n" % (self._current_indent, "",
                              self._format_action_invocation(action))]
        self._indent()  # tier labels one level under COMMAND
        for label, names in _COMMAND_TIERS:
            parts.append("\n%*s%s:\n" % (self._current_indent, "", label))
            self._indent()  # commands one level under the tier label
            for name in names:
                sa = subactions.get(name)
                if sa is not None:
                    parts.append(super()._format_action(sa))
            self._dedent()
        self._dedent()
        return self._join_parts(parts)


def _build_parser() -> argparse.ArgumentParser:
    """Return the top-level ArgumentParser. Called by main(), tools/gen_options.py
    (man-page COMMANDS generation), and tools/check_shipped.py (completions parity)."""
    from sysforge import __version__
    parser = argparse.ArgumentParser(
        prog="sysforge",
        description="Arch Linux build and maintenance suite with compiler-optimized builds.",
        formatter_class=_TieredHelpFormatter,
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
    parser.add_argument(
        "--quiet",
        action="store_true",
        dest="quiet_global",
        help=(
            "Suppress all non-error output (verbosity 0), overriding -v and the "
            "[log] verbosity config default. File logs are unaffected."
        ),
    )
    parser.add_argument(
        "--py-profile",
        action="store_true",
        dest="py_profile",
        help=(
            "Run the verb under cProfile and print the top functions "
            "(by cumulative time) to stderr at exit."
        ),
    )
    parser.add_argument(
        "--py-profile-out",
        metavar="FILE",
        dest="py_profile_out",
        help=(
            "Write raw cProfile stats to FILE for later analysis "
            "(pstats/snakeviz). Implies --py-profile."
        ),
    )
    parser.add_argument(
        "--timings",
        action="store_true",
        help="Print a wall-clock phase timing report after build/update runs.",
    )
    parser.add_argument(
        "--no-throttle",
        action="store_true",
        dest="no_throttle",
        help=(
            "Ignore the configured build throttle (nice/ionice/cpu_quota/jobs) "
            "for this run — build at normal, unthrottled priority."
        ),
    )
    parser.add_argument(
        "--turbo",
        action="store_true",
        dest="turbo",
        help=(
            "Run the build at *higher* than default priority (negative niceness, "
            "best-effort IO, no CPU/job cap). Stronger than --no-throttle; "
            "lowering niceness may need privilege (best-effort — degrades quietly)."
        ),
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default=None,
        help=(
            "Colorize output. 'auto' (default) colours when writing to a "
            "terminal and honours NO_COLOR/FORCE_COLOR; 'always' forces colour "
            "on (e.g. when piping into a pager); 'never' disables it. Overrides "
            "the [ui] color config key."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    _add_artifact_parser(sub)
    _add_build_parser(sub)
    _add_fetch_parser(sub)
    _add_update_parser(sub)
    _add_resolve_parser(sub)
    _add_doctor_parser(sub)
    _add_packages_parser(sub)
    _add_state_parser(sub)
    _add_revert_parser(sub)
    _add_run_parser(sub)
    _add_search_parser(sub)
    _add_setup_parser(sub)
    _add_uninstall_parser(sub)
    _add_env_parser(sub)
    _add_log_parser(sub)
    _add_config_parser(sub)

    # completions (used by shell completion scripts; not user-facing)
    p_completions = sub.add_parser("completions")
    p_completions.add_argument(
        "resource",
        choices=["packages", "manifest", "local", "state", "makepkg-flags"],
    )
    p_completions.set_defaults(verb_cls=CompletionsVerb)

    return parser


_INSTALL_BEARING_COMMANDS = frozenset(
    {"build", "update", "run", "setup"}
)


def _gate_sentinel_check(args) -> bool:
    """True when ``cli.main`` should call ``check_and_recover_stale_sentinel``.

    Install-bearing commands (``build``/``update``/``run``/``setup``)
    gate on a stale sentinel, except when the invocation is
    explicitly read-only (``--dry-run``). The inner verb-runner sentinel
    scope already opts out under ``--dry-run`` (see ``UpdateVerb.pre_check``);
    keeping the outer CLI gate in lockstep avoids blocking ``sysforge update
    --dry-run`` on a sentinel from an earlier mutating run the user is still
    investigating.
    """
    cmd = getattr(args, "command", None)
    if cmd not in _INSTALL_BEARING_COMMANDS:
        return False
    return not getattr(args, "dry_run", False)


def _strip_venv_from_path() -> None:
    """Scrub sysforge's own venv from inherited PATH/VIRTUAL_ENV/PYTHONPATH.

    Sysforge is typically launched from `~/src/sysforge/.venv/bin/sysforge`
    after the user's shell has activated the venv. Without this strip, any
    code that captures `os.environ["PATH"]` (e.g. `_stage_env` in the PGO
    toolchain stage) carries `.venv/bin` into the makepkg subprocess, where
    `python -m build`-style PKGBUILD steps resolve `python` to the venv
    interpreter — which lacks PEP-517 deps and dies with `No module named
    build`. Gated on a real venv (`sys.prefix != sys.base_prefix`) so a
    packaged install in `/usr/bin` doesn't accidentally strip `/usr/bin`.
    """
    if sys.prefix == sys.base_prefix:
        return
    venv_bin = str(Path(sys.executable).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    cleaned = [p for p in path_parts if p != venv_bin]
    if len(cleaned) == len(path_parts):
        return
    os.environ["PATH"] = os.pathsep.join(cleaned)
    os.environ.pop("VIRTUAL_ENV", None)
    os.environ.pop("PYTHONPATH", None)
    _log.info(f"Stripped venv bin from PATH: {venv_bin}")


def main():
    # One home for Ctrl-C (1.2.0-F39): a routine abort must not unwind as a
    # raw traceback. Verbs keep raising KeyboardInterrupt normally — a
    # mutating verb's sentinel_scope persists its recovery sentinel on the
    # way up before this handler sees the interrupt.
    try:
        _main()
    except KeyboardInterrupt:
        from sysforge.ui import progress
        progress.shutdown()  # release the DECSTBM scroll region
        log.error("[SYSFORGE]", "aborted (Ctrl-C)")
        sys.exit(130)  # 128 + SIGINT, the conventional interrupt exit


def _main():
    _strip_venv_from_path()
    from sysforge.primitives.resource_guard import install as _install_resource_guard
    _install_resource_guard()
    sys.argv[1:] = _patch_makepkg_argv(
        _extract_implicit_makepkg_flags(
            _hoist_global_flags(_hoist_verbosity_flags(sys.argv[1:]))
        )
    )
    # 3.0.0: the flat `doctor` flags were removed. Intercept them here so the
    # user gets their replacement instead of argparse's bare "unrecognized
    # arguments" (2.6.1-F1). Delete alongside _DOCTOR_MIGRATION in 3.1.0.
    from sysforge.doctor import doctor_migration_hint
    _hint = doctor_migration_hint(sys.argv[1:])
    if _hint:
        log.error("[SYSFORGE]", _hint)
        sys.exit(2)

    parser = _build_parser()
    args = parser.parse_args()
    log.set_verbosity(_resolve_verbosity(args))
    log.set_color_mode(_resolve_color_mode(getattr(args, "color", None)))
    from sysforge.primitives.build_throttle import set_run_override
    set_run_override(_resolve_throttle_override(args))
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
    # First-install init notice (F1): on a fresh package install a marker file
    # is dropped in the state dir; advise running the reconfigure + hardware
    # bootstrap stages until both complete, then self-delete. Best-effort,
    # never blocks. Skip completions (machine-readable output must stay clean).
    if getattr(args, "command", None) != "completions":
        from sysforge.primitives.init_notice import maybe_emit_init_notice
        maybe_emit_init_notice(getattr(args, "state_dir", None))
    verb_cls = getattr(args, "verb_cls", None)
    if verb_cls is None:
        _log.error("No verb dispatcher set for this command — argparse misconfiguration")
        sys.exit(2)
    sys.exit(_dispatch(verb_cls, args))


def _dispatch(verb_cls, args) -> int:
    """Run the verb, optionally under cProfile (--py-profile/--py-profile-out)."""
    if not (args.py_profile or args.py_profile_out):
        return run_verb(verb_cls(), args)
    import cProfile
    import pstats
    prof = cProfile.Profile()
    prof.enable()
    try:
        return run_verb(verb_cls(), args)
    finally:
        # Verbs may sys.exit() inside execute — emit stats regardless.
        prof.disable()
        from sysforge.ui import progress
        progress.clear()
        if args.py_profile_out:
            prof.dump_stats(args.py_profile_out)
        pstats.Stats(prof, stream=sys.stderr).sort_stats("cumulative").print_stats(25)
