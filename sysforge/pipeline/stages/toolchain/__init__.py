# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/toolchain/ — stage 6: LLVM toolchain build (GCC is register-only)

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
            are extracted to pgo_staging1 (stage1) by pgo_stage_instrumented so the
            live /usr is never touched.  Both packages stage — including the
            cmake-config / static-lib `llvm` package — so Pass 2's
            find_package(LLVM) sees the staged headers + configs.  The
            instrumented .a archives staged alongside surface __llvm_profile_*
            link errors for anything that consumes LLVM component targets;
            Pass 2 and Pass 3 work around that via profile_runtime_ldflag()
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
            daemon merges profraw every PGO_MERGE_INTERVAL seconds with
            adaptive batch sizing (PROFRAW_MERGE_BATCH_MAX →
            PROFRAW_MERGE_BATCH_MIN on OOM).  llvm-profdata invoked with
            RLIMIT_AS lifted (lift_for_child) so it is not constrained by
            the sysforge controller's 2 GiB cap.  No system install; Pass 3
            binaries extracted to pgo_staging (stage2).  Merged profdata size
            logged at [INFO]; warns if below PGO_PROFDATA_MIN_BYTES (likely
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
            pgo_install(); staging prefixes removed on success.  Profdata
            preserved with a version sidecar (clang.profdata.version) for
            reuse by sysforge update.

  A sudo keepalive thread (primitives/sudo_session.keepalive) refreshes
  credentials every SUDO_KEEPALIVE_INTERVAL seconds throughout all four passes.

Compiler propagation:
  On completion writes cc/cxx/ld to pipeline_state.toml [stages.toolchain.result]

Module layout (3.2.0-F2)
------------------------
This was one 4263-line module with four classes and a 701-line function. It is
now a package whose files follow the seams the original already had as comment
banners:

  constants.py  defaults and tuning values; no imports, no logic
  config.py     toolchain.toml -> ToolchainConfig, parsed once at stage entry
  pkgbuilds.py  resolving package names to PKGBUILDs, and syncing those trees
  reuse.py      Pass-4 input-fingerprint reuse (opt-in)
  passes.py     building one package (build_pkg) and one pass (build_pass)
  profdata.py   profile generation, merging, staging, and the version sidecar
  verify.py     post-install evidence that the installed clang actually links
  pgo.py        the four-pass sequence itself
  gates.py      Gate 1 / Gate 2, soname consumers, snapshot + rollback
  identity.py   which toolchain is active; the register-only GCC path
  bolt.py       Pass 5, optional post-link optimization
  stage.py      ToolchainStage: sequencing only, delegating every step

Dependencies run one way (constants <- config <- ... <- stage); ``stage.py`` is
the only module that imports most of the others. Modules call each other
module-qualified (``profdata.pgo_install(...)``, not a bare imported name), so a
patch on the owning module is seen by every caller.

Names that cross a module boundary are public: an underscore is a promise about
one module's internals, and it stopped being true the moment another module
needed the name. This package's import path is unchanged, and the names below
are re-exported here for callers that used the flat module.
"""

from sysforge.pipeline.stages.toolchain import (  # noqa: F401
    constants,
    config,
    pkgbuilds,
    reuse,
    passes,
    profdata,
    verify,
    pgo,
    gates,
    identity,
    bolt,
    stage,
)
from sysforge.pipeline.stages.toolchain.constants import (  # noqa: F401
    DEFAULT_LLVM_LIB32,
    DEFAULT_LLVM_NON_PGO,
    DEFAULT_LLVM_PGO,
    DEFAULT_STAGING,
    DEFAULT_STAGING_1,
    DEFAULT_STAGING_3,
    PGO_ALLOWED_MAKEPKG_FLAGS,
    PGO_MERGE_INTERVAL,
    PGO_PROFDATA_MIN_BYTES,
    PROFRAW_MERGE_BATCH_MAX,
    PROFRAW_MERGE_BATCH_MIN,
    PROFRAW_SETTLE_SECS,
)
from sysforge.pipeline.stages.toolchain.config import (  # noqa: F401
    bolt_config,
    load_toolchain_config,
    package_lists,
    resolve_packages_repo_mode,
    resolve_training_corpus,
)
from sysforge.pipeline.stages.toolchain.pkgbuilds import (  # noqa: F401
    confirm_or_abort,
    resolve_all_pkgbuilds,
    run_llvm_preflight,
    show_resolution_table,
    show_version_changes,
    sync_pkgbuild_dirs,
)
from sysforge.pipeline.stages.toolchain.reuse import (  # noqa: F401
    ReuseCtx,
    pkg_fingerprint,
    reuse_cache_path,
)
from sysforge.pipeline.stages.toolchain.passes import (  # noqa: F401
    build_pass,
    build_pkg,
)
from sysforge.pipeline.stages.toolchain.profdata import (  # noqa: F401
    assert_pass_links_shipped_libllvm,
    assert_staging_has_llvm_cmake,
    check_existing_profdata,
    collect_pgo_packages,
    do_profraw_merge,
    extract_built_to_staging,
    extract_pkg_to_staging,
    merge_profraw,
    pgo_install,
    pgo_stage_instrumented,
    pgo_target_major,
    profile_runtime_ldflag,
    profraw_merge_daemon,
    remove_staging,
    resolve_skip_build_variant,
    stage_env,
    system_llvm_is_instrumented,
    validate_pgo_environment,
    write_profdata_version,
)
from sysforge.pipeline.stages.toolchain.verify import (  # noqa: F401
    check_llvm_link_resolution,
    dump_stage_dynsym_evidence,
    llvm_recovery_command,
    query_pacman_versions,
    verify_llvm_install,
)
from sysforge.pipeline.stages.toolchain.pgo import (  # noqa: F401
    PGOAborted,
    build_llvm_pgo_inner,
    build_llvm_single,
    pgo_confirm,
    pgo_lock,
    pgo_lock_path,
)
from sysforge.pipeline.stages.toolchain.gates import (  # noqa: F401
    gate1_preflight,
    gate2_audit,
    gate_soname_consumers,
    install_bootstrap_compilers,
    log_toolchain_resolution_summary,
    parse_pkgbuild_pkgvers,
    rebuild_soname_consumers,
    resolve_abi_consumers_to_rebuild,
    rollback_to_snapshot,
    snapshot_recovery_cmd,
    snapshot_suite,
)
from sysforge.pipeline.stages.toolchain.identity import (  # noqa: F401
    ToolchainIdentity,
    compiler_paths,
    probe_toolchain_identity,
    propagate_default_toolchain,
    toolchain_identity_lines,
)
from sysforge.pipeline.stages.toolchain.bolt import (  # noqa: F401
    build_bolt_tools,
    run_bolt,
)
from sysforge.pipeline.stages.toolchain.stage import (  # noqa: F401
    ToolchainStage,
)
from sysforge.primitives.paths import TOOLCHAIN_PATH  # noqa: F401

__all__ = [
    "DEFAULT_LLVM_LIB32",
    "DEFAULT_LLVM_NON_PGO",
    "DEFAULT_LLVM_PGO",
    "DEFAULT_STAGING",
    "DEFAULT_STAGING_1",
    "DEFAULT_STAGING_3",
    "PGOAborted",
    "PGO_ALLOWED_MAKEPKG_FLAGS",
    "PGO_MERGE_INTERVAL",
    "PGO_PROFDATA_MIN_BYTES",
    "PROFRAW_MERGE_BATCH_MAX",
    "PROFRAW_MERGE_BATCH_MIN",
    "PROFRAW_SETTLE_SECS",
    "ReuseCtx",
    "ToolchainIdentity",
    "ToolchainStage",
    "assert_pass_links_shipped_libllvm",
    "assert_staging_has_llvm_cmake",
    "bolt_config",
    "build_bolt_tools",
    "build_llvm_pgo_inner",
    "build_llvm_single",
    "build_pass",
    "build_pkg",
    "check_existing_profdata",
    "check_llvm_link_resolution",
    "collect_pgo_packages",
    "compiler_paths",
    "confirm_or_abort",
    "do_profraw_merge",
    "dump_stage_dynsym_evidence",
    "extract_built_to_staging",
    "extract_pkg_to_staging",
    "gate1_preflight",
    "gate2_audit",
    "gate_soname_consumers",
    "install_bootstrap_compilers",
    "llvm_recovery_command",
    "load_toolchain_config",
    "log_toolchain_resolution_summary",
    "merge_profraw",
    "package_lists",
    "parse_pkgbuild_pkgvers",
    "pgo_confirm",
    "pgo_install",
    "pgo_lock",
    "pgo_lock_path",
    "pgo_stage_instrumented",
    "pgo_target_major",
    "pkg_fingerprint",
    "probe_toolchain_identity",
    "profile_runtime_ldflag",
    "profraw_merge_daemon",
    "propagate_default_toolchain",
    "query_pacman_versions",
    "rebuild_soname_consumers",
    "remove_staging",
    "resolve_abi_consumers_to_rebuild",
    "resolve_all_pkgbuilds",
    "resolve_packages_repo_mode",
    "resolve_skip_build_variant",
    "resolve_training_corpus",
    "reuse_cache_path",
    "rollback_to_snapshot",
    "run_bolt",
    "run_llvm_preflight",
    "show_resolution_table",
    "show_version_changes",
    "snapshot_recovery_cmd",
    "snapshot_suite",
    "stage_env",
    "sync_pkgbuild_dirs",
    "system_llvm_is_instrumented",
    "toolchain_identity_lines",
    "validate_pgo_environment",
    "verify_llvm_install",
    "write_profdata_version",
    "TOOLCHAIN_PATH",
    "constants",
    "config",
    "pkgbuilds",
    "reuse",
    "passes",
    "profdata",
    "verify",
    "pgo",
    "gates",
    "identity",
    "bolt",
    "stage",
]
