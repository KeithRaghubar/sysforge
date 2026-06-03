"""
stages/toolchain.py — stage 6: LLVM toolchain build (GCC is register-only)

Opt-in: stage is a clean no-op if /etc/sysforge/toolchain.toml is absent or
has enabled = false.  Systems that skip this stage use whatever compiler is
already installed; packages and kernel stages proceed normally.

The toolchain stage is the LLVM PGO bootstrap. The GCC path (compiler="gcc",
the default) **never builds GCC from source** — it just registers the system
`/usr/bin/gcc` and `/usr/bin/g++` paths into pipeline state so downstream
stages (packages, kernel) use them. Stock `gcc-libs` from pacman's
`base-devel` provides the runtime. Building GCC from source has no
meaningful performance gains and is error-prone, so sysforge doesn't own
that path.

toolchain.toml structure:
  enabled     = true     # must be true to activate the stage
  compiler    = "gcc"    # "gcc" (default, register-only) or "llvm" (build via PGO)
  pgo         = true     # only meaningful when compiler = "llvm"; ignored for gcc
  skip_build  = false    # LLVM only: register clang paths without building
  pgo_staging = "/var/tmp/sysforge-llvm-stage2"   # staging dir for pass-2 binaries
  pgo_store   = "/var/tmp/sysforge-llvm-pgo"      # dir for profraw/profdata files

  [packages]
  pgo     = ["llvm", "llvm-libs"]
  non_pgo = ["clang", "lld", "polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
  lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", ...]

LLVM PGO bootstrap (4 passes, only when pgo = true):
  Pass 1a — system compiler + -fprofile-generate=<pgo_store>/; builds ONLY the
            pgo list (llvm, llvm-libs).  makepkg runs WITHOUT --install; outputs
            are extracted to pgo_staging1 (stage1) by _pgo_pass1_stage so the
            live /usr is never touched.  Both packages stage — including the
            cmake-config / static-lib `llvm` package — so Pass 1b's
            find_package(LLVM) sees the staged headers + configs.  The
            instrumented .a archives staged alongside surface __llvm_profile_*
            link errors for anything that consumes LLVM component targets;
            Pass 1b and Pass 2 work around that via _profile_runtime_ldflag().
            Spurious profraw from CMake feature probes is purged before Pass 1b.
  Pass 1b — non-instrumented build of the non_pgo packages (clang, lld,
            compiler-rt, polly, openmp, spirv-llvm-translator) against stage1.
            CMAKE_PREFIX_PATH=<staging1>/usr points find_package(LLVM) at stage1
            so the new clang/lld link against stage1's libLLVM.so and are ABI-
            coherent with it.  LD_LIBRARY_PATH is deliberately NOT set —
            forcing the host /usr/bin/clang to load stage1's libLLVM would
            recreate the version-skew failure mode this refactor exists to
            prevent.  Outputs are extracted into the same pgo_staging1, making
            stage1 self-sufficient: it now has a working clang and a working
            libLLVM, both built from the in-tree LLVM source, both ABI-coherent.
  Pass 2  — training run.  CC=<staging1>/usr/bin/clang (built in Pass 1b);
            the Pass-2 env redirects dyld/cmake at stage1 via LD_LIBRARY_PATH,
            CMAKE_PREFIX_PATH, PATH.  The running clang and the libLLVM it
            loads are guaranteed coherent because they were built together —
            no possibility of version drift against /usr.  Builds pgo +
            non_pgo (lib32 excluded); the act of running stage1's clang
            against stage1's instrumented libLLVM generates profraw as a
            side effect.  CCACHE_DISABLE=1 / SCCACHE_DISABLE=1 injected so
            cache tools cannot bypass the instrumented compiler and silently
            produce no profraw.  LLVM_PROFILE_FILE uses %m_%p (per-module-
            hash + per-PID) so parallel make -j clang processes each write
            their own profraw without contending on one file.  Background
            daemon merges profraw every _PGO_MERGE_INTERVAL seconds with
            adaptive batch sizing (_PROFRAW_MERGE_BATCH_MAX →
            _PROFRAW_MERGE_BATCH_MIN on OOM).  llvm-profdata invoked with
            RLIMIT_AS lifted (lift_for_child) so it is not constrained by
            the sysforge controller's 2 GiB cap.  No system install; Pass 2
            binaries extracted to pgo_staging (stage2).  Merged profdata size
            logged at [INFO]; warns if below _PGO_PROFDATA_MIN_BYTES (likely
            indicates bypassed compilation).
  Pass 3  — CC=staged clang from stage2 if available, else system clang.
            CFLAGS/LDFLAGS += -fprofile-use=<profdata>; LTO disabled via
            LTOFLAGS="" (ThinLTO + IR PGO causes non-PIC vtable relocations
            in lld's ThinLTO codegen for libLLVM.so).  Installs all packages
            (pgo + non_pgo + lib32) via _pgo_install(); staging prefixes
            removed on success.  Profdata preserved with a version sidecar
            (clang.profdata.version) for reuse by sysforge update.

  A sudo keepalive thread refreshes credentials every _SUDO_KEEPALIVE_INTERVAL
  seconds throughout all four passes.

Compiler propagation:
  On completion writes cc/cxx/ld to pipeline_state.toml [stages.toolchain.result]
"""

import contextlib
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("TOOLCHAIN")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.config import find_pkgbuild, load_sysforge_toml
from sysforge.primitives.llvm_state import (
    collect_llvm_state,
    evaluate_strict,
    render_preflight,
)
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE
from sysforge.primitives import toolchain_safety
from sysforge.primitives.pacman import batch_install_pkgs, cached_pkg_files_for
from sysforge.primitives.makepkg_wrapper import SYNC_FLAGS
from sysforge.primitives.makepkg_wrapper import run as makepkg_run
from sysforge.build_core import make_build_options
from sysforge.primitives.prompt import is_interactive, prompt_choice
from sysforge.primitives.resource_guard import lift_for_child
from sysforge.primitives.stage_sentinel import sentinel_scope
from sysforge.ui import progress
from sysforge.primitives.source_sync import (
    STATUS_DIVERGED,
    STATUS_FAILED,
    STATUS_PURGE_REFUSED,
    STATUS_RATE_LIMITED,
    SyncRequest,
    get_scheduler,
)

_SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED,
})


def _sync_pkgbuild_dirs(
    pkgbuild_map: dict[str, "Path"],
    *,
    cleansrc: bool = False,
    cleansrc_force: bool = False,
) -> None:
    """
    Sync each unique resolved PKGBUILD directory through ``SourceSyncScheduler``.

    Mirrors the pattern in ``sysforge.update._sync_sources``: a single batched
    AUR RPC short-circuit followed by sequential per-pkgbase requests. Each
    pkgbase is classified as ``source="repo"`` (in any pacman sync DB) or
    ``source="aur"`` via a single batched ``pacman -Si`` (``repo_packages``)
    so that the scheduler's ``_clone`` path picks the right transport
    (``pkgctl_checkout`` for repo, ``aur_clone`` for AUR) on the first sync
    or after a ``--cleansrc`` purge. Without this classification, cleansrc
    on a repo package (clang/llvm/lld/...) would purge the tree then try
    to re-clone from AUR and silently leave the dir empty.

    ``cleansrc`` / ``cleansrc_force`` mirror the CLI flags: when set the
    scheduler purges + re-clones each tree, with ``cleansrc_force`` further
    bypassing the dirty-tree refusal in ``purge_src``.

    Blocker statuses (``STATUS_FAILED`` / ``STATUS_RATE_LIMITED`` /
    ``STATUS_PURGE_REFUSED``) raise ``RuntimeError``; ``STATUS_DIVERGED`` is
    surfaced as a warning so users can opt to keep their local edits.
    """
    from sysforge.primitives.aur import repo_packages

    git_cfg = (load_sysforge_toml().get("git", {}) or {})
    aur_cfg = (load_sysforge_toml().get("aur", {}) or {})
    fetch_timeout = git_cfg.get("fetch_timeout", git_cfg.get("pull_timeout", 30))
    clone_timeout = git_cfg.get("clone_timeout", 60)

    scheduler = get_scheduler(
        cleansrc=cleansrc or cleansrc_force,
        cleansrc_force=cleansrc_force,
        min_fetch_interval_ms=aur_cfg.get("min_fetch_interval_ms", 500),
        rate_limit_abort_s=aur_cfg.get("rate_limit_abort_s", 120.0),
        fetch_timeout=fetch_timeout,
        clone_timeout=clone_timeout,
    )

    pkgbases: list[str] = []
    dirs: list[Path] = []
    seen: set[str] = set()
    for path in pkgbuild_map.values():
        pkgbuild_dir = path.parent if path.name == "PKGBUILD" else path
        key = str(pkgbuild_dir)
        if key in seen:
            continue
        seen.add(key)
        pkgbases.append(pkgbuild_dir.name)
        dirs.append(pkgbuild_dir)

    if not pkgbases:
        return

    in_repo = repo_packages(pkgbases) if pkgbases else set()

    reqs: list[SyncRequest] = [
        SyncRequest(
            pkgbase=pkgbase,
            pkgbuild_dir=pkgbuild_dir,
            source="repo" if pkgbase in in_repo else "aur",
        )
        for pkgbase, pkgbuild_dir in zip(pkgbases, dirs)
    ]

    # Repo packages have no AUR-RPC entry; priming the RPC with their
    # names just wastes a request. Mirrors update.py:_sync_sources.
    aur_bases = [r.pkgbase for r in reqs if r.source != "repo"]
    if aur_bases:
        scheduler._ensure_rpc(aur_bases)

    failures: list[str] = []
    for req in reqs:
        result = scheduler.request(req)
        if result.status in _SYNC_BLOCKING_STATUSES:
            failures.append(
                f"{req.pkgbase}: {result.status} — {result.error or result.status}"
            )
        elif result.status == STATUS_DIVERGED:
            _log.warn(
                f"{req.pkgbase}: {result.error or 'divergent upstream'} — "
                "build will use the local PKGBUILD; rerun with --cleansrc "
                "to discard local edits and re-clone"
            )

    scheduler.close()

    if failures:
        raise RuntimeError(
            "[TOOLCHAIN] PKGBUILD sync failed:\n  " + "\n  ".join(failures)
        )

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_DEFAULT_LLVM_PGO = ["llvm", "llvm-libs"]
_DEFAULT_LLVM_NON_PGO = ["clang", "lld", "polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
_DEFAULT_LLVM_LIB32 = [
    "lib32-llvm",
    "lib32-llvm-libs",
    "lib32-clang",
    "lib32-spirv-llvm-translator",
]
_DEFAULT_STAGING_1 = "/var/tmp/sysforge-llvm-stage1"
_DEFAULT_STAGING = "/var/tmp/sysforge-llvm-stage2"
_DEFAULT_PGO_STORE = "/var/tmp/sysforge-llvm-pgo"

# Makepkg flags permitted through to PGO builds from user -m input.
# Only force-rebuild is safe; flags that alter build flow (e.g. --noextract,
# --nobuild, --noprepare) would corrupt the instrumentation/use sequence.
_PGO_ALLOWED_MAKEPKG_FLAGS = {"-f", "--force"}

# Interval (seconds) between intermediate profraw merges during Pass 2.
_PGO_MERGE_INTERVAL = 15

# How often (seconds) to refresh sudo credentials during the PGO build sequence.
# The 4-pass build can run for 2+ hours unattended. The keepalive calls sudo
# from the sysforge process, and _pgo_install also calls sudo from the same
# process, so they share the same timestamp entry regardless of timestamp_type.
_SUDO_KEEPALIVE_INTERVAL = 60

# Adaptive batch sizing for llvm-profdata merge. Each invocation starts at
# _PROFRAW_MERGE_BATCH_MAX files; on failure the batch is halved and retried
# at the same position. Shrinkage persists for the remainder of that merge
# call (next daemon wakeup resets to max). Gives up when batch_size falls
# below _PROFRAW_MERGE_BATCH_MIN and logs a warning.
_PROFRAW_MERGE_BATCH_MAX = 128
_PROFRAW_MERGE_BATCH_MIN = 8

# Profraw files modified more recently than this (seconds) are skipped during
# merges — they may still be actively written by a clang process, and merging
# a partial write causes SIGBUS in llvm-profdata.
_PROFRAW_SETTLE_SECS = 10

# Minimum expected profdata size after a real Pass 2 training run (bytes).
# A genuine LLVM self-compilation produces hundreds of MiB of profile data.
# Warn if the merged profdata is smaller — likely indicates compilation was
# bypassed (e.g. by a cache tool that slipped past CCACHE/SCCACHE_DISABLE).
_PGO_PROFDATA_MIN_BYTES = 10 * 1024 * 1024  # 10 MiB


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_toolchain_config() -> dict | None:
    """
    Load toolchain.toml. Returns None if absent (stage is a no-op).
    Raises RuntimeError on TOML parse failure.
    """
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with open(TOOLCHAIN_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise RuntimeError(
            f"[TOOLCHAIN] Failed to parse {TOOLCHAIN_PATH}: {e}"
        ) from None


def _package_lists(tcfg: dict) -> tuple[list[str], list[str], list[str]]:
    """
    Return (pgo_pkgs, non_pgo_pkgs, lib32_pkgs) for the LLVM toolchain build.

    Only called on the LLVM path — the GCC path short-circuits to register
    system gcc paths without building anything (stock `gcc`/`gcc-libs` from
    pacman/base-devel provide the runtime).
    """
    pkgs_cfg = tcfg.get("packages", {})
    pgo_pkgs = pkgs_cfg.get("pgo", _DEFAULT_LLVM_PGO)
    non_pgo_pkgs = pkgs_cfg.get("non_pgo", _DEFAULT_LLVM_NON_PGO)
    lib32_pkgs = pkgs_cfg.get("lib32", _DEFAULT_LLVM_LIB32)
    return pgo_pkgs, non_pgo_pkgs, lib32_pkgs


# ---------------------------------------------------------------------------
# PKGBUILD resolution
# ---------------------------------------------------------------------------


def _resolve_all_pkgbuilds(
    names: list[str], config: dict, *,
    update: bool = True,
    cleansrc: bool = False,
    cleansrc_force: bool = False,
) -> dict[str, Path]:
    """
    Resolve PKGBUILD paths for all package names.

    Three-pass strategy to handle split packages (e.g. llvm-libs comes from the
    llvm PKGBUILD and has no standalone clone target):
      1. Local direct: check pkgbuild_src_dir/<name>/PKGBUILD without cloning.
      2. Split scan: parse already-found PKGBUILDs for their pkgname arrays;
         reuse the path if a match is found.
      3. Full resolve: fall back to find_pkgbuild() which may clone from AUR/repo.

    When ``update`` is True (default), every unique resolved PKGBUILD directory
    is then routed through ``SourceSyncScheduler`` so missing trees get cloned
    and pre-existing trees are refreshed against upstream — same RPC short-
    circuit, rate-limit, and dirty-tree handling as ``sysforge update``. Pass
    ``update=False`` (mapped from ``--no-update``) to use whatever is on disk
    verbatim. Blocker statuses raise ``RuntimeError``.

    Returns {name: pkgbuild_path}. Raises RuntimeError on any miss.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    resolved: dict[str, Path] = {}

    pkgbuild_dir: Path | None = None
    if config:
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            pkgbuild_dir = Path(raw).expanduser()

    # Pass 1 — local direct (no clone)
    remaining = []
    for name in names:
        if pkgbuild_dir:
            candidate = pkgbuild_dir / name / "PKGBUILD"
            if candidate.exists():
                resolved[name] = candidate.resolve()
                continue
        remaining.append(name)

    # Pass 2 — split-package scan: check pkgname arrays of already-resolved PKGBUILDs
    if remaining and resolved:
        coverage: dict[Path, set] = {}
        for path in set(resolved.values()):
            try:
                meta = parse_pkgbuild(path)
                pkgnames = meta.get("globals", {}).get("pkgname", [])
                if isinstance(pkgnames, str):
                    pkgnames = [pkgnames]
                coverage[path] = set(pkgnames)
            except Exception as e:
                _log.info(f"  split-package scan: parse failed for {path}: {e}")
                coverage[path] = set()

        still_remaining = []
        for name in remaining:
            matched = next(
                (p for p, provides in coverage.items() if name in provides), None
            )
            if matched:
                resolved[name] = matched
                _log.info(
                    f"  {name} → split package in {matched.parent.name}/"
                )
            else:
                still_remaining.append(name)
        remaining = still_remaining

    # Pass 3 — full resolution with potential clone; re-scan for split packages after each success
    errors = []
    remaining = list(remaining)
    i = 0
    while i < len(remaining):
        name = remaining[i]
        try:
            path = find_pkgbuild(name, config)
            resolved[name] = path
            # Re-run split scan: the freshly cloned PKGBUILD may cover other remaining names
            try:
                meta = parse_pkgbuild(path)
                pkgnames = meta.get("globals", {}).get("pkgname", [])
                if isinstance(pkgnames, str):
                    pkgnames = [pkgnames]
                pkgbase = meta.get("globals", {}).get("pkgbase")
                covered = set(pkgnames)
                if pkgbase:
                    covered.add(pkgbase)
                satisfied = []
                for r in remaining[i + 1 :]:
                    if r in covered:
                        resolved[r] = path
                        satisfied.append(r)
                        _log.info(
                            f"  {r} → split package in {path.parent.name}/",
                        )
                for r in satisfied:
                    remaining.remove(r)
            except Exception as e:
                _log.info(f"  split-package re-scan: parse failed for {path}: {e}")
        except FileNotFoundError as e:
            errors.append(str(e))
        i += 1

    if errors:
        raise RuntimeError(
            "[TOOLCHAIN] Could not resolve PKGBUILDs:\n  " + "\n  ".join(errors)
        )

    if update or cleansrc or cleansrc_force:
        _sync_pkgbuild_dirs(
            resolved,
            cleansrc=cleansrc,
            cleansrc_force=cleansrc_force,
        )

    return resolved


def _llvm_strict_enabled() -> bool:
    """Read [safety] llvm_strict_toolchain from sysforge.toml. Default True."""
    safety = load_sysforge_toml().get("safety", {}) or {}
    return bool(safety.get("llvm_strict_toolchain", True))


def _run_llvm_preflight(names: list[str], config: dict, options) -> None:
    """Surface LLVM source state and (when strict) refuse on dirty/diverged.

    Always renders the report so users see the situation. When strict mode
    is enabled (the default for the toolchain stage) and the report has
    blockers, the stage is aborted unless ``options.allow_dirty_llvm`` is
    set or the user accepts the prompt interactively.

    A PGO profdata version mismatch is never suppressible — building
    against a stale profdata silently corrupts the output.
    """
    report = collect_llvm_state(names, config, probe_fetch=True)
    if not report.states:
        return

    rendered = render_preflight(report, verbose=True)
    if rendered:
        _log.ui(rendered)

    allow_dirty = bool(getattr(options, "allow_dirty_llvm", False))
    if not _llvm_strict_enabled() and not report.has_pgo_profdata_mismatch:
        return

    blockers = evaluate_strict(report, allow_dirty=allow_dirty)
    if not blockers:
        return

    blocker_lines = "\n".join(f"  - {b}" for b in blockers)
    if not is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] LLVM safety pre-flight refused — strict mode "
            "blocked the run on the following:\n"
            f"{blocker_lines}\n"
            "Re-run with --allow-dirty-llvm to bypass dirty/diverged "
            "blockers (PGO profdata mismatches cannot be bypassed)."
        )

    _log.warn("LLVM safety pre-flight has blockers:")
    for b in blockers:
        _log.warn(f"  {b}")
    choice = prompt_choice(
        "Proceed with toolchain build despite LLVM blockers? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        tag="TOOLCHAIN",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            "[TOOLCHAIN] LLVM safety pre-flight aborted by user."
        )


def _show_resolution_table(
    pkgbuild_map: dict[str, Path], role_map: dict[str, str] | None = None
) -> None:
    _log.ui("─── PKGBUILD resolution ─────────────────────────────")
    for name, path in pkgbuild_map.items():
        role = f"  [{role_map[name]}]" if role_map and name in role_map else ""
        _log.ui(f"  {name:<36}  {path}{role}")
    _log.ui("─────────────────────────────────────────────────────")


def _confirm_or_abort(state_dir) -> None:
    """Prompt user to confirm. On abort, print resume command and raise.

    EOF (non-interactive) defaults to "y" so unattended runs proceed without
    prompting — matches the long-standing behaviour of this stage.
    """
    choice = prompt_choice(
        "Proceed with toolchain build? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="y",
        tag="TOOLCHAIN",
    )
    if choice not in ("y", "yes"):
        dir_str = str(state_dir) if state_dir else "/var/lib/sysforge"
        print(
            f"\n  Resume command: sysforge run pipeline --resume --state-dir {dir_str}\n",
            file=sys.stderr,
        )
        raise RuntimeError(
            "[TOOLCHAIN] Aborted by user. Use the resume command to return."
        )


# ---------------------------------------------------------------------------
# Single-package build helpers
# ---------------------------------------------------------------------------


def _build_pkg(
    name: str,
    pkgbuild_path: Path,
    options,
    cc: str | None = None,
    cxx: str | None = None,
    extra_flags: list | None = None,
    init_session: bool = False,
    compiler_flags_extra: str | None = None,
    linker_flags_extra: str | None = None,
    pgo_build: bool = False,
    pgo_env: dict | None = None,
    strip_flags: frozenset | set | None = None,
    toolchain_variant: str | None = None,
    owner_stage: str | None = None,
) -> None:
    """Build one package via makepkg_wrapper.run().

    ``toolchain_variant`` is stamped into build_state so ``sysforge update``
    can flag drift. Set only on install-bearing passes — intermediate PGO
    passes (1a/1b/2) leave it ``None`` so their (transient, soon-overwritten)
    build_state writes don't carry a misleading variant claim.

    ``owner_stage`` is the stage-ownership marker (``"toolchain"``) that makes
    ``sysforge update`` skip these LLVM packages by default and point the user
    at ``sysforge run toolchain`` instead. Like ``toolchain_variant`` it is set
    only on install-bearing passes; intermediate passes leave it ``None``.
    """
    if options.dry_run:
        cc_label = f" CC={cc}" if cc else ""
        _log.ui(f"[dry-run] would build {name}{cc_label}")
        return
    # Strip install flags — toolchain controls install/no-install via extra_flags.
    user_flags = [
        f for f in getattr(options, "makepkg_flags", []) if f not in ("-i", "--install")
    ]
    if pgo_build:
        dropped = [f for f in user_flags if f not in _PGO_ALLOWED_MAKEPKG_FLAGS]
        user_flags = [f for f in user_flags if f in _PGO_ALLOWED_MAKEPKG_FLAGS]
        if dropped:
            _log.warn(
                f"PGO build: ignoring -m flags that could corrupt the "
                f"instrumentation sequence: {dropped}",
            )
    combined_flags = list(extra_flags or []) + user_flags
    makepkg_run(pkgbuild_path, options=make_build_options(
        "toolchain", options,
        extra_flags=combined_flags,
        compiler_flags_extra=compiler_flags_extra,
        linker_flags_extra=linker_flags_extra,
        cc_override=cc,
        cxx_override=cxx,
        init_session=init_session,
        update=not options.no_update,
        strip_full_lto=pgo_build,
        extra_env=pgo_env,
        strip_flags=strip_flags,
        toolchain_variant=toolchain_variant,
        owner_stage=owner_stage,
    ))


def _build_pass(
    label: str,
    pkgbuild_map: dict[str, Path],
    options,
    cc: str | None = None,
    cxx: str | None = None,
    install: bool = True,
    compiler_flags_extra: str | None = None,
    linker_flags_extra: str | None = None,
    pgo_build: bool = False,
    pgo_env: dict | None = None,
    staged_deps: bool = False,
    toolchain_variant: str | None = None,
    owner_stage: str | None = None,
) -> None:
    """Build all packages in pkgbuild_map for one pass.

    Deduplicates by PKGBUILD directory: split packages that share a directory
    (e.g. llvm, llvm-libs, clang from the same PKGBUILD) are only built once.

    ``staged_deps=True`` means PKGBUILD-declared deps (notably ``llvm=<ver>``)
    are satisfied by a stage prefix (e.g. ``/var/tmp/sysforge-llvm-stage1``)
    rather than by installed pacman packages.  In that mode ``--syncdeps``/``-s``
    is stripped from the resolved profile's makepkg_flags and ``--nodeps`` is
    appended — otherwise makepkg would invoke ``sudo pacman -S llvm=<ver>``,
    fail with "target not found" (the version isn't published anywhere), and
    abort the pass.  Pass 1a sets staged_deps=False because it builds against
    the live system; Pass 1b/2/3 set staged_deps=True.
    """
    extra = ["--install"] if install else []
    if pgo_build:
        extra = ["--cleanbuild", "--force"] + extra
    strip_flags: frozenset | None = None
    if staged_deps:
        extra = extra + ["--nodeps"]
        strip_flags = SYNC_FLAGS
    _log.ui(f"─── {label} ──────────────────────────────────────────")
    total = len({p.parent for p in pkgbuild_map.values()})
    seen_dirs: set[Path] = set()
    first = True
    with progress.tracker(total, label) as tick:
        for name, pkgbuild_path in pkgbuild_map.items():
            pkg_dir = pkgbuild_path.parent
            if pkg_dir in seen_dirs:
                _log.ui(f"  {name} (split — built with {pkg_dir.name})")
                continue
            seen_dirs.add(pkg_dir)
            tick(name)
            _build_pkg(
                name,
                pkgbuild_path,
                options,
                cc=cc,
                cxx=cxx,
                extra_flags=extra,
                init_session=first,
                compiler_flags_extra=compiler_flags_extra,
                linker_flags_extra=linker_flags_extra,
                pgo_build=pgo_build,
                pgo_env=pgo_env,
                strip_flags=strip_flags,
                toolchain_variant=toolchain_variant,
                owner_stage=owner_stage,
            )
            first = False


# ---------------------------------------------------------------------------
# PGO staging extraction
# ---------------------------------------------------------------------------


def _extract_pkg_to_staging(pkg_file: Path, staging: Path) -> None:
    """Extract a .pkg.tar.* file to the staging directory."""
    staging.mkdir(parents=True, exist_ok=True)
    _log.ui(f"  Extracting {pkg_file.name} → {staging}")
    result = subprocess.run(
        [
            "tar",
            "--warning=no-unknown-keyword",
            "-xf",
            str(pkg_file),
            "-C",
            str(staging),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] tar extraction failed for {pkg_file}: "
            f"{result.stderr.decode().strip()}"
        )


def _extract_pass2_to_staging(
    pkgbuild_map: dict[str, Path], staging: Path, dry_run: bool
) -> None:
    """
    After Pass 2 build (no install), find .pkg.tar* in each build dir (or
    PKGDEST if set in the system makepkg.conf) and extract to staging prefix.
    The staged binaries are used as CC/CXX in Pass 3.
    """
    if dry_run:
        _log.ui(f"[dry-run] would extract pass-2 packages to {staging}")
        return

    from sysforge.primitives.config import parse_system_makepkg_conf

    sys_conf = parse_system_makepkg_conf()
    pkgdest_raw = sys_conf.get("PKGDEST")
    pkgdest = Path(pkgdest_raw).expanduser() if pkgdest_raw else None
    if pkgdest:
        _log.info(
            f"[PGO] PKGDEST={pkgdest} — searching there for Pass 2 packages",
        )

    _log.ui(f"─── Pass 2: staging extraction → {staging} ────────")
    for name, pkgbuild_path in pkgbuild_map.items():
        build_dir = pkgbuild_path.parent
        # PKGDEST takes precedence; fall back to PKGBUILD directory.
        search_dirs = [pkgdest] if pkgdest and pkgdest.is_dir() else []
        search_dirs.append(build_dir)
        pkgs: list[Path] = []
        for d in search_dirs:
            # *.pkg.tar* matches both compressed (.pkg.tar.zst) and
            # uncompressed (.pkg.tar) packages (PKGEXT='.pkg.tar').
            # Sort by mtime descending and take only the newest to avoid
            # extracting stale packages from previous runs in PKGDEST.
            candidates = [p for p in d.glob(f"{name}-*.pkg.tar*")
                          if not p.name.endswith(".sig")]
            if candidates:
                pkgs = [max(candidates, key=lambda p: p.stat().st_mtime)]
                break
        if not pkgs:
            searched = ", ".join(str(d) for d in search_dirs)
            raise RuntimeError(
                f"[TOOLCHAIN] No .pkg.tar* found for {name!r} in: {searched}. "
                "Pass 2 build may have failed."
            )
        for pkg_file in pkgs:
            _extract_pkg_to_staging(pkg_file, staging)
        _log.ui(f"  {name}: staged")


def _remove_staging(staging: Path) -> None:
    import shutil

    if staging.exists():
        _log.ui(f"Removing staging prefix: {staging}")
        shutil.rmtree(staging)


def _do_profraw_merge(pgo_store: Path, label: str) -> tuple[int, int]:
    """
    Merge all .profraw files under pgo_store into clang.profdata using an
    atomic tmp→rename so concurrent readers always see a complete file.
    If clang.profdata already exists it is included as an input (incremental).

    Only merges files that have not been modified in the last _PROFRAW_SETTLE_SECS
    seconds.  Files with a very recent mtime are likely still being written by
    an instrumented clang process; merging them would cause SIGBUS crashes and
    truncated-profile errors in llvm-profdata.

    Returns (files_merged, n_batches). files_merged is 0 if no settled .profraw
    files were found. Logs a warning on llvm-profdata failure but does not raise
    (callers decide).
    """
    import time

    now = time.time()
    all_profraw = list(pgo_store.glob("**/*.profraw"))
    profraw_files = [
        f for f in all_profraw if (now - f.stat().st_mtime) >= _PROFRAW_SETTLE_SECS
    ]
    if not profraw_files:
        return 0, 0

    profdata_path = pgo_store / "clang.profdata"
    tmp_path      = pgo_store / "clang.profdata.tmp"

    deleted    = 0
    n_batches  = 0
    batch_size = _PROFRAW_MERGE_BATCH_MAX
    i          = 0
    while i < len(profraw_files):
        batch  = profraw_files[i : i + batch_size]
        inputs = [str(profdata_path)] if profdata_path.exists() else []
        inputs += [str(f) for f in batch]

        result = subprocess.run(
            ["llvm-profdata", "merge", "--output", str(tmp_path)] + inputs,
            capture_output=True,
            text=True,
            preexec_fn=lift_for_child,
        )
        if result.returncode != 0:
            if batch_size > _PROFRAW_MERGE_BATCH_MIN:
                new_size = batch_size // 2
                _log.info(
                    f"[PGO] {label} merge failed at batch={batch_size} "
                    f"(exit {result.returncode}), retrying with batch={new_size}",
                )
                batch_size = new_size
                continue  # retry same position with smaller batch
            _log.warn(
                f"[PGO] {label} profraw merge failed at minimum batch size "
                f"({batch_size}) (exit {result.returncode}): "
                f"{result.stderr.strip()}",
            )
            return deleted, n_batches

        tmp_path.replace(profdata_path)
        for f in batch:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
        n_batches += 1
        i += batch_size

    return deleted, n_batches


def _sudo_keepalive_daemon(stop_event: threading.Event) -> None:
    """
    Background thread: refresh sudo credentials every _SUDO_KEEPALIVE_INTERVAL
    seconds throughout the 4-pass PGO build sequence.

    The 4-pass build can run unattended for 2+ hours. _pgo_install() calls
    sudo directly from the sysforge process, so its timestamp entry is the
    same one the keepalive refreshes — credential caching works correctly
    regardless of the sudoers timestamp_type setting.
    """
    while not stop_event.wait(_SUDO_KEEPALIVE_INTERVAL):
        result = subprocess.run(["sudo", "-v"])
        if result.returncode != 0:
            _log.warn(
                      "[PGO] sudo keepalive failed — install step may prompt "
                      "for a password")


def _collect_pgo_packages(pkgbuild_map: dict[str, Path]) -> list[Path]:
    """
    Use 'makepkg --packagelist' to discover the paths of packages built by
    each unique PKGBUILD in pkgbuild_map.  Returns only paths that exist on
    disk (i.e. packages that were actually produced) and excludes .sig files.
    """
    seen_dirs: set[Path] = set()
    packages: list[Path] = []
    for pkgbuild_path in pkgbuild_map.values():
        build_dir = pkgbuild_path.parent
        if build_dir in seen_dirs:
            continue
        seen_dirs.add(build_dir)
        result = subprocess.run(
            ["makepkg", "--packagelist"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            _log.warn(
                f"[PGO] makepkg --packagelist failed in {build_dir}: "
                f"{result.stderr.strip()}",
            )
            continue
        for line in result.stdout.splitlines():
            p = Path(line.strip())
            if p.exists() and not p.name.endswith(".sig"):
                packages.append(p)
    return packages


def _pgo_install(label: str, pkgbuild_map: dict[str, Path], dry_run: bool) -> None:
    """
    Install packages built by a PGO pass via a direct 'sudo pacman -U' call.

    makepkg is run WITHOUT --install for PGO passes so that the sudo credential
    prompt (if any) occurs here — immediately after the build — rather than
    buried inside a multi-hour makepkg run.  The build may have outlasted the
    sudoers timestamp_timeout, and keepalive approaches are unreliable across
    sudo timestamp_type configurations (tty, ppid, global).  By issuing the
    sudo call here we guarantee it happens at a clean, predictable point.

    The ABI-hazard scan that used to live here is now Gate 2
    (``_gate2_audit`` → ``toolchain_safety.scan_abi_hazards``), which runs
    *before* the snapshot + sentinel so a hazardous build aborts with nothing
    installed. This install is reached only after Gate 2 has cleared the built
    packages, and runs inside the sentinel; a ``pacman -U`` failure here raises
    so the caller can roll back to the snapshot.
    """
    if dry_run:
        _log.ui(f"[dry-run] would install packages from {label}")
        return
    pkgs = _collect_pgo_packages(pkgbuild_map)
    if not pkgs:
        raise RuntimeError(
            f"[TOOLCHAIN] No built packages found for {label} — "
            "check that the build completed successfully"
        )
    _log.ui(f"[PGO] Installing {len(pkgs)} package(s) ({label}):")
    for p in pkgs:
        _log.ui(f"  {p.name}")
    result = subprocess.run(
        ["sudo", "pacman", "-U", "--noconfirm"] + [str(p) for p in pkgs]
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] pacman -U failed (exit {result.returncode}) for {label}"
        )


def _has_llvm_cmake_config(pkg_file: Path) -> bool:
    """Return True if pkg_file contains LLVM cmake config files (usr/lib/cmake/llvm/).

    Used by _pgo_pass1_install to identify the static-lib / cmake-config package
    (typically named 'llvm') so it can be excluded from the Pass 1 system install.
    """
    result = subprocess.run(
        ["tar", "--list", "--file", str(pkg_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    return any("cmake/llvm" in line for line in result.stdout.splitlines())


def _pgo_pass1_stage(
    pkgbuild_map: dict[str, Path], staging1: Path, dry_run: bool
) -> None:
    """Extract every Pass 1a package into ``staging1`` instead of installing to ``/usr``.

    Path B of the PGO audit (see DESIGN.md §Toolchain stage / PGO): Pass 1
    must not touch the live root.  Installing an instrumented ``libLLVM.so``
    over the system copy leaves the pre-existing ``/usr/bin/clang`` ABI-
    incompatible with the just-installed lib (weak/inlined symbols elided by
    ``-fprofile-generate`` are no longer exported) and breaks every subsequent
    invocation — including the CMake compiler check that Pass 2's makepkg run
    triggers.  Staging keeps the instrumented surface off the system entirely.

    Phase 2: the cmake-config / static-lib package (typically ``llvm``) is
    **included** here, so Pass 1b and Pass 2's ``find_package(LLVM)`` find
    stage1's headers and cmake configs.  The instrumented ``.a`` archives that
    land alongside still cause ``__llvm_profile_*`` link errors for anything
    that consumes LLVM component targets — Pass 1b and Pass 2 work around it
    via ``linker_flags_extra = _profile_runtime_ldflag()``.
    """
    if dry_run:
        _log.ui(f"[dry-run] would extract Pass 1a packages to {staging1}")
        return

    all_pkgs = _collect_pgo_packages(pkgbuild_map)
    if not all_pkgs:
        raise RuntimeError(
            "[TOOLCHAIN] No built packages found for Pass 1 — "
            "check that the build completed successfully"
        )

    _log.ui(f"[PGO] Staging {len(all_pkgs)} Pass 1a package(s) → {staging1}:")
    for pkg_file in all_pkgs:
        _extract_pkg_to_staging(pkg_file, staging1)


def _stage_env(staging: Path) -> dict[str, str]:
    """Env-var injection so makepkg's child processes (cmake/clang/ld) find a
    staged LLVM prefix instead of (or before) the live ``/usr``.

    Prepends to the inheritable variables so anything actually present in
    ``staging`` wins, but the system fallback still resolves libraries we
    haven't staged (``libstdc++.so``, ``glibc`` data, etc.).  Used by Pass 2
    (pointed at stage1) and Pass 3 (pointed at stage2).
    """
    usr = staging / "usr"

    def _prepend(var: str, value: str) -> str:
        existing = os.environ.get(var)
        return f"{value}:{existing}" if existing else value

    return {
        "LD_LIBRARY_PATH": _prepend("LD_LIBRARY_PATH", str(usr / "lib")),
        "CMAKE_PREFIX_PATH": _prepend("CMAKE_PREFIX_PATH", str(usr)),
        "PATH": _prepend("PATH", str(usr / "bin")),
    }


def _system_llvm_is_instrumented() -> bool:
    """Return True if the system libLLVMSupport.a contains PGO instrumentation symbols.

    Used before Pass 2 to detect whether a previous Pass 1 install left instrumented
    LLVM static libs on the system.  If so, packages that call find_package(LLVM) and
    link against those libs (e.g. a separate clang PKGBUILD) will need the profile
    runtime in LDFLAGS to satisfy the linker.
    """
    llvm_support = Path("/usr/lib/libLLVMSupport.a")
    if not llvm_support.exists():
        return False
    result = subprocess.run(
        ["nm", "--defined-only", str(llvm_support)],
        capture_output=True,
        text=True,
        check=False,
    )
    return "__llvm_profile_" in result.stdout


def _profile_runtime_ldflag() -> str | None:
    """Return '-L<runtime_dir> -lclang_rt.profile-<arch>' if the profile runtime exists.

    Returns None if the runtime library cannot be located, with a log warning.
    Injected into LDFLAGS for both Pass 2 and Pass 3 when the system LLVM static
    libs are instrumented (residual from a prior Pass 1), so packages linking
    against them can resolve __llvm_profile_* symbols.
    """
    rt_result = subprocess.run(
        ["/usr/bin/clang", "--print-runtime-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rt_result.returncode != 0 or not rt_result.stdout.strip():
        _log.warn(
            "[PGO] Could not determine clang runtime dir (clang --print-runtime-dir failed); "
            "Pass 2 and Pass 3 may fail with undefined __llvm_profile_* symbols. "
            "Run: sudo pacman -S llvm  to restore an uninstrumented system LLVM.",
        )
        return None

    arch_result = subprocess.run(
        ["uname", "-m"], capture_output=True, text=True, check=False
    )
    arch = arch_result.stdout.strip()
    runtime_dir = rt_result.stdout.strip()
    profile_lib = Path(runtime_dir) / f"libclang_rt.profile-{arch}.a"

    if not profile_lib.exists():
        _log.warn(
            f"[PGO] Profile runtime not found at {profile_lib}; "
            "Pass 2 and Pass 3 may fail with undefined __llvm_profile_* symbols. "
            "Install compiler-rt or run: sudo pacman -S llvm",
        )
        return None

    return f"-L{runtime_dir} -lclang_rt.profile-{arch}"


def _validate_pgo_environment(dry_run: bool) -> None:
    """Pre-flight check for the LLVM PGO build sequence.

    Raises RuntimeError for conditions that will definitely cause a build
    failure.  Emits [WARN] for recoverable degraded states (residual
    instrumentation from a prior aborted Pass 1) so the user has a complete
    picture up front rather than discovering issues an hour into the build.

    Checks:
      • /usr/bin/clang present          — hard requirement for Pass 1 (system CC)
      • clang --version succeeds        — catches mismatched/broken shared libs
                                          (e.g. symbol lookup errors in libclang-cpp.so
                                          from packages installed by different aborted runs)
      • lld present                     — hard requirement for all passes
      • libLLVM-*.so instrumented       — warns; causes profraw noise during Pass 1
      • libLLVMSupport.a instrumented   — warns; handled by profile runtime LDFLAGS
                                          injection, but a clean install is preferred
    """
    if dry_run:
        _log.info("[PGO] Pre-flight: skipping environment check (dry-run)")
        return

    import shutil as _shutil

    clang_path = Path("/usr/bin/clang")
    if not clang_path.exists():
        raise RuntimeError(
            "[TOOLCHAIN] /usr/bin/clang not found — "
            "install clang before running the PGO build: sudo pacman -S clang"
        )

    # Smoke-test clang by actually compiling a trivial program.  --version does not
    # load libclang-cpp.so (it short-circuits before the compilation pipeline), so
    # it misses symbol-version mismatches from mixed aborted PGO runs.  A real
    # compilation fully exercises the dynamic linker and will surface errors like
    # "symbol lookup error: libclang-cpp.so: undefined symbol ..., version LLVM_22.1".
    clang_probe = subprocess.run(
        [str(clang_path), "-x", "c", "-", "-o", "/dev/null"],
        input="int main(void){return 0;}\n",
        capture_output=True, text=True, check=False,
    )
    if clang_probe.returncode != 0 or "symbol lookup error" in clang_probe.stderr:
        detail = (clang_probe.stderr.strip() or clang_probe.stdout.strip())[:300]
        raise RuntimeError(
            f"[TOOLCHAIN] /usr/bin/clang is not functional — likely mismatched "
            f"packages from a prior aborted PGO run:\n  {detail}\n"
            "Restore a consistent set: sudo pacman -S llvm llvm-libs clang lld compiler-rt\n"
            "(Note: sysforge no longer installs clang/lld in Pass 1, so a mismatch\n"
            " here means the packages were already mixed before this run.)"
        )

    if not _shutil.which("lld"):
        raise RuntimeError(
            "[TOOLCHAIN] lld not found — "
            "install lld before running the PGO build: sudo pacman -S lld"
        )

    stale: list[str] = []

    # Instrumented shared lib: libLLVM.so installed by a prior Pass 1 does not
    # export __llvm_profile_* in its DYNAMIC symbol table (stripped), but does
    # contain __llvm_prf_* ELF sections.  Use readelf -S to detect these.
    llvm_sos = sorted(Path("/usr/lib").glob("libLLVM-*.so"))
    if llvm_sos:
        readelf_so = subprocess.run(
            ["readelf", "-S", str(llvm_sos[0])],
            capture_output=True, text=True, check=False,
        )
        if "__llvm_prf_" in readelf_so.stdout:
            stale.append(
                f"{llvm_sos[0].name} is instrumented (has __llvm_prf_* sections) — "
                "from a prior Pass 1 install; restore with: sudo pacman -S llvm llvm-libs"
            )

    # Instrumented static libs: causes undefined __llvm_profile_* linker errors
    # in Pass 2/3 for packages that call find_package(LLVM).  The build injects
    # the profile runtime into LDFLAGS to compensate, but a clean install avoids
    # the complexity entirely.
    if _system_llvm_is_instrumented():
        stale.append(
            "libLLVMSupport.a is instrumented — profile runtime will be injected "
            "into LDFLAGS for Pass 2 and Pass 3 automatically"
        )

    if stale:
        _log.warn(
            "[PGO] Pre-flight: system LLVM packages have residual instrumentation "
            "from a prior aborted Pass 1 install. "
            "For a clean build: sudo pacman -S llvm llvm-libs\n"
            + "\n".join(f"  • {s}" for s in stale),
        )

        if not is_interactive():
            raise RuntimeError(
                "[TOOLCHAIN] Aborting unattended PGO build: system LLVM packages "
                "have residual instrumentation from a prior aborted Pass 1 install. "
                "Restore clean packages before retrying: "
                "sudo pacman -S llvm llvm-libs"
            )
        answer = prompt_choice(
            "Continue with residual instrumentation? [y/N]: ",
            choices=("y", "yes", "n"),
            default="n",
            tag="TOOLCHAIN",
            level="WARN",
        )
        if answer not in ("y", "yes"):
            raise RuntimeError(
                "[TOOLCHAIN] Aborted — restore clean packages before retrying: "
                "sudo pacman -S llvm llvm-libs"
            )
    else:
        _log.info("[PGO] Pre-flight: system LLVM environment is clean")


def _profraw_merge_daemon(pgo_store: Path, stop_event: threading.Event) -> None:
    """
    Background thread: wake every _PGO_MERGE_INTERVAL seconds during Pass 2
    and merge accumulated .profraw files into the rolling clang.profdata.
    Keeps peak disk usage bounded during long instrumented builds.
    """
    while not stop_event.wait(_PGO_MERGE_INTERVAL):
        deleted, n_batches = _do_profraw_merge(pgo_store, "intermediate")
        if deleted:
            _log.newline()
            _log.info(
                f"[PGO] Intermediate merge: {deleted} .profraw file(s) merged"
                + (f" in {n_batches} batches" if n_batches > 1 else ""),
            )


def _merge_profraw(pgo_store: Path, dry_run: bool) -> Path:
    """
    Final profraw sweep after Pass 2 completes (daemon already stopped).

    Merges any .profraw files still on disk together with the existing
    clang.profdata produced by intermediate merges. If the daemon consumed
    everything there may be no remaining raws, which is fine.

    Fresh-file handling: if all remaining profraw files are younger than
    _PROFRAW_SETTLE_SECS (written in the very last seconds of the build),
    _do_profraw_merge skips them and returns 0.  When the background daemon
    already produced a profdata those files represent only the trailing tail
    of compilation data — we warn and proceed rather than aborting.

    Returns the path to clang.profdata.
    Raises RuntimeError if neither raws nor an existing profdata are present
    (indicates -fprofile-generate had no effect), or if settled profraw exists
    but llvm-profdata failed to merge it.
    """
    import time

    profdata_path = pgo_store / "clang.profdata"

    if dry_run:
        _log.ui(
            f"[dry-run] would finalize profraw merge → {profdata_path}"
        )
        return profdata_path

    profraw_files = list(pgo_store.glob("**/*.profraw"))
    has_profdata = profdata_path.exists()

    if not profraw_files and not has_profdata:
        raise RuntimeError(
            f"[TOOLCHAIN] No .profraw files and no profdata in {pgo_store} after Pass 2. "
            "Ensure the pgo packages are built with clang and -fprofile-generate "
            "was effective (check the build log)."
        )

    if not profraw_files:
        _log.info(
            "[PGO] All .profraw files already merged by background monitor",
        )
        return profdata_path

    deleted, n_batches = _do_profraw_merge(pgo_store, "final")
    if deleted == 0:
        # Distinguish between llvm-profdata failure and settle-filter exclusion.
        now = time.time()
        fresh = [
            f for f in profraw_files
            if (now - f.stat().st_mtime) < _PROFRAW_SETTLE_SECS
        ]
        if fresh and has_profdata:
            _log.warn(
                f"[PGO] {len(fresh)} fresh .profraw file(s) skipped (written "
                f"< {_PROFRAW_SETTLE_SECS}s ago at end of Pass 2); "
                "profile data from background merges is complete enough to proceed.",
            )
            return profdata_path
        raise RuntimeError(
            "[TOOLCHAIN] Final profraw merge produced no output — "
            + (
                f"{len(fresh)} profraw file(s) too fresh to merge safely and no "
                "profdata from background merges; Pass 2 may have produced no profile data"
                if fresh
                else "llvm-profdata may have failed (check warnings above)"
            )
        )
    _log.info(
        f"[PGO] Final merge: {deleted} remaining .profraw file(s) merged"
        + (f" in {n_batches} batches" if n_batches > 1 else ""),
    )
    return profdata_path


def _pgo_target_major(pgo_map: dict[str, Path]) -> str | None:
    """Return the LLVM major version of the in-tree PGO PKGBUILDs, or None.

    All pgo PKGBUILDs share the same pkgver (Gate 1's
    toolchain_safety.check_pkgver_lockstep aborts otherwise); the first one
    that parses cleanly wins.
    Mirrors the major extracted in _check_existing_profdata so the
    write/check pair always compares apples to apples.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    for name, path in pgo_map.items():
        try:
            meta = parse_pkgbuild(path)
            pkgver = meta.get("globals", {}).get("pkgver", "")
            if pkgver:
                return pkgver.split(".")[0]
        except Exception as e:
            _log.info(f"  pgo target major: parse failed for {path} ({name}): {e}")
            continue
    return None


def _write_profdata_version(pgo_store: Path, pgo_map: dict[str, Path]) -> None:
    """
    Write a version sidecar (clang.profdata.version) containing the LLVM
    major version the profdata was generated against — derived from the
    in-tree PGO PKGBUILDs (what Pass 2 instrumented), not from the system
    `pacman -Q llvm` which can disagree across a major bump.  Called right
    after Pass 2 completes so an aborted Pass 3 still leaves recoverable
    profdata that the next run can reuse via _check_existing_profdata.

    Failures are non-fatal — a missing sidecar just causes the next run to
    fall through to a full 4-pass rebuild rather than crashing.
    """
    try:
        major = _pgo_target_major(pgo_map)
        if major is None:
            _log.warn(
                "[PGO] Could not determine LLVM major from PGO PKGBUILDs — "
                "profdata version sidecar not written",
            )
            return
        version_path = pgo_store / "clang.profdata.version"
        version_path.write_text(major + "\n")
        _log.info(
            f"[PGO] Saved profdata version sidecar: LLVM {major} → {version_path}",
        )
    except Exception as e:
        _log.warn(f"[PGO] Could not write profdata version sidecar: {e}")


# ---------------------------------------------------------------------------
# Confirmation gate for the fragile PGO sub-flow
# ---------------------------------------------------------------------------


class _PGOAborted(RuntimeError):
    """User declined a PGO confirmation prompt (or non-TTY without --auto-pgo)."""


def _pgo_confirm(
    msg: str,
    *,
    default: str,
    eof_default: str,
    options,
    abort_msg: str,
) -> bool:
    """Confirmation gate for fragile PGO decision points.

    Returns True if the user (or `--auto-pgo`) approves; raises ``_PGOAborted``
    otherwise. Behaviour matrix:

      • ``--auto-pgo`` set        → no prompt; treat as approved.
      • TTY, no ``--auto-pgo``    → prompt; empty input → ``default``.
      • Non-TTY, no ``--auto-pgo``→ ``prompt_choice`` returns ``eof_default``
        (we set this to ``"n"`` everywhere, so the PGO path aborts cleanly with
        a message directing the user to pass ``--auto-pgo``).

    PGO fragility (silent mis-optimisation if profdata is wrong) is why we
    deliberately diverge from the rest of sysforge's automation-by-default
    posture here.
    """
    if getattr(options, "auto_pgo", False):
        return True
    if not is_interactive():
        raise _PGOAborted(
            f"{abort_msg} (non-interactive PGO requires --auto-pgo)"
        )
    answer = prompt_choice(
        msg,
        choices=("y", "n"),
        default=default,
        eof_default=eof_default,
        retry_on_invalid=False,
        tag="PGO",
        level="WARN",
    )
    if answer == "y":
        return True
    raise _PGOAborted(abort_msg)


# ---------------------------------------------------------------------------
# Profdata reuse check
# ---------------------------------------------------------------------------


def _resolve_skip_build_variant(pgo_store: Path) -> str:
    """Best-effort variant detection for the ``skip_build = true`` LLVM path.

    The skip_build branch registers paths without resolving PKGBUILDs, so we
    can't run the strict major-version match in ``_check_existing_profdata``.
    Fall back to a presence check: if both ``clang.profdata`` and the version
    sidecar exist in ``pgo_store``, the installed clang is the result of a
    prior PGO build and reports ``pgo_llvm``. Otherwise ``stock_llvm``.

    This reflects on-disk provenance (what the user is actually running),
    not just the stage's current action.
    """
    if (pgo_store / "clang.profdata").exists() and (pgo_store / "clang.profdata.version").exists():
        return "pgo_llvm"
    return "stock_llvm"


def _check_existing_profdata(
    pgo_store: Path,
    pgo_map: dict[str, Path],
) -> tuple[str, str | Path]:
    """
    Check whether compatible profdata exists for reuse.

    Compares the version sidecar (written by _write_profdata_version after a
    successful PGO build) against the LLVM major version in the pgo PKGBUILDs.

    Returns one of:
      ("ready",    profdata_path)  — profdata exists and major version matches
      ("mismatch", reason_str)     — profdata exists but major version differs
      ("absent",   reason_str)     — profdata or sidecar missing
    """
    profdata_path = pgo_store / "clang.profdata"
    version_path = pgo_store / "clang.profdata.version"

    if not profdata_path.exists():
        return ("absent", f"no profdata at {profdata_path}")
    if not version_path.exists():
        return ("absent", f"profdata version sidecar missing at {version_path}")

    saved_major = version_path.read_text().strip()

    # Extract target LLVM major version from the pgo PKGBUILDs.
    # All pgo PKGBUILDs should share the same pkgver (Gate 1's
    # toolchain_safety.check_pkgver_lockstep aborts otherwise); use the first one.
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    for name, path in pgo_map.items():
        try:
            meta = parse_pkgbuild(path)
            pkgver = meta.get("globals", {}).get("pkgver", "")
            if pkgver:
                target_major = pkgver.split(".")[0]
                if saved_major != target_major:
                    return (
                        "mismatch",
                        f"profdata is from LLVM {saved_major}, "
                        f"building LLVM {target_major}",
                    )
                return ("ready", profdata_path)
        except Exception as e:
            _log.info(f"  profdata reuse: parse failed for {path} ({name}): {e}")
            continue

    return ("absent", "cannot determine target LLVM version from PKGBUILDs")


# ---------------------------------------------------------------------------
# Post-install verification (LLVM only)
# ---------------------------------------------------------------------------

# Packages whose installed version must match across the LLVM toolchain set.
# A mismatch — typically from an interrupted `pacman -U` of one pass — is
# what produces the broken-GUI / missing-symbol failure mode this guard is
# here to catch. Shares the single source of truth with the preflight skew
# probe (LLVM_LOCKSTEP_SUITE) so the two checks never diverge.
_LLVM_VERSION_MATCH_SET = LLVM_LOCKSTEP_SUITE


def _query_pacman_versions(pkgnames: tuple[str, ...]) -> dict[str, str | None]:
    """Return {pkgname: version_string-or-None} via a single ``pacman -Q``.

    A missing package maps to None. Used by :func:`_verify_llvm_install`
    to assert that every installed LLVM component reports the same
    `pkgver-pkgrel`.
    """
    result: dict[str, str | None] = {n: None for n in pkgnames}
    try:
        proc = subprocess.run(
            ["pacman", "-Q", *pkgnames],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return result
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in result:
            result[parts[0]] = parts[1].strip()
    return result


def _check_llvm_link_resolution() -> list[str]:
    """Verify installed LLVM binaries resolve libLLVM only under /usr/lib.

    A stage prefix (``/var/tmp/sysforge-llvm-stage*``) appearing in the
    ``ldd`` output of an installed binary means Pass 3 packaged a bad
    RPATH or the install is incomplete — silently leaving the system on a
    libLLVM that's about to be wiped from /var/tmp.  A resolution under
    some other prefix (``$HOME/.local/lib`` etc.) is also flagged because
    it means a sibling library is shadowing the package-managed one.

    Returns issue strings; empty list means clean.
    """
    issues: list[str] = []
    for bin_path, label in (
        ("/usr/bin/clang", "clang"),
        ("/usr/bin/lld", "lld"),
    ):
        if not Path(bin_path).exists():
            continue
        try:
            proc = subprocess.run(
                ["ldd", bin_path], capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            issues.append("ldd: binary missing from PATH")
            return issues
        if proc.returncode != 0:
            issues.append(f"ldd {bin_path}: exit {proc.returncode}")
            continue
        libllvm_paths: list[str] = []
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if "libLLVM" not in stripped:
                continue
            parts = stripped.split("=>")
            if len(parts) < 2:
                continue
            tail = parts[1].strip().split()
            if not tail or tail[0] in ("not", "(0x0)"):
                continue
            libllvm_paths.append(tail[0])
        for p in libllvm_paths:
            if "/var/tmp/sysforge-llvm-stage" in p:
                issues.append(
                    f"{label} resolves libLLVM from a staging prefix: {p} "
                    "— Pass 3 packaged a bad RPATH or the install is incomplete"
                )
            elif not p.startswith("/usr/lib"):
                issues.append(
                    f"{label} resolves libLLVM outside /usr/lib: {p} "
                    "— a sibling libLLVM is shadowing the package-managed one"
                )
    return issues


def _dump_stage_dynsym_evidence(
    staging: Path, dest_dir: Path | str
) -> Path | None:
    """Dump stage2 ``libLLVM.so.*`` dynamic symbols for post-mortem inspection.

    Called from the post-install verify failure path. Writes
    ``nm -D --defined-only`` of the suspected leak source to
    ``<state_dir>/llvm_abi_hazard.log`` with a filtered "suspicious symbols"
    header listing every line matching ``_ZNSt`` — direct evidence of which
    C++ stdlib exports leaked into stage2's libLLVM under the LLVM version
    namespace. Returns the log path on success; ``None`` if staging is gone,
    the suspect ``.so`` is missing, or ``nm`` failed.
    """
    if not staging.exists():
        return None
    candidates = sorted((staging / "usr/lib").glob("libLLVM.so.*"))
    so = next((p for p in candidates if p.is_file()), None)
    if so is None:
        return None
    try:
        proc = subprocess.run(
            ["nm", "-D", "--defined-only", str(so)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    suspicious = [ln for ln in proc.stdout.splitlines() if "_ZNSt" in ln]
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "llvm_abi_hazard.log"
    body = (
        f"# Diagnostic dump from stage2 libLLVM: {so}\n"
        f"# Suspicious symbols (C++ stdlib exports, {len(suspicious)} found):\n"
        + "\n".join(suspicious)
        + ("\n" if suspicious else "")
        + "# Full nm -D --defined-only output:\n"
        + proc.stdout
    )
    out.write_text(body)
    return out


def _verify_llvm_install(
    expected_targets: list[str] | None = None,
) -> list[str]:
    """Run post-install LLVM consistency checks.

    Returns a list of human-readable issue strings; empty list means
    everything is consistent. Caller decides what to do (typically: prompt
    the user with a recovery `pacman -S ...` command). Checks:

    1. ``pacman -Q`` versions across :data:`_LLVM_VERSION_MATCH_SET` all
       match. A mismatch is the canonical interrupted-install symptom.
    2. ``clang --version`` and ``lld --version`` run without crashing.
    3. ``llvm-config --targets-built`` is a superset of
       ``expected_targets`` (skipped when ``expected_targets`` is None or
       empty — i.e. no LLVM_TARGETS filtering was configured).
    4. ``ldd`` of installed clang/lld resolves libLLVM under /usr/lib —
       never under /var/tmp/sysforge-llvm-stage*.  Catches a Pass-3 RPATH
       mistake before /var/tmp gets cleaned and breaks the live toolchain.
    """
    issues: list[str] = []

    # Skew arm — drawn from the pure fact in toolchain_safety so this entry
    # point and the preflight probe share one definition (the data source,
    # _query_pacman_versions, stays here so the subprocess call remains the
    # patch point). detect_suite_skew is brick-class on disagreement.
    versions = _query_pacman_versions(_LLVM_VERSION_MATCH_SET)
    skew = toolchain_safety.detect_suite_skew(versions)
    if skew is not None:
        issues.append(skew.message)

    issues.extend(_check_llvm_link_resolution())

    for cmd, label in (
        (["clang", "--version"], "clang --version"),
        (["lld", "--version"], "lld --version"),
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            issues.append(f"{label}: binary missing from PATH")
            continue
        if proc.returncode != 0:
            issues.append(
                f"{label}: exit {proc.returncode} — {proc.stderr.strip()[:200]}"
            )

    if expected_targets:
        try:
            proc = subprocess.run(
                ["llvm-config", "--targets-built"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            issues.append("llvm-config: binary missing from PATH")
        else:
            if proc.returncode != 0:
                issues.append(
                    f"llvm-config --targets-built: exit {proc.returncode}"
                )
            else:
                built = set(proc.stdout.split())
                missing = [t for t in expected_targets if t not in built]
                if missing:
                    issues.append(
                        "llvm-config --targets-built missing expected backends "
                        f"({', '.join(missing)}); built={sorted(built)}"
                    )

    return issues


def _llvm_recovery_command() -> str:
    """The canonical pacman command that restores a consistent LLVM set."""
    return "sudo pacman -S " + " ".join(_LLVM_VERSION_MATCH_SET)


# ---------------------------------------------------------------------------
# Concurrent-build lock
# ---------------------------------------------------------------------------


def _pgo_lock(lock_path: Path):
    """Advisory flock guard for the duration of a PGO toolchain build.

    Two concurrent ``sysforge run toolchain`` invocations would clobber
    ``/var/tmp/sysforge-llvm-stage1``, ``/var/tmp/sysforge-llvm-stage2`` and
    the shared ``pgo_store`` profraw directory.  The sentinel scope guards
    the state-dir but not these /var/tmp + ~/pgo paths, so we add an
    explicit advisory lock here.  Delegates to the shared ``build_lock``
    primitive (the kernel stage uses the same one) — don't roll a second
    flock path.
    """
    return build_lock(lock_path, label="PGO")


# ---------------------------------------------------------------------------
# Build paths
# ---------------------------------------------------------------------------


def _build_llvm_single(
    pkgbuild_map: dict[str, Path],
    non_pgo_map: dict[str, Path],
    lib32_map: dict[str, Path],
    options,
) -> tuple[dict[str, Path], str, str, str, str]:
    """Single-pass LLVM build (pgo = false). Builds WITHOUT installing.

    Returns ``(built_map, cc, cxx, ld, variant)``. ``built_map`` is the
    name→PKGBUILD map the caller uses to collect built ``.pkg.tar*`` for the
    Gate-2 ABI audit and the atomic, batched install inside the sentinel. The
    build itself mutates nothing — the live ``/usr`` is only touched by the
    caller's ``batch_install_pkgs`` step, so a build failure here leaves no
    sentinel and no partial install (the old per-package ``install=True`` loop
    could leave a half-installed suite if one package failed mid-batch).
    """
    all_pkgs = {**pkgbuild_map, **non_pgo_map, **lib32_map}
    _build_pass(
        "LLVM build (single pass, no PGO)",
        all_pkgs,
        options,
        install=False,
        toolchain_variant="stock_llvm",
        owner_stage="toolchain",
    )
    return all_pkgs, "/usr/bin/clang", "/usr/bin/clang++", "lld", "stock_llvm"


def _pgo_lock_path(staging1: Path) -> Path:
    """Lock-file path guarding the PGO staging dirs + pgo_store.

    Lives in the parent of staging1 (typically ``/var/tmp``) so neither the
    Pass-1 purge nor the post-build cleanup can delete it. The stage acquires
    this (via :func:`_pgo_lock`) around the whole build → audit → install
    window, mirroring how the kernel stage wraps its build with
    ``kernel-build.lock``.
    """
    return staging1.parent / "sysforge-pgo.lock"


def _build_llvm_pgo_inner(
    pgo_map: dict[str, Path],
    non_pgo_map: dict[str, Path],
    lib32_map: dict[str, Path],
    staging1: Path,
    staging: Path,
    pgo_store: Path,
    options,
) -> tuple[dict[str, Path], str, str, str, str]:
    """
    4-pass LLVM PGO build. Builds WITHOUT installing.

    Returns ``(built_map, cc, cxx, ld, variant)`` where ``built_map`` is the
    Pass-3 package map (pgo ∪ non_pgo ∪ lib32) the caller installs *after* the
    Gate-2 ABI audit, inside the sentinel. Passes 1a/1b/2 and the Pass-3 build
    all run with ``install=False`` so the live ``/usr`` is untouched until the
    caller's install step — a build-pass failure therefore leaves no sentinel.

    Pass 1a: system compiler + -fprofile-generate; builds ONLY the pgo list
             (llvm, llvm-libs).  makepkg runs WITHOUT --install; outputs are
             extracted to pgo_staging1 (stage1) — the live /usr is never
             touched.  Both packages stage, including the cmake-config /
             static-lib `llvm` package, so Pass 1b's find_package(LLVM)
             resolves stage1's headers + configs.
    Pass 1b: non-instrumented build of non_pgo packages (clang, lld,
             compiler-rt, polly, openmp, spirv-llvm-translator) against
             stage1.  CMAKE_PREFIX_PATH=<staging1>/usr; LD_LIBRARY_PATH is
             deliberately NOT set (forcing system clang to load stage1's
             libLLVM would recreate the version-skew failure mode this
             refactor exists to prevent).  Outputs extracted into the same
             pgo_staging1, making stage1 self-sufficient — both a working
             clang and a working libLLVM, both ABI-coherent.
    Pass 2:  training run.  CC=<staging1>/usr/bin/clang (built in Pass 1b);
             the running clang and the libLLVM it loads are guaranteed
             coherent because they were built together.  Builds pgo + non_pgo;
             generates profraw as a side effect.  CCACHE_DISABLE=1 and
             SCCACHE_DISABLE=1 injected so cache tools cannot bypass the
             instrumented compiler and silently produce no profraw.
             Background daemon merges profraw periodically; final sweep after
             build.  Profdata size checked; warns if suspiciously small.
             Pgo-package binaries extracted to pgo_staging (stage2); no
             system install.
    Pass 3:  CC=staged clang from stage2 if available, else system clang.
             CFLAGS/LDFLAGS += -fprofile-use; LTO disabled via LTOFLAGS=""
             (ThinLTO + IR PGO causes non-PIC vtable relocations in lld's
             ThinLTO codegen for libLLVM.so); install pgo + non_pgo + lib32.
             Staging prefixes removed on success.  Profdata preserved with a
             version sidecar (clang.profdata.version) for reuse by sysforge update.

    Returns (cc, cxx, ld).
    """
    staged_cc = str(staging / "usr/bin/clang")
    staged_cxx = str(staging / "usr/bin/clang++")

    n_pgo = len(set(pgo_map.values()))
    n_total = len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))

    _validate_pgo_environment(options.dry_run)

    # Check for existing compatible profdata before purging.
    # If profdata from a previous run matches the target LLVM major version,
    # skip passes 1a-2 and go straight to Pass 3 (the optimized build).
    # --rebuild-profdata forces a full 4-pass build regardless.
    skip_profgen = False
    profdata_path = pgo_store / "clang.profdata"
    if not options.rebuild_profdata and not options.dry_run:
        pgo_state, pgo_info = _check_existing_profdata(pgo_store, pgo_map)
        if pgo_state == "ready":
            # Prompt #1 — profdata reuse decision. Default yes (the common,
            # cheap path); declining drops into prompts #2 and #3 below.
            try:
                _pgo_confirm(
                    f"Reuse existing profdata at {pgo_info}? "
                    "Selecting no triggers a full 4-pass rebuild. [Y/n]:",
                    default="y",
                    eof_default="n",
                    options=options,
                    abort_msg="user declined profdata reuse",
                )
                skip_profgen = True
                profdata_path = Path(pgo_info)
                _log.ui(
                    f"[PGO] Reusing existing profdata: {profdata_path}  "
                    f"(use --rebuild-profdata to force a full 4-pass build)",
                )
            except _PGOAborted:
                _log.ui(
                    "[PGO] Profdata reuse declined — falling through to "
                    "full 4-pass rebuild",
                )
        elif pgo_state == "mismatch":
            _log.info(f"[PGO] Existing profdata incompatible: {pgo_info}")
        else:
            _log.info(f"[PGO] No existing profdata: {pgo_info}")

    if skip_profgen:
        _log.ui(
            f"[PGO] Skipping passes 1a-2 (instrument/bootstrap/train), building with existing profdata  "
            f"({n_total} package(s) across {len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))} PKGBUILD(s))  "
            f"pgo_store={pgo_store}",
        )

    if not skip_profgen and not options.dry_run:
        import shutil as _shutil

        # Prompt #2 — purge staging/pgo_store. rmtree is silently destructive
        # of partial Pass-1/Pass-3 staging from a prior failed run, so gate it.
        if staging1.exists() or staging.exists() or pgo_store.exists():
            _pgo_confirm(
                f"Purge staging dirs and pgo_store to start fresh 4-pass build?\n"
                f"  stage1:    {staging1}\n"
                f"  stage2:    {staging}\n"
                f"  pgo_store: {pgo_store}\n"
                "[y/N]:",
                default="n",
                eof_default="n",
                options=options,
                abort_msg="user declined purge of staging/pgo_store",
            )

        # Prompt #3 — confirm the long 4-pass build before launch. Replaces
        # the old "Starting LLVM PGO build" log with an explicit gate.
        _pgo_confirm(
            "About to start 4-pass LLVM PGO build "
            f"({n_pgo} pgo PKGBUILD(s), {n_total} total) — "
            "~2-3 hours; pass 2 is a long instrumented training run. "
            f"pgo_store={pgo_store}. Proceed? [y/N]:",
            default="n",
            eof_default="n",
            options=options,
            abort_msg="user declined 4-pass PGO start",
        )
        _log.ui(
            f"[PGO] Starting 4-pass LLVM PGO build  "
            f"({n_pgo} pgo PKGBUILD(s), {n_total} total across all passes)  "
            f"pgo_store={pgo_store}",
        )

        if staging1.exists():
            _log.info(f"[PGO] Purging stale stage1: {staging1}")
            _shutil.rmtree(staging1)
        if staging.exists():
            _log.info(f"[PGO] Purging stale staging: {staging}")
            _shutil.rmtree(staging)
        if pgo_store.exists():
            _log.info(f"[PGO] Purging stale pgo_store: {pgo_store}")
            _shutil.rmtree(pgo_store)
        pgo_store.mkdir(parents=True, exist_ok=True)

    # Sudo keepalive for the build sequence. _pgo_install() calls sudo
    # directly from sysforge, so the keepalive's `sudo -v` refreshes the correct
    # timestamp entry (same parent PID) for all passes.
    if not options.dry_run:
        subprocess.run(["sudo", "-v"])
    sudo_stop = threading.Event()
    sudo_keepalive = threading.Thread(
        target=_sudo_keepalive_daemon,
        args=(sudo_stop,),
        daemon=True,
        name="sysforge-sudo-keepalive",
    )
    if not options.dry_run:
        sudo_keepalive.start()

    try:
        residual_linker_flags: str | None = None

        if not skip_profgen:
            # Pass 1a — build pgo packages with the system compiler +
            # -fprofile-generate. -fprofile-generate produces LLVM-format
            # .profraw (consumed by llvm-profdata); on a running Arch system
            # with LLVM installed the system compiler is always clang. makepkg
            # runs WITHOUT --install; outputs are extracted into stage1 by
            # _pgo_pass1_stage so the live root is never touched.
            _build_pass(
                "PGO 1/4 · instrument llvm / llvm-libs",
                pgo_map,
                options,
                cc=None,
                cxx=None,
                install=False,
                pgo_build=True,
                compiler_flags_extra=f"-fprofile-generate={pgo_store}/",
            )
            _pgo_pass1_stage(pgo_map, staging1, options.dry_run)
            _log.ui("[PGO] 1/4 complete (staged to "
                    f"{staging1} — system /usr untouched)")

            # Purge any profraw accumulated during Pass 1a + 1b. CMake feature-
            # test programs compiled with -fprofile-generate run during
            # configuration and deposit spurious profraw files (and emit
            # "Running out of static counters" warnings). Those represent tiny
            # probe programs, not clang doing real work — they would contaminate
            # the training profile if kept. Pass 2 generates the real data.
            # We purge here (after Pass 1a) and again after Pass 1b — the latter
            # only matters if Pass 1b's profile runtime injection somehow caused
            # a feature-test to write profraw, but defensive purging is cheap.
            if not options.dry_run:
                spurious = list(pgo_store.glob("**/*.profraw"))
                for f in spurious:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                if spurious:
                    _log.info(
                        f"[PGO] Purged {len(spurious)} spurious profraw file(s) "
                        f"from Pass 1a CMake probes",
                    )

            # Pass 1b — build non_pgo packages (clang, lld, compiler-rt, …)
            # NON-instrumented but against stage1's headers and libs. The
            # output binaries are ABI-coherent with stage1's libLLVM, so
            # stage1/usr/bin/clang can drive Pass 2 without version drift
            # against the live /usr clang.
            #
            # CC=/usr/bin/clang here is the bootstrap host compiler — it
            # compiles C++ source into objects, not anything that loads stage1's
            # libLLVM. We deliberately do NOT inject LD_LIBRARY_PATH for
            # Pass 1b; that would force the system clang to load stage1's
            # (possibly newer) libLLVM, recreating the version-skew failure
            # mode this whole refactor exists to prevent.
            #
            # CMAKE_PREFIX_PATH points cmake's find_package(LLVM) at stage1
            # so the new clang links against stage1's libLLVM.so. The
            # instrumented .a archives staged alongside surface __llvm_profile_*
            # link errors; _profile_runtime_ldflag() injects the clang profile
            # runtime to satisfy them.
            pass1b_env = {
                "CMAKE_PREFIX_PATH": f"{staging1}/usr",
            }
            residual_linker_flags = _profile_runtime_ldflag()
            if residual_linker_flags is not None:
                _log.info(
                    "[PGO] Pass 1b: injecting clang profile runtime into LDFLAGS "
                    "to resolve __llvm_profile_* in stage1's instrumented .a archives "
                    f"({residual_linker_flags})",
                )
            _build_pass(
                "PGO 2/4 · bootstrap clang/lld/... against stage1",
                non_pgo_map,
                options,
                cc="/usr/bin/clang",
                cxx="/usr/bin/clang++",
                install=False,
                linker_flags_extra=residual_linker_flags,
                pgo_build=True,
                pgo_env=pass1b_env,
                staged_deps=True,
            )
            _extract_pass2_to_staging(non_pgo_map, staging1, options.dry_run)
            _log.ui(f"[PGO] 2/4 complete (stage1 self-sufficient at {staging1})")

            # Pass 2 — training run. CC is stage1's freshly built clang (built
            # in Pass 1b against stage1's instrumented libLLVM), so the running
            # compiler and the loaded libLLVM are guaranteed ABI-coherent —
            # no version drift, no missing weak/inlined symbols. Profraw is
            # generated by the instrumented libLLVM as a side effect of
            # running stage1/usr/bin/clang. Background daemon merges periodically.
            #
            # LLVM_PROFILE_FILE uses %m_%p so each parallel clang process writes
            # to its own file (module-hash + PID) instead of all contending on
            # default_%m.profraw. Without this, N parallel make -j clang
            # invocations corrupt each other's profraw via concurrent writes,
            # causing SIGBUS crashes in llvm-profdata.
            pass2_env = {
                "LLVM_PROFILE_FILE": f"{pgo_store}/default_%m_%p.profraw",
                # Prevent ccache/sccache from serving cached objects during the
                # training run.  If either tool intercepts a compilation it skips
                # running the instrumented clang entirely, producing no profraw.
                # _DISABLE=1 makes each tool act as a transparent pass-through so
                # the instrumented binary always executes and writes profraw data.
                "CCACHE_DISABLE": "1",
                "SCCACHE_DISABLE": "1",
                # Redirect dyld/cmake/clang at stage1.  stage1/usr/bin/clang's
                # NEEDED entries reference libLLVM by SONAME; dyld picks up
                # stage1/usr/lib first, then falls back to /usr/lib for system
                # libs not staged here (libstdc++, libc, …).
                **_stage_env(staging1),
            }
            _log.info(
                f"[PGO] Pass 2: LLVM_PROFILE_FILE={pass2_env['LLVM_PROFILE_FILE']}",
            )

            stop_event = threading.Event()
            monitor = threading.Thread(
                target=_profraw_merge_daemon,
                args=(pgo_store, stop_event),
                daemon=True,
                name="sysforge-profraw-monitor",
            )
            if not options.dry_run:
                monitor.start()
            # Include non_pgo packages (polly, compiler-rt, openmp,
            # spirv-llvm-translator) in the training run.  They exercise different
            # clang code paths: OpenMP structured blocks, compiler-rt intrinsics,
            # polyhedral analysis (Polly) — all absent from LLVM self-compilation
            # alone.  lib32 is excluded; cross-compilation paths aren't worth the
            # extra build time here.
            pass2_map = {**pgo_map, **non_pgo_map}
            stage1_clang = staging1 / "usr/bin/clang"
            stage1_clangxx = staging1 / "usr/bin/clang++"
            # Pass 1b produces stage1's clang only when non_pgo_map is non-
            # empty (a deliberately empty list — used by tests / minimal
            # configs — skips Pass 1b entirely).  Fall back to /usr/bin/clang
            # in that case; the live system stays the bootstrap for Pass 2.
            if stage1_clang.exists() or options.dry_run:
                pass2_cc, pass2_cxx = str(stage1_clang), str(stage1_clangxx)
            else:
                _log.info(
                    f"[PGO] Pass 2: stage1 clang absent at {stage1_clang} "
                    "(non_pgo_map empty — Pass 1b skipped); falling back to "
                    "/usr/bin/clang. Version skew between system clang and "
                    "in-tree LLVM source may surface here.",
                )
                pass2_cc, pass2_cxx = "/usr/bin/clang", "/usr/bin/clang++"
            try:
                _build_pass(
                    "PGO 3/4 · train (profraw generation, no system install)",
                    pass2_map,
                    options,
                    cc=pass2_cc,
                    cxx=pass2_cxx,
                    install=False,
                    linker_flags_extra=residual_linker_flags,
                    pgo_build=True,
                    pgo_env=pass2_env,
                    staged_deps=True,
                )
            finally:
                stop_event.set()
                if not options.dry_run:
                    monitor.join()
            _log.ui("[PGO] 3/4 complete")

            # Final sweep: merge any profraw not yet handled by the daemon
            profdata_path = _merge_profraw(pgo_store, options.dry_run)
            if not options.dry_run:
                profdata_size = profdata_path.stat().st_size
                _log.info(
                    f"[PGO] Merged profdata size: {profdata_size // (1024 * 1024)} MiB",
                )
                if profdata_size < _PGO_PROFDATA_MIN_BYTES:
                    _log.warn(
                        f"[PGO] Profdata is unexpectedly small "
                        f"({profdata_size // (1024 * 1024)} MiB < "
                        f"{_PGO_PROFDATA_MIN_BYTES // (1024 * 1024)} MiB). "
                        "Pass 2 may not have exercised enough code paths — "
                        "check that CCACHE_DISABLE/SCCACHE_DISABLE took effect "
                        "and compilation actually ran.",
                    )
                    # Prompt #4 — abort before Pass 3 unless the user explicitly
                    # accepts the suspicious profdata. Wrong profdata silently
                    # mis-optimises the resulting compiler, so default to no.
                    _pgo_confirm(
                        f"Pass 2 profdata is suspiciously small "
                        f"({profdata_size // (1024 * 1024)} MiB) — "
                        "instrumentation may have been bypassed. "
                        "Continue to Pass 3 with this profdata? [y/N]:",
                        default="n",
                        eof_default="n",
                        options=options,
                        abort_msg="user declined Pass 3 due to suspicious profdata",
                    )
            _log.ui(f"[PGO] Profile data ready: {profdata_path}")
            # Write the sidecar now (right after Pass 2 has produced the
            # profdata, before Pass 3 starts) so an aborted Pass 3 still
            # leaves recoverable profdata that the next run can reuse via
            # _check_existing_profdata.  The sidecar's only invariant is
            # "this profdata is for LLVM major N", determined entirely by
            # what Pass 2 instrumented — Pass 3 success has no bearing on it.
            if not options.dry_run:
                _write_profdata_version(pgo_store, pgo_map)
            _extract_pass2_to_staging(pgo_map, staging, options.dry_run)

        # Pass 3 (or sole pass when reusing profdata) — PGO-optimized build.
        # Use staged clang from Pass 2 if available; otherwise fall back to
        # system clang (which, after a prior successful run, is already PGO-optimized).
        using_staged_cc = (
            not skip_profgen
            and not options.dry_run
            and Path(staged_cc).exists()
        )
        if not skip_profgen and not options.dry_run and not using_staged_cc:
            _log.info(
                f"[PGO] staged clang not found at {staged_cc} "
                "(clang is non-pgo) — using system clang for Pass 3",
            )
            pass3_cc, pass3_cxx = "/usr/bin/clang", "/usr/bin/clang++"
        elif skip_profgen:
            # No staging when reusing profdata — system clang is the compiler
            pass3_cc, pass3_cxx = "/usr/bin/clang", "/usr/bin/clang++"
        else:
            pass3_cc, pass3_cxx = staged_cc, staged_cxx

        all_pass3 = {**pgo_map, **non_pgo_map, **lib32_map}
        pass3_label = (
            "PGO optimize · all packages (reusing profdata)"
            if skip_profgen
            else "PGO 4/4 · optimize all packages"
        )
        # Pass 3 env: clear LLVM_PROFILE_FILE so any inherited Pass-2 training
        # path doesn't get reused.  Only when CC is the staged clang from
        # stage2 do we also redirect cmake/dyld at stage2 — that clang's
        # NEEDED libLLVM is stage2's libLLVM, so the redirect is ABI-coherent.
        # System /usr/bin/clang on the system-clang fallback is linked against
        # the live /usr libLLVM (full target list) and must NOT be steered at
        # stage2's stripped (LLVM_TARGETS_TO_BUILD-restricted) libLLVM via
        # LD_LIBRARY_PATH — that recreates the version-skew failure mode the
        # Pass 1b comments warn about (symbol lookup errors for missing target
        # init functions like LLVMInitializeBPFTarget).
        pass3_env: dict[str, str] = {"LLVM_PROFILE_FILE": ""}
        if using_staged_cc:
            pass3_env.update(_stage_env(staging))
        # Phase 2: Pass 3 builds against stage2's non-instrumented LLVM, so
        # find_package(LLVM) sees no __llvm_profile_* references — no
        # profile-runtime injection here.  Leaving linker_flags_extra unset
        # also prevents the Pass 1b/Pass 2 residual flag from leaking into
        # the final optimized binaries.
        _build_pass(
            pass3_label,
            all_pass3,
            options,
            cc=pass3_cc,
            cxx=pass3_cxx,
            install=False,
            compiler_flags_extra=f"-fprofile-use={profdata_path}",
            pgo_build=True,
            pgo_env=pass3_env,
            staged_deps=True,
            toolchain_variant="pgo_llvm",
            owner_stage="toolchain",
        )
        # Pass 3 is built but NOT installed here — the caller runs the Gate-2
        # ABI audit on the built packages, snapshots the current suite, then
        # installs inside the sentinel via _pgo_install. Keeping the install
        # out of the build function means a Gate-2 abort leaves nothing
        # installed and no sentinel.
        if skip_profgen:
            _log.ui("[PGO] Optimized build complete (profdata reused) — pending audit + install")
        else:
            _log.ui("[PGO] 4/4 build complete — pending audit + install")

    finally:
        sudo_stop.set()
        if not options.dry_run:
            sudo_keepalive.join()

    # Sidecar is written after Pass 2 (above), not here — see comment there.
    # Staging is intentionally NOT removed here: the caller's Gate-3
    # _verify_llvm_install runs after install, and on a verify failure the
    # stage2 prefix is needed by _dump_stage_dynsym_evidence to capture which
    # exports leaked into stage2's libLLVM. Staging is removed by the caller
    # after Gate 3 passes (or after a successful rollback).
    return all_pass3, "/usr/bin/clang", "/usr/bin/clang++", "lld", "pgo_llvm"


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


def _gate1_preflight(
    lib32_pkgs, staging1, staging, pgo_store,
    pkgbuild_map, options, tcfg, *, snapshot,
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
    require_multilib = bool(tcfg.get("require_multilib", True))
    min_free_gb = float(tcfg.get("min_build_free_gb", 40))
    lib32_in_scope = bool(lib32_pkgs)

    def _abort_or_warn(finding) -> None:
        if dry_run:
            _log.warn(f"[dry-run] Gate 1 [{finding.severity.upper()}] "
                      f"{finding.check_id}: {finding.message}")
            if finding.remediation:
                _log.warn(f"  → {finding.remediation}")
        else:
            raise RuntimeError(
                f"[TOOLCHAIN] Gate 1 ({finding.check_id}): {finding.message} "
                f"{finding.remediation}".rstrip()
            )

    # Brick: PKGBUILD pkgver skew across lockstep members (spirv + lib32 excluded).
    if not allow_skew:
        pkgvers = _parse_pkgbuild_pkgvers(pkgbuild_map)
        skew = toolchain_safety.check_pkgver_lockstep(pkgvers)
        if skew is not None:
            _abort_or_warn(skew)
    else:
        _log.warn("--allow-version-skew: skipping the PKGBUILD pkgver lockstep check")

    # Brick: clang must compile + lld must be present (both paths now).
    for finding in toolchain_safety.smoke_test_compilers():
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


def _parse_pkgbuild_pkgvers(pkgbuild_map: dict[str, Path]) -> dict[str, str]:
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


def _gate2_audit(built_map: dict[str, Path], *, dry_run: bool) -> None:
    """Scan the built ``.pkg.tar*`` for the std::-bound-to-LLVM ABI hazard.

    Runs *outside* the install sentinel, between build and install, for BOTH
    the PGO and non-PGO paths (previously only PGO's ``_pgo_install`` scanned).
    A brick (any ``_ZNSt*@LLVM_*`` symbol) aborts before any ``pacman -U`` — so
    the live ``/usr`` is untouched and no sentinel is left behind. Skipped in
    dry-run (nothing was built).
    """
    if dry_run:
        _log.ui("[dry-run] would audit built packages for ABI hazards (Gate 2)")
        return
    pkgs = _collect_pgo_packages(built_map)
    if not pkgs:
        # Nothing to audit (e.g. AlreadyBuilt with PKGDEST cleared) — the
        # install step will surface a missing-package error of its own.
        return
    _log.ui(f"Gate 2: auditing {len(pkgs)} built package(s) for ABI hazards")
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


def _snapshot_suite(built_map: dict[str, Path]) -> dict[str, "Path | None"]:
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
    return cached_pkg_files_for(sorted(names))


def _rollback_to_snapshot(snapshot: dict[str, "Path | None"]) -> bool:
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
    return batch_install_pkgs(files)


def _snapshot_recovery_cmd(snapshot: dict[str, "Path | None"]) -> str:
    """Recovery command stored in the sentinel when rollback can't run cleanly.

    Prefers an offline ``pacman -U <cached files>`` when every member is cached;
    otherwise falls back to the online ``pacman -S <suite>``. Stored in the
    sentinel so the next-run recovery prompt has a copy-pasteable restore.
    """
    files = [p for p in snapshot.values() if p is not None]
    if files and all(p is not None for p in snapshot.values()):
        return "sudo pacman -U --noconfirm " + " ".join(str(p) for p in files)
    return _llvm_recovery_command()


def _log_toolchain_resolution_summary(
    *, compiler, pgo, variant, pgo_pkgs, non_pgo_pkgs, lib32_pkgs,
    staging1, staging, pgo_store, tcfg, options, snapshot,
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
        f"min_build_free={float(tcfg.get('min_build_free_gb', 40)):g}GiB "
        f"require_multilib={'on' if tcfg.get('require_multilib', True) else 'off'}"
        + (f" overrides={','.join(overrides)}" if overrides else "")
    )
    _log.ui("Toolchain build plan:")
    _log.ui(f"  compiler:   {compiler}  pgo={pgo}  variant={variant}")
    _log.ui(f"  packages:   {n_total} total "
            f"({len(pgo_pkgs)} pgo / {len(non_pgo_pkgs)} non-pgo / {len(lib32_pkgs)} lib32)")
    if pgo:
        _log.ui(f"  staging1:   {staging1}")
        _log.ui(f"  staging2:   {staging}")
        _log.ui(f"  pgo_store:  {pgo_store}")
    _log.ui(f"  gates:      {gates}")
    _log.ui(f"  snapshot:   {cached}/{len(snapshot)} suite package(s) cached for offline rollback")


# ---------------------------------------------------------------------------
# Compiler path lookup (no build)
# ---------------------------------------------------------------------------


def _compiler_paths(compiler: str) -> tuple[str, str, str | None]:
    """Return (cc, cxx, ld) for a named compiler without building anything."""
    if compiler == "gcc":
        return "/usr/bin/gcc", "/usr/bin/g++", None
    return "/usr/bin/clang", "/usr/bin/clang++", "lld"


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class ToolchainStage(Stage):
    name = "toolchain"
    description = "LLVM/GCC toolchain build"
    depends_on = ["reconfigure"]

    def run(self, config, state, options):
        tcfg = _load_toolchain_config()
        if tcfg is None or not tcfg.get("enabled", False):
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

        compiler = tcfg.get("compiler", "gcc")
        pgo = tcfg.get("pgo", True) if compiler == "llvm" else False
        staging1 = Path(tcfg.get("pgo_staging1", _DEFAULT_STAGING_1))
        staging = Path(tcfg.get("pgo_staging", _DEFAULT_STAGING))
        pgo_store = Path(tcfg.get("pgo_store", _DEFAULT_PGO_STORE))

        # GCC path: never build from source. Register system gcc paths so
        # downstream stages (packages, kernel) pick them up; stock gcc-libs
        # from pacman/base-devel provides the runtime. This is intentionally
        # the only behaviour for compiler="gcc" — sysforge does not own a
        # GCC build path (no meaningful performance gain, error-prone, and
        # base-devel already covers it).
        if compiler == "gcc":
            cc, cxx, ld = _compiler_paths("gcc")
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
            return

        # skip_build (LLVM only): register clang paths without building.
        # Variant reflects on-disk clang provenance (pgo_llvm if a profdata
        # + version sidecar pair is present, else stock_llvm) so downstream
        # conditionals see the actual installed compiler, not just the
        # stage's current action.
        if tcfg.get("skip_build", False):
            cc, cxx, ld = _compiler_paths(compiler)
            variant = _resolve_skip_build_variant(pgo_store)
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
            return

        pgo_pkgs, non_pgo_pkgs, lib32_pkgs = _package_lists(tcfg)

        all_names = pgo_pkgs + non_pgo_pkgs + lib32_pkgs
        total = len(all_names)
        if pgo:
            parts = [f"{len(pgo_pkgs)} pgo", f"{len(non_pgo_pkgs)} non-pgo"]
        else:
            parts = [f"{len(pgo_pkgs) + len(non_pgo_pkgs)} packages"]
        if lib32_pkgs:
            parts.append(f"{len(lib32_pkgs)} lib32")
        pkg_summary = f"{total} total  ({' / '.join(parts)})"
        _log.ui(
            f"Compiler: {compiler}  |  PGO: {pgo}  |  Packages: {pkg_summary}",
        )

        # Resolve PKGBUILDs for all packages. Sync through SourceSyncScheduler
        # unless --no-update was passed; --cleansrc[/-force] forces the sync
        # path even with --no-update so an explicit purge isn't silently
        # skipped.
        pkgbuild_map = _resolve_all_pkgbuilds(
            all_names, config,
            update=not options.no_update,
            cleansrc=getattr(options, "cleansrc", False),
            cleansrc_force=getattr(options, "cleansrc_force", False),
        )

        # LLVM safety pre-flight: refuse-by-default on dirty/diverged trees.
        # Strict mode is rule-driven from sysforge.toml [safety]; the CLI
        # --allow-dirty-llvm flag bypasses dirty/diverged blockers (a stale
        # PGO profdata mismatch is never suppressible).
        _run_llvm_preflight(all_names, config, options)

        pgo_map = {n: pkgbuild_map[n] for n in pgo_pkgs}
        non_pgo_map = {n: pkgbuild_map[n] for n in non_pgo_pkgs}
        lib32_map = {n: pkgbuild_map[n] for n in lib32_pkgs}

        role_map = (
            (
                {n: "pgo" for n in pgo_pkgs}
                | {n: "non-pgo" for n in non_pgo_pkgs}
                | {n: "lib32" for n in lib32_pkgs}
            )
            if pgo
            else {n: "lib32" for n in lib32_pkgs}
        )
        _show_resolution_table(pkgbuild_map, role_map=role_map or None)

        # Capture the prior-good install set BEFORE any mutation so it can be
        # restored offline if Gate 3 (post-install verify) fails. Cheap pacman
        # cache lookup; safe in dry-run (read-only).
        snapshot = _snapshot_suite({**pgo_map, **non_pgo_map, **lib32_map})

        # B1: consolidated resolution summary — one labelled block plus the
        # readable core of a --dry-run preview (kernel parity).
        variant = "pgo_llvm" if pgo else "stock_llvm"
        _log_toolchain_resolution_summary(
            compiler=compiler, pgo=pgo, variant=variant,
            pgo_pkgs=pgo_pkgs, non_pgo_pkgs=non_pgo_pkgs, lib32_pkgs=lib32_pkgs,
            staging1=staging1, staging=staging, pgo_store=pgo_store,
            tcfg=tcfg, options=options, snapshot=snapshot,
        )

        # Gate 1 — cheap pre-build preflight. Hard-fails (overridable) on
        # definite-failure conditions BEFORE any build time is spent; dry-run
        # downgrades bricks to warnings. Runs for both PGO and non-PGO.
        _gate1_preflight(
            lib32_pkgs, staging1, staging, pgo_store,
            pkgbuild_map, options, tcfg, snapshot=snapshot,
        )

        # Prompt for confirmation (interactive only)
        try:
            import sys as _sys

            if _sys.stdin.isatty() and not options.dry_run:
                _confirm_or_abort(options.state_dir)
        except RuntimeError:
            raise

        # Advisory lock around the whole build → audit → snapshot → install
        # window, mirroring the kernel stage's kernel-build.lock. Guards the
        # PGO /var/tmp staging dirs + pgo_store (and the non-PGO build area) so
        # two concurrent `sysforge run toolchain` runs can't clobber each
        # other. Skipped in dry-run (the lock file would be a side effect).
        _lock = (
            contextlib.nullcontext()
            if options.dry_run
            else _pgo_lock(_pgo_lock_path(staging1))
        )
        with _lock:
            # Build WITHOUT installing (both paths). The build mutates nothing,
            # so it runs OUTSIDE the sentinel; a build-pass failure leaves no
            # sentinel behind (matches kernel).
            if pgo:
                built_map, cc, cxx, ld, variant = _build_llvm_pgo_inner(
                    pgo_map, non_pgo_map, lib32_map,
                    staging1, staging, pgo_store, options,
                )
            else:
                built_map, cc, cxx, ld, variant = _build_llvm_single(
                    pgo_map, non_pgo_map, lib32_map, options
                )

            # Gate 2 — pre-install ABI-hazard audit (both paths), OUTSIDE the
            # sentinel: a brick abort here leaves nothing installed and no
            # sentinel, keeping the live toolchain intact.
            _gate2_audit(built_map, dry_run=options.dry_run)

            # Install + post-install verify are the mutation window — wrap them
            # in the sentinel so an interrupted/failed install blocks the next
            # run with a recovery command. CleanExitRequested → RuntimeError
            # translation (with retry_cmd + recovery_cmd) happens inside
            # sentinel_scope. See primitives/stage_sentinel.py.
            with sentinel_scope(
                options.state_dir,
                "toolchain",
                recovery_cmd=_snapshot_recovery_cmd(snapshot),
                retry_cmd="sysforge run toolchain",
                compiler=compiler,
                pgo=pgo,
            ) as sentinel:
                if options.dry_run:
                    _log.ui("[dry-run] would install built toolchain and verify")
                else:
                    label = "PGO optimize" if pgo else "LLVM build (single pass, no PGO)"
                    _pgo_install(label, built_map, options.dry_run)

                # Gate 3 — post-install verify (H). On failure, auto-restore the
                # prior-good toolchain from the snapshot (offline pacman -U):
                #   restore OK   → live /usr is whole again → clear sentinel + raise.
                #   restore FAIL → keep sentinel (recovery_cmd = snapshot restore).
                expected_targets = (tcfg.get("llvm", {}) or {}).get("targets") or None
                if not options.dry_run:
                    issues = _verify_llvm_install(expected_targets=expected_targets)
                    if issues:
                        evidence_path = (
                            _dump_stage_dynsym_evidence(staging, options.state_dir)
                            if pgo
                            else None
                        )
                        _log.warn("Gate 3: post-install LLVM verification failed:")
                        for issue in issues:
                            _log.warn(f"  - {issue}")
                        if evidence_path is not None:
                            _log.warn(f"Diagnostic evidence written to: {evidence_path}")
                        _log.warn("Attempting automatic rollback to the prior-good toolchain…")
                        if _rollback_to_snapshot(snapshot):
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
                            f"{_snapshot_recovery_cmd(snapshot)}"
                        )
                    # Verify passed — safe to wipe the stage2 prefix (PGO only).
                    if pgo:
                        _remove_staging(staging)

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

        _log.ui(
            f"Toolchain stage complete. cc={cc}  cxx={cxx}"
            + (f"  ld={ld}" if ld else ""),
        )
