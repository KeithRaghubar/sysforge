# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
  pgo_staging = "/var/tmp/sysforge-llvm-stage2"   # staging dir for pass-3 binaries
  pgo_store   = "/var/cache/sysforge/llvm-pgo"    # dir for profraw/profdata files

  [packages]
  pgo     = ["llvm", "llvm-libs"]
  non_pgo = ["clang", "lld", "polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
  lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", ...]

LLVM PGO bootstrap (4 passes, only when pgo = true):
  Pass 1 — live system clang (pinned; the resolved profile ships CC=gcc) +
            -fprofile-generate=<pgo_store>/; builds ONLY the
            pgo list (llvm, llvm-libs).  makepkg runs WITHOUT --install; outputs
            are extracted to pgo_staging1 (stage1) by _pgo_stage_instrumented so the
            live /usr is never touched.  Both packages stage — including the
            cmake-config / static-lib `llvm` package — so Pass 2's
            find_package(LLVM) sees the staged headers + configs.  The
            instrumented .a archives staged alongside surface __llvm_profile_*
            link errors for anything that consumes LLVM component targets;
            Pass 2 and Pass 3 work around that via _profile_runtime_ldflag()
            (force-loads the clang profile runtime) AND by selecting lld
            (toolchain_variant="pgo_llvm" → the [VARIANT_LD] guard in
            emit_makepkg_conf): under the CC=gcc profile's default bfd, the
            runtime would otherwise be dropped by strict left-to-right archive
            order. Spurious profraw from CMake feature probes is purged before
            Pass 2.
  Pass 2 — non-instrumented build of the non_pgo packages (clang, lld,
            compiler-rt, polly, openmp, spirv-llvm-translator) against stage1.
            CMAKE_PREFIX_PATH=<staging1>/usr points find_package(LLVM) at stage1
            so the new clang/lld link against stage1's libLLVM.so and are ABI-
            coherent with it.  LD_LIBRARY_PATH is deliberately NOT set —
            forcing the host /usr/bin/clang to load stage1's libLLVM would
            recreate the version-skew failure mode this refactor exists to
            prevent.  Outputs are extracted into the same pgo_staging1, making
            stage1 self-sufficient: it now has a working clang and a working
            libLLVM, both built from the in-tree LLVM source, both ABI-coherent.
  Pass 3  — training run.  CC=<staging1>/usr/bin/clang (built in Pass 2);
            the Pass-3 env redirects dyld/cmake at stage1 via LD_LIBRARY_PATH,
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
            the sysforge controller's 2 GiB cap.  No system install; Pass 3
            binaries extracted to pgo_staging (stage2).  Merged profdata size
            logged at [INFO]; warns if below _PGO_PROFDATA_MIN_BYTES (likely
            indicates bypassed compilation).
  Pass 4  — CC=staged clang from stage2 if available, else system clang.
            CFLAGS/LDFLAGS += -fprofile-use=<profdata>; LTO disabled via
            LTOFLAGS="" (ThinLTO + IR PGO causes non-PIC vtable relocations
            in lld's ThinLTO codegen for libLLVM.so).  Built in coherent
            sub-passes so the non-pgo suite links against the libLLVM that
            ships, NOT the live /usr one (the std::-symbol-re-export profile
            flips between stock/instrumented and -fprofile-use builds, so a
            mismatch dangles libclang-cpp's _ZNSt*@LLVM_* requirements):
            4a builds pgo (llvm/llvm-libs) and stages the optimized result
            to stage3; 4b builds non_pgo (clang, lld, …) with
            CMAKE_PREFIX_PATH=<stage3>/usr; 4c builds lib32 likewise.  All
            packages then installed (pgo + non_pgo + lib32) via
            _pgo_install(); staging prefixes removed on success.  Profdata
            preserved with a version sidecar (clang.profdata.version) for
            reuse by sysforge update.

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
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
_log = log.get_logger("TOOLCHAIN")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.config import (
    find_pkgbuild,
    load_sysforge_toml,
    set_default_toolchain,
    resolve_repo_mode,
    resolve_repo_track,
    REPO_MODE_PACMAN,
)
from sysforge.primitives.llvm_state import (
    collect_llvm_state,
    evaluate_strict,
    render_preflight,
)
from sysforge.primitives.paths import TOOLCHAIN_PATH, resolve_packages_path
from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE
from sysforge.primitives import build_fingerprint, fs_provision, toolchain_safety
from sysforge.primitives.makepkg_pgo import resolve_pgo_store
from sysforge.primitives.pacman import (
    batch_install_pkgs,
    cached_pkg_files_for,
    get_pkgdest,
    install_repo_pkgs,
)
from sysforge.primitives.makepkg_flags import SYNC_FLAGS
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
        repo_track=resolve_repo_track(load_sysforge_toml().get("build", {})),
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
# The canonical lib32 LLVM suite — lib32-llvm, lib32-llvm-libs, lib32-clang,
# lib32-spirv-llvm-translator — is documented here for opt-in reference but NOT
# built by the toolchain stage by default (see _DEFAULT_LLVM_LIB32 below). A user
# opts it back in via `[packages] lib32 = [...]` in toolchain.toml; it is kept as
# prose (not a live constant) because nothing in code consumes it.
# lib32 is intentionally NOT part of the toolchain pass by default. lib32 ships no
# headers of its own and compiles against the all-target 64-bit /usr/include/llvm
# headers, so reducing its LLVM_TARGETS_TO_BUILD (which the toolchain target filter
# does for the host's GPU/CPU) leaves lib32-llvm without the target-init symbols
# lib32-clang's offload tools (clang-nvlink-wrapper / clang-sycl-linker) reference
# from those headers — a hard link failure. PGO is also useless here: an
# x86_64-trained profile is discarded by the i686 build, and lib32 libLLVM is a
# cold path. lib32 builds correctly via `sysforge update` (repo, full targets, no
# PGO). A user can opt lib32 back into the toolchain pass with
# `[packages] lib32 = [...]`; the target-filter exemption (makepkg_wrapper
# ._maybe_patch_llvm_targets) and the lib32 PGO scrub (makepkg_conf) keep that
# path correct.
_DEFAULT_LLVM_LIB32: list[str] = []
_DEFAULT_STAGING_1 = "/var/tmp/sysforge-llvm-stage1"
_DEFAULT_STAGING = "/var/tmp/sysforge-llvm-stage2"
# Pass-4 staging prefix. Holds the *final optimized* libLLVM (+ headers + cmake
# configs) so the non-pgo suite (clang, lld, …) links against the exact libLLVM
# that ships — guaranteeing ABI coherence. Distinct from stage2 (the Pass-3
# training binaries) so the two never conflate. See _build_llvm_pgo_inner Pass 4.
_DEFAULT_STAGING_3 = "/var/tmp/sysforge-llvm-stage3"
# pgo_store resolution (toolchain.toml > SYSFORGE_PGO_STORE > /var/cache default)
# lives in primitives.makepkg_pgo.resolve_pgo_store — the one home shared with
# the reader (_resolve_pgo_state) and the wrapper's orphan-profraw guard.

# Makepkg flags permitted through to PGO builds from user -m input.
# Only force-rebuild is safe; flags that alter build flow (e.g. --noextract,
# --nobuild, --noprepare) would corrupt the instrumentation/use sequence.
_PGO_ALLOWED_MAKEPKG_FLAGS = {"-f", "--force"}

# Interval (seconds) between intermediate profraw merges during Pass 3.
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

# Minimum expected profdata size after a real Pass 3 training run (bytes).
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


def _resolve_packages_repo_mode(config: dict) -> str:
    """Read packages.toml ``[build] repo_mode`` (the single read chokepoint).

    Returns ``"pacman"`` or ``"build_from_source"`` via
    :func:`config.resolve_repo_mode`. A missing/unreadable packages.toml falls
    back to the documented default (``"pacman"``) — that is the correct default
    for the repo-install branch (install the stock LLVM suite rather than build).
    """
    try:
        path = resolve_packages_path(config)
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return REPO_MODE_PACMAN
    return resolve_repo_mode(data.get("build", {}))


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


# Known training-corpus members. "llvm" is the implicit base — the 4-pass build
# compiles LLVM's own source regardless, so listing it is a no-op. Extra members
# (currently only "mesa") are compiled by the instrumented stage1 clang during
# Pass 3 *purely to enrich clang.profdata* with non-LLVM codegen patterns; they
# are never installed and never become -fprofile-use targets (the profile stays
# clang-keyed). See DESIGN.md §Flag/Profile System (training corpus).
_KNOWN_CORPUS = frozenset({"llvm", "mesa"})
_DEFAULT_TRAINING_CORPUS = ["llvm"]


def _resolve_training_corpus(tcfg: dict) -> list[str]:
    """Return the *extra* (non-llvm) training-corpus package names, ordered.

    Reads ``[packages] training_corpus`` (default ``["llvm"]``). "llvm" is the
    implicit base and is stripped from the result; unknown members are warned
    and dropped; duplicates are collapsed. The returned list is exactly what
    Pass 3 additionally compiles with the instrumented stage1 clang so their
    codegen lands in ``clang.profdata``. An empty list means "LLVM self-build
    only" — the historical behaviour.
    """
    raw = tcfg.get("packages", {}).get("training_corpus", _DEFAULT_TRAINING_CORPUS)
    if isinstance(raw, str):
        raw = [raw]
    extras: list[str] = []
    for name in raw:
        if name == "llvm":
            continue  # implicit base; not an "extra"
        if name not in _KNOWN_CORPUS:
            _log.warn(
                f"[PGO] Unknown training_corpus member {name!r} — ignoring "
                f"(known: {', '.join(sorted(_KNOWN_CORPUS))})",
            )
            continue
        if name not in extras:
            extras.append(name)
    return extras


def _bolt_config(tcfg: dict) -> dict:
    """Return the ``[bolt]`` config with defaults applied (LLVM/PGO Pass 5).

    Keys: ``enabled`` (default false — opt-in), ``libllvm`` (also BOLT
    libLLVM.so, default false — the shared lib is more fragile than the clang
    executable), ``training_workload`` (path to a .cpp profiled to collect the
    BOLT profile; empty → a generated header-heavy TU). One read home so the
    Pass-4 emit-relocs gate and the Pass-5 orchestration agree on the same flags.
    """
    bcfg = tcfg.get("bolt", {}) or {}
    return {
        "enabled": bool(bcfg.get("enabled", False)),
        "libllvm": bool(bcfg.get("libllvm", False)),
        "training_workload": str(bcfg.get("training_workload", "") or ""),
    }


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


# ---------------------------------------------------------------------------
# Pass-4 input-fingerprint reuse (opt-in). See primitives/build_fingerprint.py
# and DESIGN.md §Toolchain stage → Pass-4 input-fingerprint reuse.
# ---------------------------------------------------------------------------


@dataclass
class _ReuseCtx:
    """Per-sub-pass context for input-fingerprint build reuse (Pass 4 only).

    ``consult`` gates whether a fingerprint match *skips* the build (opt-in via
    ``--reuse-built`` / ``reuse_unchanged``); the cache is *written* regardless
    so a first, non-opted-in run still populates it for a later resume.
    ``staged_dep_fps`` carries the prior sub-pass's fingerprints (Merkle chain:
    4b/4c fold in 4a's so a rebuilt libLLVM forces its consumers to rebuild).
    """
    pass_id: str
    cache: dict
    cache_path: Path
    config_digest: str
    profdata_sha: str | None
    pkgdest: Path | None
    consult: bool
    staged_dep_fps: list[str] = field(default_factory=list)


def _dep_versions_from_globals(globals_: dict) -> dict[str, str | None]:
    """Installed versions of a PKGBUILD's build deps, for fingerprinting.

    Takes already-parsed PKGBUILD globals, collects depends+makedepends+
    checkdepends (arch arrays already merged by ``parse_pkgbuild``), strips
    version constraints, drops unresolved ``${...}``/``$(...)`` tokens, and
    queries pacman for each installed version. Staged deps (e.g. ``llvm=<ver>``
    satisfied by a stage prefix) map to None — that dimension stays constant in
    the staged context and the staged libLLVM is captured via ``staged_dep_fps``.
    """
    from sysforge.primitives.aur_resolve import _looks_unresolved, _strip_version

    names: set[str] = set()
    for key in ("depends", "makedepends", "checkdepends"):
        for tok in globals_.get(key, []) or []:
            if not tok or _looks_unresolved(tok):
                continue
            bare = _strip_version(tok)
            if bare:
                names.add(bare)
    if not names:
        return {}
    return dict(sorted(_query_pacman_versions(tuple(sorted(names))).items()))


def _pkg_fingerprint(
    ctx: _ReuseCtx,
    name: str,
    pkgbuild_path: Path,
    cc: str | None,
    compiler_flags_extra: str | None,
    linker_flags_extra: str | None,
    cmake_llvm_dir: str | None,
    extra_flags,
) -> tuple[str, str]:
    """Return ``(pkgbase, fingerprint)`` for one PKGBUILD in pass ``ctx.pass_id``.

    Parses the PKGBUILD once (for pkgbase + dep versions) and folds every input
    that determines the build output into the fingerprint. Parse failure is
    non-fatal — the recipe is still captured by ``pkgbuild_sha`` and the missing
    metadata only ever over-invalidates (forces a rebuild), never under.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    try:
        globals_ = parse_pkgbuild(pkgbuild_path).get("globals", {})
    except Exception:  # noqa: BLE001 — fingerprint helper must never abort a build
        globals_ = {}
    pkgbase = globals_.get("pkgbase") or name
    components = {
        "pass_id": ctx.pass_id,
        "pkgbase": pkgbase,
        "pkgbuild_sha": build_fingerprint.hash_file(pkgbuild_path),
        "source_commit": build_fingerprint.source_commit(pkgbuild_path.parent),
        "cc_identity": build_fingerprint.clang_identity(cc),
        "compiler_flags_extra": compiler_flags_extra,
        "linker_flags_extra": linker_flags_extra,
        "cmake_llvm_dir": cmake_llvm_dir,
        "extra_flags": list(extra_flags or []),
        "config_digest": ctx.config_digest,
        "profdata_sha": ctx.profdata_sha,
        "makedep_versions": _dep_versions_from_globals(globals_),
        "staged_dep_fps": sorted(ctx.staged_dep_fps),
    }
    return pkgbase, build_fingerprint.compute_fingerprint(components)


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
    cmake_llvm_dir: str | None = None,
) -> None:
    """Build one package via makepkg_wrapper.run().

    ``toolchain_variant`` is stamped into build_state so ``sysforge update``
    can flag drift. Set only on install-bearing passes — intermediate PGO
    passes (1/2/3) leave it ``None`` so their (transient, soon-overwritten)
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
        cmake_llvm_dir=cmake_llvm_dir,
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
    cmake_llvm_dir: str | None = None,
    reuse: "_ReuseCtx | None" = None,
) -> dict[str, str]:
    """Build all packages in pkgbuild_map for one pass.

    Deduplicates by PKGBUILD directory: split packages that share a directory
    (e.g. llvm, llvm-libs, clang from the same PKGBUILD) are only built once.

    ``staged_deps=True`` means PKGBUILD-declared deps (notably ``llvm=<ver>``)
    are satisfied by a stage prefix (e.g. ``/var/tmp/sysforge-llvm-stage1``)
    rather than by installed pacman packages.  In that mode ``--syncdeps``/``-s``
    is stripped from the resolved profile's makepkg_flags and ``--nodeps`` is
    appended — otherwise makepkg would invoke ``sudo pacman -S llvm=<ver>``,
    fail with "target not found" (the version isn't published anywhere), and
    abort the pass.  Pass 1 sets staged_deps=False because it builds against
    the live system; Pass 2/3/4 set staged_deps=True.

    ``reuse`` enables opt-in input-fingerprint reuse (Pass 4 only). When set,
    each built PKGBUILD's fingerprint is computed and recorded; if
    ``reuse.consult`` and a matching, still-present artifact is cached, the build
    is *skipped* (the on-disk artifact is reused by the later staging/install).
    Returns ``{pkgbase: fingerprint}`` for the dirs built or skipped this pass —
    the caller chains it into the next sub-pass's ``staged_dep_fps`` (Merkle).
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
    fingerprints: dict[str, str] = {}
    with progress.tracker(total, label) as tick:
        for name, pkgbuild_path in pkgbuild_map.items():
            pkg_dir = pkgbuild_path.parent
            if pkg_dir in seen_dirs:
                _log.ui(f"  {name} (split — built with {pkg_dir.name})")
                continue
            seen_dirs.add(pkg_dir)
            tick(name)

            # Input-fingerprint reuse (Pass 4, opt-in). Compute always (so a
            # first run populates the cache); skip the build only when opted in
            # AND the cached artifact set is still valid. Never active in
            # dry-run (no artifacts to validate or reuse).
            fp: str | None = None
            pkgbase = name
            if reuse is not None and not options.dry_run:
                pkgbase, fp = _pkg_fingerprint(
                    reuse, name, pkgbuild_path, cc, compiler_flags_extra,
                    linker_flags_extra, cmake_llvm_dir, extra,
                )
                fingerprints[pkgbase] = fp
                if reuse.consult:
                    key = build_fingerprint.cache_key(reuse.pass_id, pkgbase)
                    hit = build_fingerprint.cache_hit(reuse.cache, key, fp)
                    if hit:
                        _log.ui(
                            f"  [PGO] reusing cached build of {pkgbase} "
                            f"({reuse.pass_id}) — fingerprint match, "
                            f"{len(hit)} artifact(s) on disk; skipping rebuild",
                        )
                        continue  # build skipped; do not consume `first`

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
                cmake_llvm_dir=cmake_llvm_dir,
            )
            first = False

            if reuse is not None and fp is not None:
                members = [n for n, p in pkgbuild_map.items() if p.parent == pkg_dir]
                search_dirs = ([reuse.pkgdest] if reuse.pkgdest else []) + [pkg_dir]
                key = build_fingerprint.cache_key(reuse.pass_id, pkgbase)
                build_fingerprint.record_build(
                    reuse.cache, key, fp, search_dirs, members,
                )
                build_fingerprint.save_cache(reuse.cache_path, reuse.cache)
    return fingerprints


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


def _extract_built_to_staging(
    pkgbuild_map: dict[str, Path], staging: Path, dry_run: bool
) -> None:
    """
    After Pass 3 build (no install), find .pkg.tar* in each build dir (or
    PKGDEST if set in the system makepkg.conf) and extract to staging prefix.
    The staged binaries are used as CC/CXX in Pass 4.
    """
    if dry_run:
        _log.ui(f"[dry-run] would extract pass-3 packages to {staging}")
        return

    from sysforge.primitives.config import parse_system_makepkg_conf

    sys_conf = parse_system_makepkg_conf()
    pkgdest_raw = sys_conf.get("PKGDEST")
    pkgdest = Path(pkgdest_raw).expanduser() if pkgdest_raw else None
    if pkgdest:
        _log.info(
            f"[PGO] PKGDEST={pkgdest} — searching there for Pass 3 packages",
        )

    _log.ui(f"─── Pass 3: staging extraction → {staging} ────────")
    for name, pkgbuild_path in pkgbuild_map.items():
        build_dir = pkgbuild_path.parent
        # PKGDEST takes precedence; fall back to PKGBUILD directory.
        search_dirs = [pkgdest] if pkgdest and pkgdest.is_dir() else []
        search_dirs.append(build_dir)
        pkgs: list[Path] = []
        for d in search_dirs:
            # *.pkg.tar* matches both compressed (.pkg.tar.zst) and
            # uncompressed (.pkg.tar) packages (PKGEXT='.pkg.tar').
            # The version segment is anchored with [0-9] (pkgver/epoch always
            # starts with a digit) so a shorter pkgname does NOT swallow a
            # longer sibling that shares its prefix — critically, name="llvm"
            # must not match the split sibling "llvm-libs-…" (a plain
            # f"{name}-*" glob does, and the mtime tiebreak below would then
            # stage llvm-libs for the "llvm" key, leaving staging3 without
            # LLVMConfig.cmake/headers and silently defeating the Pass-4 split).
            # Same version-anchored idiom as pacman.cached_pkg_files_for and
            # build_core._find_existing_artifacts. Sort by mtime descending and
            # take only the newest to avoid extracting stale packages from
            # previous runs in PKGDEST.
            candidates = [p for p in d.glob(f"{name}-[0-9]*-*.pkg.tar*")
                          if not p.name.endswith(".sig")]
            if candidates:
                pkgs = [max(candidates, key=lambda p: p.stat().st_mtime)]
                break
        if not pkgs:
            searched = ", ".join(str(d) for d in search_dirs)
            raise RuntimeError(
                f"[TOOLCHAIN] No .pkg.tar* found for {name!r} in: {searched}. "
                "Pass 3 build may have failed."
            )
        for pkg_file in pkgs:
            _extract_pkg_to_staging(pkg_file, staging)
        _log.ui(f"  {name}: staged")


def _assert_staging_has_llvm_cmake(staging: Path) -> None:
    """Fail fast when ``staging`` lacks ``usr/lib/cmake/llvm/LLVMConfig.cmake``.

    Pass 4b/4c steer ``find_package(LLVM)`` at ``<staging>/usr`` via
    ``CMAKE_PREFIX_PATH``. If the cmake config is missing — e.g. the split
    ``llvm`` (dev) artifact was not staged, only ``llvm-libs`` — find_package
    silently falls back to the system ``/usr`` LLVM, so the non-pgo suite links
    against the wrong libLLVM and bricks at Gate 3. Catching it here turns a
    multi-hour build + failed install into an immediate, actionable abort.
    """
    cfg = staging / "usr/lib/cmake/llvm/LLVMConfig.cmake"
    if not cfg.exists():
        raise RuntimeError(
            f"[TOOLCHAIN] Pass-4 staging is incomplete: {cfg} is missing. The "
            "optimized 'llvm' dev package (cmake config + headers) was not "
            "staged, so clang/lld would link against the live /usr libLLVM "
            "instead of the libLLVM being shipped. Aborting before install."
        )


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


def _assert_pass_links_shipped_libllvm(
    pkgbuild_map: dict[str, Path], *, label: str, dry_run: bool
) -> None:
    """Fail fast when a just-built non-pgo pass linked the wrong libLLVM.

    After Pass 4b/4c (clang/lld/… built against the staged *shipped* libLLVM but
    not yet installed), scan the produced ``.pkg.tar*`` for the
    std::-bound-to-LLVM hazard (any ``_ZNSt*@LLVM_*`` undefined ref). A
    correctly-steered build binds its C++ stdlib symbols to libstdc++
    (``@GLIBCXX_*``) and yields zero such refs; a non-empty result means
    ``find_package(LLVM)`` resolved the live ``/usr`` libLLVM despite
    ``-DLLVM_DIR``/``CMAKE_PREFIX_PATH`` — the Gate-3 brick in the making.
    Raising here aborts **before install** (no sentinel, no rollback) and names
    the offending pass. Reuses :func:`toolchain_safety.scan_abi_hazards` (the
    same check Gate 2 runs over the full set) — no parallel symbol differ.
    """
    if dry_run:
        return
    pkgs = _collect_pgo_packages(pkgbuild_map)
    if not pkgs:
        return
    findings = toolchain_safety.scan_abi_hazards(pkgs)
    if findings:
        joined = "\n".join(f"  - {f.message}" for f in findings)
        raise RuntimeError(
            f"[TOOLCHAIN] {label}: the built suite linked C++ stdlib symbols "
            "against the live /usr libLLVM instead of the staged libLLVM that "
            "ships — the Pass-4 split was defeated (find_package(LLVM) ignored "
            "the staged prefix despite -DLLVM_DIR). Nothing was installed; the "
            "live toolchain is untouched.\n"
            f"{joined}\n"
            "Verify the staged 'llvm' dev package (cmake config + headers) "
            "reached the staging prefix, then rerun (optionally with "
            "--rebuild-profdata)."
        )


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


def _pgo_stage_instrumented(
    pkgbuild_map: dict[str, Path], staging1: Path, dry_run: bool
) -> None:
    """Extract every Pass 1 package into ``staging1`` instead of installing to ``/usr``.

    Path B of the PGO audit (see DESIGN.md §Toolchain stage / PGO): Pass 1
    must not touch the live root.  Installing an instrumented ``libLLVM.so``
    over the system copy leaves the pre-existing ``/usr/bin/clang`` ABI-
    incompatible with the just-installed lib (weak/inlined symbols elided by
    ``-fprofile-generate`` are no longer exported) and breaks every subsequent
    invocation — including the CMake compiler check that Pass 3's makepkg run
    triggers.  Staging keeps the instrumented surface off the system entirely.

    Phase 2: the cmake-config / static-lib package (typically ``llvm``) is
    **included** here, so Pass 2 and Pass 3's ``find_package(LLVM)`` find
    stage1's headers and cmake configs.  The instrumented ``.a`` archives that
    land alongside still cause ``__llvm_profile_*`` link errors for anything
    that consumes LLVM component targets — Pass 2 and Pass 3 work around it
    via ``linker_flags_extra = _profile_runtime_ldflag()``.
    """
    if dry_run:
        _log.ui(f"[dry-run] would extract Pass 1 packages to {staging1}")
        return

    all_pkgs = _collect_pgo_packages(pkgbuild_map)
    if not all_pkgs:
        raise RuntimeError(
            "[TOOLCHAIN] No built packages found for Pass 1 — "
            "check that the build completed successfully"
        )

    _log.ui(f"[PGO] Staging {len(all_pkgs)} Pass 1 package(s) → {staging1}:")
    for pkg_file in all_pkgs:
        _extract_pkg_to_staging(pkg_file, staging1)


def _stage_env(staging: Path) -> dict[str, str]:
    """Env-var injection so makepkg's child processes (cmake/clang/ld) find a
    staged LLVM prefix instead of (or before) the live ``/usr``.

    Prepends to the inheritable variables so anything actually present in
    ``staging`` wins, but the system fallback still resolves libraries we
    haven't staged (``libstdc++.so``, ``glibc`` data, etc.).  Used by Pass 3
    (pointed at stage1) and Pass 4 (pointed at stage2).
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

    Used before Pass 3 to detect whether a previous Pass 1 install left instrumented
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
    """Return a force-load LDFLAGS fragment for the clang profile runtime, or None.

    Form: ``-Wl,--push-state,--whole-archive <profile_lib>.a -Wl,--pop-state``,
    using the full archive path so the linker locates it unambiguously. Returns
    None if the runtime library cannot be located, with a log warning.

    Injected into LDFLAGS for Pass 2 and Pass 3 (the passes that link against
    stage1's instrumented LLVM static libs), so packages linking those archives
    can resolve __llvm_profile_* symbols. ``--whole-archive`` force-loads the
    runtime regardless of link order: a bare ``-lclang_rt.profile`` appended to
    LDFLAGS lands ahead of the libraries in CMAKE_EXE_LINKER_FLAGS, and bfd's
    strict left-to-right archive resolution then drops it before the
    instrumented archives reference it. lld is order-independent, but the
    force-load keeps correctness from depending on the linker. push-state /
    pop-state confines the --whole-archive to this one archive so surrounding
    --as-needed behaviour is preserved.
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
            "Pass 3 and Pass 4 may fail with undefined __llvm_profile_* symbols. "
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
            "Pass 3 and Pass 4 may fail with undefined __llvm_profile_* symbols. "
            "Install compiler-rt or run: sudo pacman -S llvm",
        )
        return None

    return f"-Wl,--push-state,--whole-archive {profile_lib} -Wl,--pop-state"


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
    Background thread: wake every _PGO_MERGE_INTERVAL seconds during Pass 3
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
    Final profraw sweep after Pass 3 completes (daemon already stopped).

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
            f"[TOOLCHAIN] No .profraw files and no profdata in {pgo_store} after Pass 3. "
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
                f"< {_PROFRAW_SETTLE_SECS}s ago at end of Pass 3); "
                "profile data from background merges is complete enough to proceed.",
            )
            return profdata_path
        raise RuntimeError(
            "[TOOLCHAIN] Final profraw merge produced no output — "
            + (
                f"{len(fresh)} profraw file(s) too fresh to merge safely and no "
                "profdata from background merges; Pass 3 may have produced no profile data"
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
    in-tree PGO PKGBUILDs (what Pass 3 instrumented), not from the system
    `pacman -Q llvm` which can disagree across a major bump.  Called right
    after Pass 3 completes so an aborted Pass 4 still leaves recoverable
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
    ``ldd`` output of an installed binary means Pass 4 packaged a bad
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
                    "— Pass 4 packaged a bad RPATH or the install is incomplete"
                )
            elif not p.startswith("/usr/lib"):
                issues.append(
                    f"{label} resolves libLLVM outside /usr/lib: {p} "
                    "— a sibling libLLVM is shadowing the package-managed one"
                )
    return issues


def _nm_dynsyms(so: Path, *, undefined: bool) -> list[str]:
    """Return ``nm -D`` symbol names (with their ``@version`` suffix) from ``so``.

    ``undefined=True`` lists undefined references (``U``); otherwise defined
    exports (``--defined-only``). Returns ``[]`` if ``nm`` is missing or fails.
    """
    flag = "--undefined-only" if undefined else "--defined-only"
    try:
        proc = subprocess.run(
            ["nm", "-D", flag, str(so)],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    return [parts[-1] for parts in (ln.split() for ln in proc.stdout.splitlines()) if parts]


def _so_ver(path: Path) -> tuple[int, ...]:
    """Numeric version tuple parsed from a ``lib*.so.<ver>`` filename (() if none).

    ``libLLVM.so.22.1`` → ``(22, 1)``; stops at the first non-numeric component
    so a plain ``lib*.so`` symlink yields ``()``. Used to compare sonames
    numerically rather than lexically (``.21.1`` must not sort ahead of ``.9``).
    """
    marker = ".so."
    idx = path.name.rfind(marker)
    if idx == -1:
        return ()
    out: list[int] = []
    for part in path.name[idx + len(marker):].split("."):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


def _newest_so(base: Path, name_glob: str) -> Path | None:
    """Highest-versioned regular ``base/usr/lib/<name_glob>`` match.

    Unlike :func:`_first_so` (lexical first), this picks the newest soname, so a
    compat package's older library (e.g. ``llvm21-libs``'s ``libLLVM.so.21.1``
    sitting alongside ``llvm-libs``'s ``libLLVM.so.22.1``) never shadows the
    real, just-installed one.
    """
    cands = [p for p in (base / "usr/lib").glob(name_glob)
             if p.is_file() and not p.is_symlink()]
    if not cands:
        return None
    return max(cands, key=_so_ver)


def _dump_stage_dynsym_evidence(
    staging3: Path,
    dest_dir: Path | str | None,
    *,
    install_root: Path = Path("/"),
) -> Path | None:
    """Capture Gate-3 symbol-version brick evidence to ``llvm_abi_hazard.log``.

    The brick is a C++ stdlib symbol bound to libLLVM's ``LLVM_<ver>`` version
    node: a consumer (``libclang-cpp`` / ``liblldCommon``) carries an *undefined*
    ``_ZNSt*@LLVM_<ver>`` reference that the *installed* ``libLLVM`` does not
    export. The actionable evidence is the **difference** between what the
    installed consumers demand under ``@LLVM_*`` and what the installed
    ``libLLVM`` provides — read straight from the live ``/usr`` files that
    bricked, not from a staging prefix (the previous stage2 dump captured an
    unrelated, often stale, library and never contained the brick symbols).

    ``staging3`` (the libLLVM Pass 4b linked against) is included for contrast:
    if it exports the missing symbols but the installed ``libLLVM`` does not, the
    split staged the wrong/incomplete libLLVM (e.g. a missing ``LLVMConfig.cmake``
    sent ``find_package(LLVM)`` back to ``/usr``). Returns the log path, or
    ``None`` when the dest is unset or no ``libLLVM`` is installed.
    """
    if dest_dir is None:
        return None

    # Consumers that bind C++ stdlib into the LLVM version namespace and brick.
    consumers = [
        c for c in (
            _newest_so(install_root, "libclang-cpp.so.*"),
            _newest_so(install_root, "liblldCommon.so.*"),
        )
        if c is not None
    ]
    # Select the installed libLLVM whose soname matches what the (newest)
    # consumers link — NOT the lexical-first glob, which picks a compat package's
    # older libLLVM.so.<old> (e.g. llvm21-libs' .21.1) ahead of the just-built
    # .22.1 and reports a false "0 NOT provided" all-clear. clang/llvm are in
    # version lockstep, so the consumer soname version == the wanted libLLVM one.
    target_ver = max((_so_ver(c) for c in consumers), default=())
    installed_libllvm: Path | None = None
    if target_ver:
        cand = (install_root / "usr/lib"
                / f"libLLVM.so.{'.'.join(str(x) for x in target_ver)}")
        if cand.is_file():
            installed_libllvm = cand
    if installed_libllvm is None:
        installed_libllvm = _newest_so(install_root, "libLLVM.so.*")
    if installed_libllvm is None:
        return None

    def _llvm_std(syms: list[str]) -> set[str]:
        # Normalise @@ (defined) and @ (undefined) so the two sets compare;
        # keep only C++ stdlib symbols bound to an LLVM version node.
        return {
            s.replace("@@", "@")
            for s in syms
            if "_ZNSt" in s and "@LLVM_" in s
        }

    provided = _llvm_std(_nm_dynsyms(installed_libllvm, undefined=False))
    staged_libllvm = _newest_so(staging3, "libLLVM.so.*")
    staged_provided = (
        _llvm_std(_nm_dynsyms(staged_libllvm, undefined=False))
        if staged_libllvm is not None
        else set()
    )

    lines: list[str] = [
        "# Gate-3 ABI brick evidence — C++ stdlib symbols bound to LLVM_<ver>",
        f"# installed libLLVM: {installed_libllvm}  "
        f"({len(provided)} _ZNSt*@LLVM_* exports)",
        f"# staged   libLLVM: {staged_libllvm or '(absent)'}  "
        f"({len(staged_provided)} _ZNSt*@LLVM_* exports)",
        "",
    ]
    for so in consumers:
        demanded = _llvm_std(_nm_dynsyms(so, undefined=True))
        missing = sorted(demanded - provided)
        lines.append(
            f"## {so}: {len(demanded)} _ZNSt*@LLVM_* undefined refs, "
            f"{len(missing)} NOT provided by installed libLLVM"
        )
        if missing:
            lines.append(
                "# >>> brick cause: demanded under @LLVM_* but absent in installed libLLVM:"
            )
            lines.extend(missing)
            in_staged = [m for m in missing if m in staged_provided]
            if in_staged:
                lines.append(
                    f"# note: {len(in_staged)}/{len(missing)} of these ARE exported by "
                    "the staged (staging3) libLLVM — the shipped libLLVM differs "
                    "from what Pass 4b linked against:"
                )
                lines.extend(in_staged)
        lines.append("")

    lines.append(
        f"# Full nm -D --defined-only {installed_libllvm} (_ZNSt* only):"
    )
    lines.extend(
        sorted(s for s in _nm_dynsyms(installed_libllvm, undefined=False) if "_ZNSt" in s)
    )

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "llvm_abi_hazard.log"
    out.write_text("\n".join(lines) + "\n")
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
    2. ``clang --version`` and ``ld.lld --version`` run without crashing.
    3. ``llvm-config --targets-built`` is a superset of
       ``expected_targets`` (skipped when ``expected_targets`` is None or
       empty — i.e. no LLVM_TARGETS filtering was configured).
    4. ``ldd`` of installed clang/lld resolves libLLVM under /usr/lib —
       never under /var/tmp/sysforge-llvm-stage*.  Catches a Pass-4 RPATH
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

    # Probe ``ld.lld``, not bare ``lld``: ``lld`` is the generic multiplexer
    # driver and dispatches on argv[0], so ``lld --version`` always exits 1
    # ("lld is a generic driver"). ``ld.lld`` is the GNU-compatible flavor that
    # ``-fuse-ld=lld`` actually resolves to for every build pass.
    for cmd, label in (
        (["clang", "--version"], "clang --version"),
        (["ld.lld", "--version"], "ld.lld --version"),
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
    staging3: Path,
    pgo_store: Path,
    options,
    *,
    config_digest: str = "",
    reuse_built: bool = False,
    corpus_map: dict[str, Path] | None = None,
    bolt_relocs: bool = False,
) -> tuple[dict[str, Path], str, str, str, str]:
    """
    4-pass LLVM PGO build. Builds WITHOUT installing.

    Returns ``(built_map, cc, cxx, ld, variant)`` where ``built_map`` is the
    Pass-4 package map (pgo ∪ non_pgo ∪ lib32) the caller installs *after* the
    Gate-2 ABI audit, inside the sentinel. Passes 1/2/3 and the Pass-4 build
    all run with ``install=False`` so the live ``/usr`` is untouched until the
    caller's install step — a build-pass failure therefore leaves no sentinel.

    Pass 1: live system clang (pinned — the resolved profile ships CC=gcc) +
             -fprofile-generate; builds ONLY the pgo list
             (llvm, llvm-libs).  makepkg runs WITHOUT --install; outputs are
             extracted to pgo_staging1 (stage1) — the live /usr is never
             touched.  Both packages stage, including the cmake-config /
             static-lib `llvm` package, so Pass 2's find_package(LLVM)
             resolves stage1's headers + configs.
    Pass 2: non-instrumented build of non_pgo packages (clang, lld,
             compiler-rt, polly, openmp, spirv-llvm-translator) against
             stage1.  CMAKE_PREFIX_PATH=<staging1>/usr; LD_LIBRARY_PATH is
             deliberately NOT set (forcing system clang to load stage1's
             libLLVM would recreate the version-skew failure mode this
             refactor exists to prevent).  Links with lld
             (toolchain_variant="pgo_llvm") + a force-loaded profile runtime so
             stage1's instrumented archives resolve __llvm_profile_*.  Outputs
             extracted into the same pgo_staging1, making stage1 self-sufficient
             — both a working clang and a working libLLVM, both ABI-coherent.
    Pass 3:  training run.  CC=<staging1>/usr/bin/clang (built in Pass 2);
             the running clang and the libLLVM it loads are guaranteed
             coherent because they were built together.  Builds pgo + non_pgo;
             generates profraw as a side effect.  CCACHE_DISABLE=1 and
             SCCACHE_DISABLE=1 injected so cache tools cannot bypass the
             instrumented compiler and silently produce no profraw.
             Background daemon merges profraw periodically; final sweep after
             build.  Profdata size checked; warns if suspiciously small.
             Pgo-package binaries extracted to pgo_staging (stage2); no
             system install.
    Pass 4:  CC=staged clang from stage2 if available, else system clang.
             CFLAGS/LDFLAGS += -fprofile-use; LTO disabled via LTOFLAGS=""
             (ThinLTO + IR PGO causes non-PIC vtable relocations in lld's
             ThinLTO codegen for libLLVM.so).  Built in coherent sub-passes
             so the non-pgo suite links against the libLLVM that ships (not
             the live /usr one): 4a builds pgo (llvm/llvm-libs) and stages the
             optimized result into ``staging3``; 4b builds non_pgo (clang,
             lld, …) with CMAKE_PREFIX_PATH=<staging3>/usr so
             find_package(LLVM) resolves the just-built libLLVM; 4c builds
             lib32 likewise.  Without this, clang/libclang-cpp records
             ``_M_assign@LLVM_<ver>`` (the live libLLVM re-exports the C++
             stdlib) while the shipped -fprofile-use libLLVM inlines it away
             and exports nothing — a runtime symbol-lookup brick.  All
             packages installed (pgo + non_pgo + lib32) by the caller.
             Staging prefixes removed on success.  Profdata preserved with a
             version sidecar (clang.profdata.version) for reuse by sysforge update.

    Returns (cc, cxx, ld).
    """
    staged_cc = str(staging / "usr/bin/clang")
    staged_cxx = str(staging / "usr/bin/clang++")
    corpus_map = corpus_map or {}

    n_pgo = len(set(pgo_map.values()))
    n_total = len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))

    _validate_pgo_environment(options.dry_run)

    # Check for existing compatible profdata before purging.
    # If profdata from a previous run matches the target LLVM major version,
    # skip passes 1-3 and go straight to Pass 4 (the optimized build).
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
            f"[PGO] Skipping passes 1-3 (instrument/bootstrap/train), building with existing profdata  "
            f"({n_total} package(s) across {len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))} PKGBUILD(s))  "
            f"pgo_store={pgo_store}",
        )

    if not skip_profgen and not options.dry_run:
        import shutil as _shutil

        # Prompt #2 — purge staging/pgo_store. rmtree is silently destructive
        # of partial Pass-1/Pass-4 staging from a prior failed run, so gate it.
        if staging1.exists() or staging.exists() or staging3.exists() or pgo_store.exists():
            _pgo_confirm(
                f"Purge staging dirs and pgo_store to start fresh 4-pass build?\n"
                f"  stage1:    {staging1}\n"
                f"  stage2:    {staging}\n"
                f"  stage3:    {staging3}\n"
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
            "~2-3 hours; pass 3 is a long instrumented training run. "
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
        if staging3.exists():
            _log.info(f"[PGO] Purging stale stage3: {staging3}")
            _shutil.rmtree(staging3)
        if pgo_store.exists():
            # Empty the contents but keep the node: pgo_store lives under a
            # root-owned FHS parent (/var/cache/sysforge), so rmtree's final
            # rmdir would need write on that parent and fail with EACCES.
            _log.info(f"[PGO] Purging stale pgo_store contents: {pgo_store}")
            fs_provision.empty_dir_contents(pgo_store)
        fs_provision.ensure_writable_dir(pgo_store, dry_run=options.dry_run)

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
            # Pass 1 — build pgo packages with the live system clang +
            # -fprofile-generate. cc MUST be pinned to clang explicitly: the
            # resolved profile is `[profiles.standard]`, shipped as CC=gcc, and
            # cc=None would let that gcc win. clang's -fprofile-generate emits
            # LLVM-format .profraw and __llvm_profile_* refs in the staged .a
            # archives (resolved by Pass 2's _profile_runtime_ldflag); gcc's
            # would emit gcov __gcov_* refs that the clang profile runtime
            # cannot satisfy, bricking the Pass 2 link. Mirrors Pass 2's
            # explicit cc="/usr/bin/clang" (this branch only runs for
            # compiler="llvm"; the gcc path returns early before any PGO pass).
            # makepkg runs WITHOUT --install; outputs are extracted into stage1
            # by _pgo_stage_instrumented so the live root is never touched.
            _build_pass(
                "PGO 1/4 · instrument llvm / llvm-libs",
                pgo_map,
                options,
                cc="/usr/bin/clang",
                cxx="/usr/bin/clang++",
                install=False,
                pgo_build=True,
                compiler_flags_extra=f"-fprofile-generate={pgo_store}/",
                # Select lld like every other LLVM toolchain build. The PGO
                # bootstrap runs under a CC=gcc profile whose makepkg.conf
                # defaults to bfd; only the [VARIANT_LD] guard (keyed on this
                # variant) injects -fuse-ld=lld. Pass 1 links via the
                # -fprofile-generate driver flag regardless, but stay consistent
                # with Pass 2/3/4 so the whole sequence uses one linker.
                toolchain_variant="pgo_llvm",
            )
            _pgo_stage_instrumented(pgo_map, staging1, options.dry_run)
            _log.ui("[PGO] 1/4 complete (staged to "
                    f"{staging1} — system /usr untouched)")

            # Purge any profraw accumulated during Pass 1 + 2. CMake feature-
            # test programs compiled with -fprofile-generate run during
            # configuration and deposit spurious profraw files (and emit
            # "Running out of static counters" warnings). Those represent tiny
            # probe programs, not clang doing real work — they would contaminate
            # the training profile if kept. Pass 3 generates the real data.
            # We purge here (after Pass 1) and again after Pass 2 — the latter
            # only matters if Pass 2's profile runtime injection somehow caused
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
                        f"from Pass 1 CMake probes",
                    )

            # Pass 2 — build non_pgo packages (clang, lld, compiler-rt, …)
            # NON-instrumented but against stage1's headers and libs. The
            # output binaries are ABI-coherent with stage1's libLLVM, so
            # stage1/usr/bin/clang can drive Pass 3 without version drift
            # against the live /usr clang.
            #
            # CC=/usr/bin/clang here is the bootstrap host compiler — it
            # compiles C++ source into objects, not anything that loads stage1's
            # libLLVM. We deliberately do NOT inject LD_LIBRARY_PATH for
            # Pass 2; that would force the system clang to load stage1's
            # (possibly newer) libLLVM, recreating the version-skew failure
            # mode this whole refactor exists to prevent.
            #
            # CMAKE_PREFIX_PATH points cmake's find_package(LLVM) at stage1
            # so the new clang links against stage1's libLLVM.so. The
            # instrumented .a archives staged alongside surface __llvm_profile_*
            # link errors; _profile_runtime_ldflag() force-loads the clang
            # profile runtime to satisfy them. toolchain_variant="pgo_llvm"
            # selects lld via the [VARIANT_LD] guard — without it this pass
            # falls back to the CC=gcc profile's bfd, whose strict left-to-right
            # archive resolution drops the profile runtime before the
            # instrumented archives reference it (the historical Pass 2
            # failure). lld resolves it regardless of order; the --whole-archive
            # form of _profile_runtime_ldflag() makes it order-proof either way.
            bootstrap_env = {
                "CMAKE_PREFIX_PATH": f"{staging1}/usr",
            }
            residual_linker_flags = _profile_runtime_ldflag()
            if residual_linker_flags is not None:
                _log.info(
                    "[PGO] Pass 2: injecting clang profile runtime into LDFLAGS "
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
                pgo_env=bootstrap_env,
                staged_deps=True,
                cmake_llvm_dir=f"{staging1}/usr/lib/cmake/llvm",
                toolchain_variant="pgo_llvm",
            )
            _extract_built_to_staging(non_pgo_map, staging1, options.dry_run)
            _log.ui(f"[PGO] 2/4 complete (stage1 self-sufficient at {staging1})")

            # Pass 3 — training run. CC is stage1's freshly built clang (built
            # in Pass 2 against stage1's instrumented libLLVM), so the running
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
            train_env = {
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
                f"[PGO] Pass 3: LLVM_PROFILE_FILE={train_env['LLVM_PROFILE_FILE']}",
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
            # Pass 2 produces stage1's clang only when non_pgo_map is non-
            # empty (a deliberately empty list — used by tests / minimal
            # configs — skips Pass 2 entirely).  Fall back to /usr/bin/clang
            # in that case; the live system stays the bootstrap for Pass 3.
            if stage1_clang.exists() or options.dry_run:
                pass2_cc, pass2_cxx = str(stage1_clang), str(stage1_clangxx)
            else:
                _log.info(
                    f"[PGO] Pass 3: stage1 clang absent at {stage1_clang} "
                    "(non_pgo_map empty — Pass 2 skipped); falling back to "
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
                    pgo_env=train_env,
                    staged_deps=True,
                    # lld parity (see Pass 2): without it this pass links the
                    # instrumented stage1 archives under the gcc profile's bfd
                    # and the bare profile-runtime ref drops out by order.
                    toolchain_variant="pgo_llvm",
                )
                # Training-corpus enrichment (mesa, …). Compiled by the SAME
                # instrumented stage1 clang and SAME LLVM_PROFILE_FILE so their
                # codegen profraw lands in pgo_store and merges into
                # clang.profdata alongside the LLVM self-build's. These targets
                # are NEVER installed and never become -fprofile-use targets —
                # they only broaden the corpus toward graphics/C-heavy code the
                # LLVM self-compilation under-exercises. The merge daemon is
                # still running here, and the final _merge_profraw sweep below
                # picks up whatever this adds. staged_deps=True keeps the
                # no-pacman-mutation invariant (--nodeps, no --syncdeps), so the
                # extras' makedepends must already be installed. Best-effort: a
                # corpus build failure (missing makedep, mesa configure quirk)
                # is logged and the PGO run proceeds with the LLVM-only profile
                # — enrichment must never brick the toolchain build.
                if corpus_map:
                    try:
                        _build_pass(
                            f"PGO 3/4 · corpus enrich ({', '.join(corpus_map)})",
                            corpus_map,
                            options,
                            cc=pass2_cc,
                            cxx=pass2_cxx,
                            install=False,
                            linker_flags_extra=residual_linker_flags,
                            pgo_build=True,
                            pgo_env=train_env,
                            staged_deps=True,
                            toolchain_variant="pgo_llvm",
                        )
                    except Exception as e:
                        _log.warn(
                            f"[PGO] Training-corpus enrichment build failed "
                            f"({', '.join(corpus_map)}): {e} — continuing with "
                            "LLVM-only profile data",
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
                        "Pass 3 may not have exercised enough code paths — "
                        "check that CCACHE_DISABLE/SCCACHE_DISABLE took effect "
                        "and compilation actually ran.",
                    )
                    # Prompt #4 — abort before Pass 4 unless the user explicitly
                    # accepts the suspicious profdata. Wrong profdata silently
                    # mis-optimises the resulting compiler, so default to no.
                    _pgo_confirm(
                        f"Pass 3 profdata is suspiciously small "
                        f"({profdata_size // (1024 * 1024)} MiB) — "
                        "instrumentation may have been bypassed. "
                        "Continue to Pass 4 with this profdata? [y/N]:",
                        default="n",
                        eof_default="n",
                        options=options,
                        abort_msg="user declined Pass 4 due to suspicious profdata",
                    )
            _log.ui(f"[PGO] Profile data ready: {profdata_path}")
            # Write the sidecar now (right after Pass 3 has produced the
            # profdata, before Pass 4 starts) so an aborted Pass 4 still
            # leaves recoverable profdata that the next run can reuse via
            # _check_existing_profdata.  The sidecar's only invariant is
            # "this profdata is for LLVM major N", determined entirely by
            # what Pass 3 instrumented — Pass 4 success has no bearing on it.
            if not options.dry_run:
                _write_profdata_version(pgo_store, pgo_map)
            _extract_built_to_staging(pgo_map, staging, options.dry_run)

        # Pass 4 (or sole pass when reusing profdata) — PGO-optimized build.
        # Use staged clang from Pass 3 if available; otherwise fall back to
        # system clang (which, after a prior successful run, is already PGO-optimized).
        using_staged_cc = (
            not skip_profgen
            and not options.dry_run
            and Path(staged_cc).exists()
        )
        if not skip_profgen and not options.dry_run and not using_staged_cc:
            _log.info(
                f"[PGO] staged clang not found at {staged_cc} "
                "(clang is non-pgo) — using system clang for Pass 4",
            )
            pass3_cc, pass3_cxx = "/usr/bin/clang", "/usr/bin/clang++"
        elif skip_profgen:
            # No staging when reusing profdata — system clang is the compiler
            pass3_cc, pass3_cxx = "/usr/bin/clang", "/usr/bin/clang++"
        else:
            pass3_cc, pass3_cxx = staged_cc, staged_cxx

        all_pass3 = {**pgo_map, **non_pgo_map, **lib32_map}
        opt = "reusing profdata" if skip_profgen else "PGO 4/4"
        profile_use = f"-fprofile-use={profdata_path}"

        # Input-fingerprint reuse setup (Pass 4 only). The cache lives in
        # pgo_store, so it is wiped on a fresh 4-pass start but survives a
        # profdata-reuse resume. The profdata is hashed once (constant across
        # 4a/4b/4c). The cache is always *written*; it is only *consulted*
        # (skipping rebuilds) when ``reuse_built`` is opted in.
        reuse_cache: dict = {}
        reuse_cache_path = pgo_store / "build_cache.json"
        reuse_profdata_sha: str | None = None
        reuse_pkgdest: Path | None = None
        if not options.dry_run:
            reuse_cache = build_fingerprint.load_cache(reuse_cache_path)
            reuse_profdata_sha = build_fingerprint.hash_file(profdata_path)
            reuse_pkgdest = get_pkgdest()
            if reuse_built:
                _log.ui(
                    "[PGO] --reuse-built: unchanged Pass-4 packages will be "
                    f"reused from cache at {reuse_cache_path}",
                )

        def _mk_reuse_ctx(pass_id: str, staged_dep_fps: list[str]) -> _ReuseCtx:
            return _ReuseCtx(
                pass_id=pass_id,
                cache=reuse_cache,
                cache_path=reuse_cache_path,
                config_digest=config_digest,
                profdata_sha=reuse_profdata_sha,
                pkgdest=reuse_pkgdest,
                consult=reuse_built,
                staged_dep_fps=staged_dep_fps,
            )
        # Pass 4 is split into coherent sub-passes (4a pgo → stage3 → 4b non_pgo
        # → 4c lib32) so the non-pgo suite links against the *final optimized*
        # libLLVM that ships, NOT the live /usr one. The std::-symbol re-export
        # profile flips between stock/instrumented builds (an out-of-line weak
        # copy of e.g. std::string::_M_assign is emitted and globbed into
        # LLVM_<ver> by the `global: *` version script) and -fprofile-use builds
        # (inlined away → imported from libstdc++ as @GLIBCXX_*). If clang links
        # against an exporting libLLVM but the run ships a non-exporting one,
        # libclang-cpp dangles `_ZNSt*@LLVM_<ver>` and the live clang bricks at
        # the first symbol lookup. Building 4b against staging3 makes clang record
        # its true ABI (@GLIBCXX_*), coherent with the shipped libLLVM.

        # 4a — optimize the pgo packages (llvm/llvm-libs). Clear
        # LLVM_PROFILE_FILE so the Pass-3 training path doesn't leak; redirect
        # cmake/dyld at stage2 ONLY when CC is stage2's staged clang (its NEEDED
        # libLLVM is stage2's, so the redirect is ABI-coherent). System
        # /usr/bin/clang on the fallback must NOT be steered at stage2's
        # target-stripped libLLVM via LD_LIBRARY_PATH (missing target-init
        # symbols like LLVMInitializeBPFTarget). No profile-runtime LDFLAGS: the
        # pgo build is -fprofile-use (non-instrumented), so no __llvm_profile_*.
        build_pgo_env: dict[str, str] = {"LLVM_PROFILE_FILE": ""}
        if using_staged_cc:
            build_pgo_env.update(_stage_env(staging))
        # BOLT Pass 5 (opt-in) rewrites the *finished* clang/libLLVM, which needs
        # relocations retained at link time — so the binaries that ship from
        # Pass 4a (libLLVM) and 4b (clang) link with -Wl,--emit-relocs. Off
        # unless [bolt] enabled; lib32 (4c) is never BOLTed so it is untouched.
        from sysforge.primitives import bolt as _bolt
        _bolt_ldflag = _bolt.emit_relocs_ldflag() if bolt_relocs else None
        pgo_fps = _build_pass(
            f"PGO optimize · llvm/llvm-libs ({opt})",
            pgo_map,
            options,
            cc=pass3_cc,
            cxx=pass3_cxx,
            install=False,
            compiler_flags_extra=profile_use,
            linker_flags_extra=_bolt_ldflag,
            pgo_build=True,
            pgo_env=build_pgo_env,
            staged_deps=True,
            toolchain_variant="pgo_llvm",
            owner_stage="toolchain",
            reuse=_mk_reuse_ctx("build-pgo", []),
        )

        # 4b's fingerprints feed 4c's Merkle chain; default empty so 4c is safe
        # when non_pgo_map is empty but lib32_map is not.
        nonpgo_fps: dict[str, str] = {}

        # Stage the just-built OPTIMIZED libLLVM (+ headers + cmake configs) so
        # the non-pgo / lib32 sub-passes resolve find_package(LLVM) against the
        # exact libLLVM that ships. staging3 IS the final artifact (full
        # configured targets) — unlike stage2 (training) — so steering clang at
        # it is correct and is the whole point of the split.
        if non_pgo_map or lib32_map:
            _remove_staging(staging3)
            _extract_built_to_staging(pgo_map, staging3, options.dry_run)
            # Fail fast if the split-package 'llvm' (cmake config + headers) did
            # not reach staging3: without LLVMConfig.cmake, the 4b/4c
            # find_package(LLVM) silently falls back to the live /usr libLLVM and
            # the non-pgo suite links against the wrong libLLVM → Gate-3
            # symbol-version brick. Cheaper to catch here than after install.
            if not options.dry_run:
                _assert_staging_has_llvm_cmake(staging3)

        # 4b — build the non-pgo suite (clang, lld, …) against staging3's
        # libLLVM. Set ONLY CMAKE_PREFIX_PATH (mirror Pass 2): the host clang
        # compiles the source, it must not be forced to *load* the staged libLLVM
        # via LD_LIBRARY_PATH.
        if non_pgo_map:
            build_nonpgo_env = {
                "LLVM_PROFILE_FILE": "",
                "CMAKE_PREFIX_PATH": f"{staging3}/usr",
            }
            nonpgo_fps = _build_pass(
                f"PGO optimize · clang/lld/... against shipped libLLVM ({opt})",
                non_pgo_map,
                options,
                cc=pass3_cc,
                cxx=pass3_cxx,
                install=False,
                compiler_flags_extra=profile_use,
                linker_flags_extra=_bolt_ldflag,
                pgo_build=True,
                pgo_env=build_nonpgo_env,
                staged_deps=True,
                toolchain_variant="pgo_llvm",
                owner_stage="toolchain",
                cmake_llvm_dir=f"{staging3}/usr/lib/cmake/llvm",
                reuse=_mk_reuse_ctx("build-nonpgo", sorted(pgo_fps.values())),
            )
            # Verify the split actually held: clang/lld must have linked the
            # staged shipped libLLVM, not the live /usr one. Abort before install
            # (no sentinel, no rollback) if a std::-bound-to-LLVM ref leaked.
            _assert_pass_links_shipped_libllvm(
                non_pgo_map, label="Pass 4b", dry_run=options.dry_run,
            )
            # Stage the new clang/lld so a lib32 sub-pass can resolve them too.
            if lib32_map:
                _extract_built_to_staging(non_pgo_map, staging3, options.dry_run)

        # 4c — lib32 against staging3 (usually empty; lib32 dropped from PGO in
        # d191a89). Same CMAKE_PREFIX_PATH steering.
        if lib32_map:
            pass3c_env = {
                "LLVM_PROFILE_FILE": "",
                "CMAKE_PREFIX_PATH": f"{staging3}/usr",
            }
            _build_pass(
                f"PGO optimize · lib32 against shipped libLLVM ({opt})",
                lib32_map,
                options,
                cc=pass3_cc,
                cxx=pass3_cxx,
                install=False,
                compiler_flags_extra=profile_use,
                pgo_build=True,
                pgo_env=pass3c_env,
                staged_deps=True,
                toolchain_variant="pgo_llvm",
                owner_stage="toolchain",
                cmake_llvm_dir=f"{staging3}/usr/lib/cmake/llvm",
                reuse=_mk_reuse_ctx(
                    "build-lib32", sorted([*pgo_fps.values(), *nonpgo_fps.values()]),
                ),
            )
            _assert_pass_links_shipped_libllvm(
                lib32_map, label="Pass 4c (lib32)", dry_run=options.dry_run,
            )

        # Pass 4 is built but NOT installed here — the caller runs the Gate-2
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

    # Sidecar is written after Pass 3 (above), not here — see comment there.
    # Staging is intentionally NOT removed here: the caller's Gate-3
    # _verify_llvm_install runs after install, and on a verify failure the
    # staging3 prefix is needed by _dump_stage_dynsym_evidence to contrast the
    # libLLVM Pass 4b linked against with the (bricked) installed one. Staging
    # is removed by the caller after Gate 3 passes (or a successful rollback).
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


def _gate_soname_consumers(
    pkgbuild_map: dict[str, Path], all_names: list[str], options, tcfg,
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
    mode = (
        getattr(options, "rebuild_soname_consumers", None)
        or tcfg.get("rebuild_soname_consumers")
        or "prompt"
    )
    pkgvers = _parse_pkgbuild_pkgvers(pkgbuild_map)
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
    if not is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] Building this libLLVM would change its soname and break "
            f"{len(impact.consumers)} installed package(s), and this is a "
            "non-interactive run. Re-run with --rebuild-soname-consumers=auto to "
            "approve the rebuild, or =off to proceed without it."
        )
    choice = prompt_choice(
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


def _rebuild_soname_consumers(consumers: list[str], config, options, state) -> None:
    """Rebuild installed libLLVM consumers after a soname-bumping install.

    Runs OUTSIDE the toolchain sentinel, after Gate 3 passed — a consumer
    rebuild failure must NOT roll back the (intended) toolchain bump. Resolves
    each consumer pkgbase to a build target (``find_pkgbuild`` auto-clones repo
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
            pkgbuild = find_pkgbuild(pkg, config)
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


def _gate2_audit(
    built_map: dict[str, Path], all_names: list[str], options, tcfg,
    *, dry_run: bool,
) -> list[str]:
    """Scan the built ``.pkg.tar*`` for ABI hazards before any install.

    Runs *outside* the install sentinel, between build and install, for BOTH the
    PGO and non-PGO paths (previously only PGO's ``_pgo_install`` scanned). Two
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
    pkgs = _collect_pgo_packages(built_map)
    if not pkgs:
        # Nothing to audit (e.g. AlreadyBuilt with PKGDEST cleared) — the
        # install step will surface a missing-package error of its own.
        return []
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
    return _resolve_abi_consumers_to_rebuild(all_names, options, tcfg)


def _resolve_abi_consumers_to_rebuild(all_names, options, tcfg) -> list[str]:
    """Apply ``rebuild_soname_consumers`` mode to the same-soname ABI-drift case.

    Enumerates the installed libLLVM consumers (``libllvm_abi_consumers`` — the
    reverse-dep walk shared with the soname-bump gate) and, per the mode (CLI >
    toolchain.toml > ``prompt``), returns them for post-Gate-3 rebuild, aborts,
    or proceeds without rebuilding. Mirrors :func:`_gate_soname_consumers`.
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

    mode = (
        getattr(options, "rebuild_soname_consumers", None)
        or tcfg.get("rebuild_soname_consumers")
        or "prompt"
    )
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
    if not is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] Gate 2: the freshly-built libLLVM strands "
            f"{len(consumers)} installed consumer(s) via std:: re-export drift, "
            "and this is a non-interactive run. Nothing was installed. Re-run "
            "with --rebuild-soname-consumers=auto to install + rebuild them, or "
            "=off to install without rebuilding."
        )
    choice = prompt_choice(
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
    staging1, staging, staging3, pgo_store, tcfg, options, snapshot,
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
        _log.ui(f"  staging3:   {staging3}")
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


def _propagate_default_toolchain(compiler: str, options) -> None:
    """Sync ``profiles.toml [defaults] toolchain`` to ``toolchain.toml compiler``.

    Called only on a successful register/build so the package-compiler default
    tracks the toolchain this stage just registered. ``compiler`` is the
    ``toolchain.toml`` value (``"gcc"``/``"llvm"``) — never the build variant
    (stock_llvm/pgo_llvm), which is a different axis. No-op on dry runs;
    tolerant of an unwritable config (warns, does not fail the stage).
    """
    if getattr(options, "dry_run", False):
        return
    try:
        set_default_toolchain(compiler)
    except OSError as exc:
        _log.warn(f"Could not update profiles.toml [defaults] toolchain: {exc}")


# ---------------------------------------------------------------------------
# BOLT Pass 5 (post-link optimization of the just-installed PGO clang)
# ---------------------------------------------------------------------------


def _build_bolt_tools(tcfg: dict, config: dict, options, variant: str | None) -> bool:
    """Pass 5a — build+install the BOLT tools (`llvm-bolt`/`perf2bolt`/…).

    BOLT is EXPERIMENTAL: it is not in the official Arch repos and the stock
    `llvm` package does not build it. sysforge generates an `llvm-bolt` PKGBUILD
    (`bolt.materialize_pkgbuild`, version-locked to the just-installed llvm) and
    builds it standalone against the installed PGO libLLVM — the `bolt/` subtree
    rides inside the same `llvm-project` monorepo tarball the `llvm` build used.

    Returns True if the tools are available afterward (built now, or already
    present). Best-effort: a build failure WARNs and returns False so Pass 5b is
    skipped and the verified PGO toolchain is left intact.
    """
    from sysforge.primitives import bolt as _bolt

    # Already present (e.g. a prior run installed them) — nothing to build.
    if _bolt.tools_available(need_perf=False)[0]:
        return True

    # BLOCKED guard: BOLT's tools all force static LLVM linkage
    # (DISABLE_LLVM_LINK_LLVM_DYLIB), so a standalone build needs the per-component
    # static archives. The PGO toolchain (like stock Arch llvm) is dylib-only and
    # omits them — the build would fail ~100 ninja steps in with an unfindable
    # -lLLVMObject. Fail fast with the reason instead of a cryptic late link error.
    if not _bolt.standalone_build_viable():
        _log.warn(
            "[BOLT] Pass 5 skipped — BLOCKED: the BOLT tools link LLVM statically, but "
            "the PGO toolchain ships a dylib-only libLLVM without the per-component "
            "static archives a standalone llvm-bolt build needs. Building BOLT in-tree "
            "with LLVM is the only viable path (not yet implemented). PGO toolchain left "
            "intact — see DESIGN.md §Toolchain stage (BOLT Pass 5)."
        )
        return False

    pkgbuild_dir = (config.get("paths", {}) or {}).get("pkgbuild_src_dir")
    if not pkgbuild_dir:
        _log.warn(
            "[BOLT] Pass 5a skipped — no [paths] pkgbuild_src_dir to materialize "
            "the llvm-bolt PKGBUILD into; PGO toolchain left as-is."
        )
        return False

    llvm_ver = _query_pacman_versions(("llvm",)).get("llvm")
    if not llvm_ver:
        _log.warn("[BOLT] Pass 5a skipped — installed llvm version not found")
        return False
    pkgver = llvm_ver.split("-", 1)[0]  # strip pkgrel; BOLT locks to llvm pkgver

    try:
        pkgbuild = _bolt.materialize_pkgbuild(Path(pkgbuild_dir), pkgver)
    except OSError as e:
        _log.warn(f"[BOLT] Pass 5a skipped — could not write llvm-bolt PKGBUILD ({e})")
        return False

    _log.ui(
        f"[BOLT] Pass 5a — building the BOLT tools (llvm-bolt {pkgver}, "
        "experimental: no official Arch package) against the installed libLLVM"
    )
    try:
        _build_pkg(
            _bolt.PKG_NAME, pkgbuild, options,
            extra_flags=["--install"],
            toolchain_variant=variant,
            owner_stage="toolchain",
        )
    except Exception as e:
        _log.warn(
            f"[BOLT] Pass 5a failed to build llvm-bolt ({e}) — PGO toolchain "
            "left in place; BOLT optimization skipped."
        )
        return False

    ok, missing = _bolt.tools_available(need_perf=False)
    if not ok:
        _log.warn(
            f"[BOLT] llvm-bolt built but {', '.join(missing)} still not on PATH — "
            "skipping BOLT optimization."
        )
    return ok


def _run_bolt(tcfg: dict, config: dict, options, variant: str | None) -> None:
    """Pass 5 — BOLT-optimize the freshly-installed PGO clang (post-link).

    Runs after Gate 3 has *verified* the PGO toolchain in ``/usr`` and inside the
    stage sentinel, so a mishap is covered by the same snapshot rollback as the
    install. The canonical PGO→BOLT "fast clang" stack, in two steps: **4a**
    builds the BOLT tools sysforge needs (see :func:`_build_bolt_tools` — they are
    not in the Arch repos), then **4b** profiles the installed clang on a
    representative compile job (``perf record``), converts with ``perf2bolt``,
    rewrites with ``llvm-bolt``, smoke-tests the result, and only then atomically
    replaces ``/usr/bin/clang``.

    EXPERIMENTAL and best-effort throughout: a failed tool build, a missing
    ``perf``, a ``perf``/``llvm-bolt`` failure, or a failed smoke test WARNs and
    leaves the verified PGO clang untouched — BOLT is an opt-in extra win, never
    allowed to regress the working toolchain. No-op unless ``[bolt] enabled`` (and
    not dry-run). Note: this rewrites the installed binary post-link, so
    ``pacman -Qkk clang`` will report it modified — an inherent property of
    post-link optimization, not corruption.
    """
    import subprocess as _sp
    import tempfile as _tempfile

    bcfg = _bolt_config(tcfg)
    if not bcfg["enabled"]:
        return
    if options.dry_run:
        _log.ui(
            "[dry-run] would build the BOLT tools (experimental) and "
            "BOLT-optimize the installed clang (Pass 5)"
        )
        return

    from sysforge.primitives import bolt as _bolt
    from sysforge.primitives import fs_provision as _fsp

    # Pass 5a — sysforge builds llvm-bolt/perf2bolt itself (not in Arch repos).
    if not _build_bolt_tools(tcfg, config, options, variant):
        return

    # perf (linux-tools) is needed for collection but isn't something sysforge
    # builds — surface its absence with an actionable hint, don't silently skip.
    ok, missing = _bolt.tools_available(need_perf=True)
    if not ok:
        _log.warn(
            f"[BOLT] Pass 5b skipped — {', '.join(missing)} not on PATH "
            "(install the linux-tools `perf` package). PGO clang left in place."
        )
        return

    clang = Path("/usr/bin/clang")
    clangxx = Path("/usr/bin/clang++")
    if not clang.exists():
        _log.warn("[BOLT] Pass 5 skipped — /usr/bin/clang not found")
        return

    store = _bolt.resolve_store(tcfg)
    try:
        _fsp.ensure_writable_dir(store)
    except _fsp.FsProvisionError as e:
        _log.warn(f"[BOLT] profile store {store} not group-provisioned ({e})")

    with _tempfile.TemporaryDirectory(prefix="sysforge-bolt-") as _td:
        td = Path(_td)
        workload_cfg = bcfg["training_workload"]
        workload = (
            Path(workload_cfg).expanduser()
            if workload_cfg
            else _bolt.write_default_workload(td)
        )
        if not workload.is_file():
            _log.warn(
                f"[BOLT] training_workload {workload} not found — Pass 5 skipped"
            )
            return

        _log.ui(
            "[BOLT] Pass 5 — profiling clang on a compile job and rewriting "
            "with llvm-bolt (PGO→BOLT)"
        )
        try:
            fdata = _bolt.collect_profile(
                clang, store,
                _bolt.compile_workload_argv(str(clangxx), workload, td / "w.o"),
            )
            bolted = _bolt.bolt_binary(clang, fdata, out=td / "clang.bolt")
        except _bolt.BoltError as e:
            _log.warn(f"[BOLT] Pass 5 failed ({e}) — PGO clang left in place")
            return

        # Smoke-test the BOLTed clang *before* it replaces the system compiler.
        smoke = _sp.run(
            [str(bolted), "-std=c++17", "-O2", "-c", str(workload), "-o", str(td / "s.o")],
            capture_output=True, text=True,
        )
        if smoke.returncode != 0 or not (td / "s.o").exists():
            _log.warn(
                "[BOLT] the BOLT-optimized clang failed its smoke test — "
                "discarding it; the verified PGO clang stays in place"
            )
            return

        try:
            _fsp._run_priv(["install", "-Dm755", str(bolted), str(clang)])
        except _fsp.FsProvisionError as e:
            _log.warn(
                f"[BOLT] could not install the BOLTed clang ({e}) — "
                "PGO clang left in place"
            )
            return

        # Provenance sidecar (the build_state entry stays the PGO record; this
        # marks that a post-link BOLT pass was applied on top).
        try:
            (store / "applied.txt").write_text(
                f"bolt_llvm applied to {clang}\n", encoding="utf-8"
            )
        except OSError:
            pass
        _log.ui(
            "[BOLT] Pass 5 complete — /usr/bin/clang is now PGO+BOLT optimized "
            f"(build_mode {_bolt.BUILD_MODE})"
        )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class ToolchainStage(Stage):
    name = "toolchain"
    description = "LLVM/GCC toolchain build"
    depends_on = ["reconfigure"]
    makepkg_bearing = True

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
        staging3 = Path(tcfg.get("pgo_staging3", _DEFAULT_STAGING_3))
        pgo_store = resolve_pgo_store(tcfg)

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
            _propagate_default_toolchain(compiler, options)
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
            _propagate_default_toolchain(compiler, options)
            return

        # Repo-install path (LLVM, PGO off): when packages.toml [build]
        # repo_mode is "pacman", honor the user's package-sourcing preference
        # and pull the stock LLVM suite from the repos instead of compiling it.
        # PGO is deliberately excluded — a profiled toolchain is the point of
        # enabling PGO and has no repo artifact, so PGO always builds from
        # source regardless of repo_mode.
        if not pgo and _resolve_packages_repo_mode(config) == REPO_MODE_PACMAN:
            pgo_pkgs, non_pgo_pkgs, lib32_pkgs = _package_lists(tcfg)
            suite = pgo_pkgs + non_pgo_pkgs + lib32_pkgs
            cc, cxx, ld = _compiler_paths(compiler)
            _log.ui(
                f"repo_mode=pacman, PGO off — installing {len(suite)} LLVM "
                f"package(s) from repo (no build): {' '.join(suite)}",
            )
            if options.dry_run:
                _log.ui(
                    f"[dry-run] would install from repo: {' '.join(suite)}"
                )
            else:
                # Install is the mutation window — wrap in the sentinel so an
                # interrupted/failed install blocks the next run with a recovery
                # command.
                with sentinel_scope(
                    options.state_dir,
                    "toolchain",
                    recovery_cmd="sudo pacman -S " + " ".join(suite),
                    retry_cmd="sysforge run toolchain",
                    compiler=compiler,
                    pgo=pgo,
                ):
                    install_repo_pkgs(suite)
            result = {"cc": cc, "cxx": cxx, "variant": "stock_llvm"}
            if ld is not None:
                result["ld"] = ld
            state.set_stage_result("toolchain", result)
            try:
                state.save()
            except PermissionError:
                _log.warn(
                    "Cannot write state — toolchain results will not be checkpointed",
                )
            _propagate_default_toolchain(compiler, options)
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

        # Training-corpus extras (e.g. mesa): compiled by the instrumented
        # stage1 clang in Pass 3 purely to enrich clang.profdata with non-LLVM
        # (graphics/C-heavy) codegen; never installed. Resolved through the same
        # scheduler-synced PKGBUILD path as the toolchain packages. Best-effort
        # — a resolution miss degrades to an LLVM-only corpus with a warning,
        # never blocks the toolchain build. PGO path only (a non-PGO single-pass
        # build generates no profraw, so the corpus is meaningless there).
        corpus_map: dict[str, Path] = {}
        corpus_extras = _resolve_training_corpus(tcfg) if pgo else []
        if corpus_extras:
            try:
                corpus_resolved = _resolve_all_pkgbuilds(
                    corpus_extras, config,
                    update=not options.no_update,
                    cleansrc=getattr(options, "cleansrc", False),
                    cleansrc_force=getattr(options, "cleansrc_force", False),
                )
                corpus_map = {n: corpus_resolved[n] for n in corpus_extras}
                _log.ui(
                    f"[PGO] Training-corpus extras: {', '.join(corpus_extras)} "
                    "(compiled in Pass 3 for profile enrichment; not installed)",
                )
            except RuntimeError as e:
                _log.warn(
                    f"[PGO] Could not resolve training-corpus extras "
                    f"{corpus_extras}: {e} — proceeding with LLVM-only corpus",
                )

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
            staging1=staging1, staging=staging, staging3=staging3, pgo_store=pgo_store,
            tcfg=tcfg, options=options, snapshot=snapshot,
        )

        # Gate 1 — cheap pre-build preflight. Hard-fails (overridable) on
        # definite-failure conditions BEFORE any build time is spent; dry-run
        # downgrades bricks to warnings. Runs for both PGO and non-PGO.
        _gate1_preflight(
            lib32_pkgs, staging1, staging, pgo_store,
            pkgbuild_map, options, tcfg, snapshot=snapshot,
        )

        # Pre-build soname gate: if the about-to-be-built libLLVM bumps the
        # soname, warn + list the installed consumers that would break and
        # (per rebuild_soname_consumers mode) require approval up front. The
        # captured consumers are rebuilt after Gate 3 so there is no
        # post-install shock. Returns [] when nothing changes / dry-run / off.
        soname_consumers = _gate_soname_consumers(
            pkgbuild_map, all_names, options, tcfg,
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
        # PGO /var/tmp staging dirs + pgo_store cache (and the non-PGO build area) so
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
                # Opt-in input-fingerprint reuse (Pass 4): CLI --reuse-built >
                # toolchain.toml reuse_unchanged > off. config_digest folds the
                # flag-relevant config (profiles/rules) + the toolchain settings
                # (e.g. [llvm] targets, which drive the LLVM_TARGETS_TO_BUILD
                # cmake patch the upstream-PKGBUILD hash can't see) so a config
                # edit between runs invalidates the cache.
                reuse_built = bool(getattr(options, "reuse_built", False)) or bool(
                    tcfg.get("reuse_unchanged", False)
                )
                config_digest = build_fingerprint.hash_obj({
                    "profiles": config.get("profiles"),
                    "rules": config.get("rules"),
                    "toolchain": tcfg,
                })
                built_map, cc, cxx, ld, variant = _build_llvm_pgo_inner(
                    pgo_map, non_pgo_map, lib32_map,
                    staging1, staging, staging3, pgo_store, options,
                    config_digest=config_digest,
                    reuse_built=reuse_built,
                    corpus_map=corpus_map,
                    # BOLT Pass 5 (opt-in) needs the shipped clang/libLLVM linked
                    # with -Wl,--emit-relocs so llvm-bolt can rewrite them.
                    bolt_relocs=_bolt_config(tcfg)["enabled"],
                )
            else:
                built_map, cc, cxx, ld, variant = _build_llvm_single(
                    pgo_map, non_pgo_map, lib32_map, options
                )

            # Gate 2 — pre-install ABI-hazard audit (both paths), OUTSIDE the
            # sentinel: an unhealable brick aborts here leaving nothing installed
            # and no sentinel, keeping the live toolchain intact. A healable
            # std:: re-export drift returns the libLLVM consumers to rebuild
            # after Gate 3 (same machinery as a soname bump).
            abi_consumers = _gate2_audit(
                built_map, all_names, options, tcfg, dry_run=options.dry_run,
            )

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
                #
                # expected_targets is the *actually resolved* LLVM_TARGETS_TO_BUILD
                # the build patched in (resolve_or_detect_llvm_targets — the same
                # value makepkg_wrapper used), NOT just toolchain.toml [llvm]
                # targets. On an autodetect host that key is unset, so the old
                # `tcfg[...]` form resolved to None and skipped check #3 entirely —
                # the gap that let a target-reduced libLLVM install unverified.
                from sysforge.pipeline.state import resolve_state_dir
                from sysforge.primitives.llvm_targets import (
                    resolve_or_detect_llvm_targets,
                )
                # Resolve the state dir the same way the build's patcher did
                # (makepkg_wrapper._maybe_patch_llvm_targets → resolve_state_dir),
                # so Gate 3 reads the same hardware_profile.toml. options.state_dir
                # may be None (env/default fallback); resolve_state_dir handles it.
                _gate3_state_dir, _ = resolve_state_dir(options.state_dir)
                hw_profile = _gate3_state_dir / "hardware_profile.toml"
                expected_targets = resolve_or_detect_llvm_targets(
                    TOOLCHAIN_PATH, hw_profile,
                )
                if not options.dry_run:
                    issues = list(
                        _verify_llvm_install(expected_targets=expected_targets)
                    )
                    # Graphics-consumer sufficiency vs the NOW-INSTALLED libLLVM.
                    # An *unhealable* dropped backend an installed mesa consumer
                    # imports is brick-class — folded into `issues` so the same
                    # snapshot auto-restore fires while rollback is still armed
                    # (inside the sentinel). *Healable* std:: re-export misses are
                    # EXPECTED here (mesa is not rebuilt until after Gate 3): they
                    # must NOT trip rollback, or we would revert the very libLLVM
                    # the post-Gate-3 consumer rebuild is about to make coherent.
                    for f in toolchain_safety.check_installed_consumer_symbols():
                        if f.healable:
                            _log.ui(
                                "Gate 3: std:: re-export drift (healable by the "
                                f"queued consumer rebuild): {f.message}"
                            )
                        else:
                            issues.append(f.message)
                    if issues:
                        evidence_path = (
                            _dump_stage_dynsym_evidence(staging3, state.path.parent)
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
                    # Verify passed — safe to wipe the stage2/stage3 prefixes
                    # (PGO only). stage3 held the optimized libLLVM that the
                    # non-pgo sub-pass linked against; it is no longer needed.
                    if pgo:
                        _remove_staging(staging)
                        _remove_staging(staging3)
                        # Pass 5 — BOLT the verified PGO clang (opt-in, gated on
                        # [bolt] enabled). 4a builds the BOLT tools (not in the
                        # Arch repos), 4b rewrites clang. Best-effort and
                        # smoke-tested before it replaces /usr/bin/clang; runs
                        # inside the sentinel so a mishap stays covered by the
                        # snapshot rollback.
                        _run_bolt(tcfg, config, options, variant)

        # Toolchain is installed and Gate-3-verified. Rebuild the libLLVM
        # consumers so the live system is left coherent: the pre-build soname
        # gate's set (re-link the new soname) merged with Gate 2's same-soname
        # std:: re-export drift set (re-link std:: to libstdc++). OUTSIDE the
        # sentinel — a consumer rebuild failure must not roll back the intended
        # toolchain bump; it surfaces as an actionable error instead.
        rebuild_consumers = sorted(set(soname_consumers) | set(abi_consumers))
        if rebuild_consumers and not options.dry_run:
            _rebuild_soname_consumers(rebuild_consumers, config, options, state)

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

        # Installed + Gate-3-verified: sync the profile default to the configured
        # compiler (outside the sentinel, on success only — a failed build above
        # returns/raises before here, so the default never flips to an
        # uninstalled llvm).
        _propagate_default_toolchain(compiler, options)

        _log.ui(
            f"Toolchain stage complete. cc={cc}  cxx={cxx}"
            + (f"  ld={ld}" if ld else ""),
        )
