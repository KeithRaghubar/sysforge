# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/constants.py — defaults and tuning constants for the LLVM build.

Pure values, no imports, no logic — which is why they are their own module:
almost every other module in the package reads some of them, and any module
that also *did* something would become an import-cycle hub.

The staging prefixes are stable paths under /var/tmp rather than temp dirs, on
purpose: the four PGO passes have to find each other's output across separate
makepkg invocations, and cross-stage ABI coherence depends on stage N+1 linking
against exactly the artifacts stage N left behind.
"""

from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_LLVM_PGO = ["llvm", "llvm-libs"]
DEFAULT_LLVM_NON_PGO = ["clang", "lld", "polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
# The canonical lib32 LLVM suite — lib32-llvm, lib32-llvm-libs, lib32-clang,
# lib32-spirv-llvm-translator — is documented here for opt-in reference but NOT
# built by the toolchain stage by default (see DEFAULT_LLVM_LIB32 below). A user
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
DEFAULT_LLVM_LIB32: list[str] = []
DEFAULT_STAGING_1 = "/var/tmp/sysforge-llvm-stage1"  # noqa: S108 — stable multi-stage LLVM build path (cross-stage ABI coherence), not a temp file
DEFAULT_STAGING = "/var/tmp/sysforge-llvm-stage2"  # noqa: S108 — stable multi-stage LLVM build path (cross-stage ABI coherence), not a temp file
# Pass-4 staging prefix. Holds the *final optimized* libLLVM (+ headers + cmake
# configs) so the non-pgo suite (clang, lld, …) links against the exact libLLVM
# that ships — guaranteeing ABI coherence. Distinct from stage2 (the Pass-3
# training binaries) so the two never conflate. See build_llvm_pgo_inner Pass 4.
DEFAULT_STAGING_3 = "/var/tmp/sysforge-llvm-stage3"  # noqa: S108 — stable multi-stage LLVM build path (cross-stage ABI coherence), not a temp file
# pgo_store resolution (toolchain.toml > SYSFORGE_PGO_STORE > /var/cache default)
# lives in primitives.makepkg_pgo.resolve_pgo_store — the one home shared with
# the reader (_resolve_pgo_state) and the wrapper's orphan-profraw guard.

# Makepkg flags permitted through to PGO builds from user -m input.
# Only force-rebuild is safe; flags that alter build flow (e.g. --noextract,
# --nobuild, --noprepare) would corrupt the instrumentation/use sequence.
PGO_ALLOWED_MAKEPKG_FLAGS = {"-f", "--force"}

# Interval (seconds) between intermediate profraw merges during Pass 3.
PGO_MERGE_INTERVAL = 15

# Adaptive batch sizing for llvm-profdata merge. Each invocation starts at
# PROFRAW_MERGE_BATCH_MAX files; on failure the batch is halved and retried
# at the same position. Shrinkage persists for the remainder of that merge
# call (next daemon wakeup resets to max). Gives up when batch_size falls
# below PROFRAW_MERGE_BATCH_MIN and logs a warning.
PROFRAW_MERGE_BATCH_MAX = 128
PROFRAW_MERGE_BATCH_MIN = 8

# Profraw files modified more recently than this (seconds) are skipped during
# merges — they may still be actively written by a clang process, and merging
# a partial write causes SIGBUS in llvm-profdata.
PROFRAW_SETTLE_SECS = 10

# Minimum expected profdata size after a real Pass 3 training run (bytes).
# A genuine LLVM self-compilation produces hundreds of MiB of profile data.
# Warn if the merged profdata is smaller — likely indicates compilation was
# bypassed (e.g. by a cache tool that slipped past CCACHE/SCCACHE_DISABLE).
PGO_PROFDATA_MIN_BYTES = 10 * 1024 * 1024  # 10 MiB
