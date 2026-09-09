# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/pgo.py — the four-pass PGO sequence.

The narrative half of the PGO build: what runs, in what order, against which
staging prefix, and what happens when a pass fails. Pass 1 builds instrumented
llvm/llvm-libs into stage1; Pass 2 builds the non-PGO suite against stage1 so
the two are ABI-coherent; Pass 3 is the training run whose side effect is
profraw; Pass 4 rebuilds everything with -fprofile-use. The module docstring of
``sysforge.pipeline.stages.toolchain`` has the full account.

The mechanisms these passes call live in ``profdata.py`` and ``passes.py``.
``PGOAborted`` is the module's own control-flow signal: the PGO sub-flow is
fragile enough that a user-declined confirmation must unwind cleanly rather
than leave staging prefixes behind.
"""
from pathlib import Path
import contextlib
import threading

from sysforge.primitives import build_fingerprint
from sysforge.primitives import fs_provision
from sysforge.primitives import sudo_session
from sysforge.primitives.build_lock import build_lock

from sysforge.pipeline.stages.toolchain import constants, passes, profdata, reuse

from sysforge.primitives import prompt
from sysforge.primitives import pacman
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Confirmation gate for the fragile PGO sub-flow
# ---------------------------------------------------------------------------


class PGOAborted(RuntimeError):
    """User declined a PGO confirmation prompt (or non-TTY without --auto-pgo)."""


def pgo_confirm(
    msg: str,
    *,
    default: str,
    eof_default: str,
    options,
    abort_msg: str,
) -> bool:
    """Confirmation gate for fragile PGO decision points.

    Returns True if the user (or `--auto-pgo`) approves; raises ``PGOAborted``
    otherwise. Behaviour matrix:

      • ``--auto-pgo`` set        → no prompt; treat as approved.
      • TTY, no ``--auto-pgo``    → prompt; empty input → ``default``.
      • Non-TTY, no ``--auto-pgo``→ ``prompt.prompt_choice`` returns ``eof_default``
        (we set this to ``"n"`` everywhere, so the PGO path aborts cleanly with
        a message directing the user to pass ``--auto-pgo``).

    PGO fragility (silent mis-optimisation if profdata is wrong) is why we
    deliberately diverge from the rest of sysforge's automation-by-default
    posture here.
    """
    if getattr(options, "auto_pgo", False):
        return True
    if not prompt.is_interactive():
        raise PGOAborted(
            f"{abort_msg} (non-interactive PGO requires --auto-pgo)"
        )
    answer = prompt.prompt_choice(
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
    raise PGOAborted(abort_msg)

# ---------------------------------------------------------------------------
# Concurrent-build lock
# ---------------------------------------------------------------------------


def pgo_lock(lock_path: Path):
    """Advisory flock guard for the duration of a PGO toolchain build.

    Two concurrent ``sysforge run toolchain`` invocations would clobber
    ``/var/tmp/sysforge-llvm-stage1``, ``/var/tmp/sysforge-llvm-stage2`` and
    the shared ``pgo_store`` profraw directory.  The sentinel scope guards
    the state-dir but not these /var/tmp + ~/pgo paths, so we add an
    explicit advisory lock here.  Delegates to the shared ``build_lock``
    primitive (the kernel stage uses the same one) — don't roll a second
    flock path.
    """
    return build_lock(lock_path, label="PGO", noun="build")

# ---------------------------------------------------------------------------
# Build paths
# ---------------------------------------------------------------------------


def build_llvm_single(
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
    passes.build_pass(
        "LLVM build (single pass, no PGO)",
        all_pkgs,
        options,
        install=False,
        toolchain_variant="stock_llvm",
        owner_stage="toolchain",
    )
    return all_pkgs, "/usr/bin/clang", "/usr/bin/clang++", "lld", "stock_llvm"


def pgo_lock_path(staging1: Path) -> Path:
    """Lock-file path guarding the PGO staging dirs + pgo_store.

    Lives in the parent of staging1 (typically ``/var/tmp``) so neither the
    Pass-1 purge nor the post-build cleanup can delete it. The stage acquires
    this (via :func:`pgo_lock`) around the whole build → audit → install
    window, mirroring how the kernel stage wraps its build with
    ``kernel-build.lock``.
    """
    return staging1.parent / "sysforge-pgo.lock"


def build_llvm_pgo_inner(
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

    profdata.validate_pgo_environment(options.dry_run)

    # Check for existing compatible profdata before purging.
    # If profdata from a previous run matches the target LLVM major version,
    # skip passes 1-3 and go straight to Pass 4 (the optimized build).
    # --rebuild-profdata forces a full 4-pass build regardless.
    skip_profgen = False
    profdata_path = pgo_store / "clang.profdata"
    if not options.rebuild_profdata and not options.dry_run:
        pgo_state, pgo_info = profdata.check_existing_profdata(pgo_store, pgo_map)
        if pgo_state == "ready":
            # Prompt #1 — profdata reuse decision. Default yes (the common,
            # cheap path); declining drops into prompts #2 and #3 below.
            try:
                pgo_confirm(
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
            except PGOAborted:
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
            f"[PGO] Skipping passes 1-3 (instrument/bootstrap/train), "
            f"building with existing profdata  "
            f"({n_total} package(s) across "
            f"{len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))} PKGBUILD(s))  "
            f"pgo_store={pgo_store}",
        )

    if not skip_profgen and not options.dry_run:
        import shutil as _shutil

        # Prompt #2 — purge staging/pgo_store. rmtree is silently destructive
        # of partial Pass-1/Pass-4 staging from a prior failed run, so gate it.
        if staging1.exists() or staging.exists() or staging3.exists() or pgo_store.exists():
            pgo_confirm(
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
        pgo_confirm(
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

    # Sudo keepalive for the build sequence. pgo_install() calls sudo
    # directly from sysforge, so the keepalive's `sudo -v` refreshes the correct
    # timestamp entry (same parent PID) for all passes.
    if not options.dry_run:
        sudo_session.authenticate()
    sudo_stack = contextlib.ExitStack()
    sudo_stack.enter_context(
        sudo_session.keepalive(tag="PGO", enabled=not options.dry_run)
    )

    try:
        residual_linker_flags: str | None = None

        if not skip_profgen:
            # Pass 1 — build pgo packages with the live system clang +
            # -fprofile-generate. cc MUST be pinned to clang explicitly: the
            # resolved profile is `[profiles.standard]`, shipped as CC=gcc, and
            # cc=None would let that gcc win. clang's -fprofile-generate emits
            # LLVM-format .profraw and __llvm_profile_* refs in the staged .a
            # archives (resolved by Pass 2's profdata.profile_runtime_ldflag); gcc's
            # would emit gcov __gcov_* refs that the clang profile runtime
            # cannot satisfy, bricking the Pass 2 link. Mirrors Pass 2's
            # explicit cc="/usr/bin/clang" (this branch only runs for
            # compiler="llvm"; the gcc path returns early before any PGO pass).
            # makepkg runs WITHOUT --install; outputs are extracted into stage1
            # by profdata.pgo_stage_instrumented so the live root is never touched.
            passes.build_pass(
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
            profdata.pgo_stage_instrumented(pgo_map, staging1, options.dry_run)
            _log.info("[PGO] 1/4 complete (staged to "
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
                    with contextlib.suppress(OSError):
                        f.unlink()
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
            # link errors; profdata.profile_runtime_ldflag() force-loads the clang
            # profile runtime to satisfy them. toolchain_variant="pgo_llvm"
            # selects lld via the [VARIANT_LD] guard — without it this pass
            # falls back to the CC=gcc profile's bfd, whose strict left-to-right
            # archive resolution drops the profile runtime before the
            # instrumented archives reference it (the historical Pass 2
            # failure). lld resolves it regardless of order; the --whole-archive
            # form of profdata.profile_runtime_ldflag() makes it order-proof either way.
            bootstrap_env = {
                "CMAKE_PREFIX_PATH": f"{staging1}/usr",
            }
            residual_linker_flags = profdata.profile_runtime_ldflag()
            if residual_linker_flags is not None:
                _log.info(
                    "[PGO] Pass 2: injecting clang profile runtime into LDFLAGS "
                    "to resolve __llvm_profile_* in stage1's instrumented .a archives "
                    f"({residual_linker_flags})",
                )
            passes.build_pass(
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
            profdata.extract_built_to_staging(non_pgo_map, staging1, options.dry_run)
            _log.info(f"[PGO] 2/4 complete (stage1 self-sufficient at {staging1})")

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
                **profdata.stage_env(staging1),
            }
            _log.info(
                f"[PGO] Pass 3: LLVM_PROFILE_FILE={train_env['LLVM_PROFILE_FILE']}",
            )

            stop_event = threading.Event()
            monitor = threading.Thread(
                target=profdata.profraw_merge_daemon,
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
                passes.build_pass(
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
                # still running here, and the final profdata.merge_profraw sweep below
                # picks up whatever this adds. staged_deps=True keeps the
                # no-pacman-mutation invariant (--nodeps, no --syncdeps), so the
                # extras' makedepends must already be installed. Best-effort: a
                # corpus build failure (missing makedep, mesa configure quirk)
                # is logged and the PGO run proceeds with the LLVM-only profile
                # — enrichment must never brick the toolchain build.
                if corpus_map:
                    try:
                        passes.build_pass(
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
            _log.info("[PGO] 3/4 complete")

            # Final sweep: merge any profraw not yet handled by the daemon
            profdata_path = profdata.merge_profraw(pgo_store, options.dry_run)
            if not options.dry_run:
                profdata_size = profdata_path.stat().st_size
                _log.info(
                    f"[PGO] Merged profdata size: {profdata_size // (1024 * 1024)} MiB",
                )
                if profdata_size < constants.PGO_PROFDATA_MIN_BYTES:
                    _log.warn(
                        f"[PGO] Profdata is unexpectedly small "
                        f"({profdata_size // (1024 * 1024)} MiB < "
                        f"{constants.PGO_PROFDATA_MIN_BYTES // (1024 * 1024)} MiB). "
                        "Pass 3 may not have exercised enough code paths — "
                        "check that CCACHE_DISABLE/SCCACHE_DISABLE took effect "
                        "and compilation actually ran.",
                    )
                    # Prompt #4 — abort before Pass 4 unless the user explicitly
                    # accepts the suspicious profdata. Wrong profdata silently
                    # mis-optimises the resulting compiler, so default to no.
                    pgo_confirm(
                        f"Pass 3 profdata is suspiciously small "
                        f"({profdata_size // (1024 * 1024)} MiB) — "
                        "instrumentation may have been bypassed. "
                        "Continue to Pass 4 with this profdata? [y/N]:",
                        default="n",
                        eof_default="n",
                        options=options,
                        abort_msg="user declined Pass 4 due to suspicious profdata",
                    )
            _log.info(f"[PGO] Profile data ready: {profdata_path}")
            # Write the sidecar now (right after Pass 3 has produced the
            # profdata, before Pass 4 starts) so an aborted Pass 4 still
            # leaves recoverable profdata that the next run can reuse via
            # profdata.check_existing_profdata.  The sidecar's only invariant is
            # "this profdata is for LLVM major N", determined entirely by
            # what Pass 3 instrumented — Pass 4 success has no bearing on it.
            if not options.dry_run:
                profdata.write_profdata_version(pgo_store, pgo_map)
            profdata.extract_built_to_staging(pgo_map, staging, options.dry_run)

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

        # Input-fingerprint reuse setup (Pass 4 only). The cache lives *outside*
        # pgo_store (a sibling dir; see reuse_cache_file) so a fresh 4-pass
        # start's pgo_store purge doesn't wipe it — a prior run's records survive
        # for the resume to consult. The profdata is hashed once (constant across
        # 4a/4b/4c). The cache is always *written*; it is only *consulted*
        # (skipping rebuilds) when ``reuse_built`` is opted in.
        reuse_cache: dict = {}
        reuse_cache_file = reuse.reuse_cache_path(pgo_store)
        reuse_profdata_sha: str | None = None
        reuse_pkgdest: Path | None = None
        if not options.dry_run:
            fs_provision.ensure_writable_dir(
                reuse_cache_file.parent, dry_run=options.dry_run
            )
            reuse_cache = build_fingerprint.load_cache(reuse_cache_file)
            reuse_profdata_sha = build_fingerprint.hash_file(profdata_path)
            reuse_pkgdest = pacman.get_pkgdest()
            if reuse_built:
                _log.ui(
                    "[PGO] --reuse-built: unchanged Pass-4 packages will be "
                    f"reused from cache at {reuse_cache_file}",
                )

        # The full toolchain build-set (all pgo/non_pgo/lib32 package names).
        # Every member is built and staged this run, so its *installed* version
        # is not a Pass-4 build input — exclude it from makedep_versions (2.5.1-B2).
        _build_set_names = frozenset(all_pass3)

        def _mk_reuse_ctx(pass_id: str, staged_dep_fps: list[str]) -> reuse.ReuseCtx:
            return reuse.ReuseCtx(
                pass_id=pass_id,
                cache=reuse_cache,
                cache_path=reuse_cache_file,
                config_digest=config_digest,
                profdata_sha=reuse_profdata_sha,
                pkgdest=reuse_pkgdest,
                consult=reuse_built,
                staged_dep_fps=staged_dep_fps,
                exclude_deps=_build_set_names,
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
            build_pgo_env.update(profdata.stage_env(staging))
        # BOLT Pass 5 (opt-in) rewrites the *finished* clang/libLLVM, which needs
        # relocations retained at link time — so the binaries that ship from
        # Pass 4a (libLLVM) and 4b (clang) link with -Wl,--emit-relocs. Off
        # unless [bolt] enabled; lib32 (4c) is never BOLTed so it is untouched.
        from sysforge.primitives import bolt as _bolt
        _bolt_ldflag = _bolt.emit_relocs_ldflag() if bolt_relocs else None
        pgo_fps = passes.build_pass(
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
            reuse_ctx=_mk_reuse_ctx("build-pgo", []),
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
            profdata.remove_staging(staging3)
            profdata.extract_built_to_staging(pgo_map, staging3, options.dry_run)
            # Fail fast if the split-package 'llvm' (cmake config + headers) did
            # not reach staging3: without LLVMConfig.cmake, the 4b/4c
            # find_package(LLVM) silently falls back to the live /usr libLLVM and
            # the non-pgo suite links against the wrong libLLVM → Gate-3
            # symbol-version brick. Cheaper to catch here than after install.
            if not options.dry_run:
                profdata.assert_staging_has_llvm_cmake(staging3)

        # 4b — build the non-pgo suite (clang, lld, …) against staging3's
        # libLLVM. Set ONLY CMAKE_PREFIX_PATH (mirror Pass 2): the host clang
        # compiles the source, it must not be forced to *load* the staged libLLVM
        # via LD_LIBRARY_PATH.
        if non_pgo_map:
            build_nonpgo_env = {
                "LLVM_PROFILE_FILE": "",
                "CMAKE_PREFIX_PATH": f"{staging3}/usr",
            }
            nonpgo_fps = passes.build_pass(
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
                reuse_ctx=_mk_reuse_ctx("build-nonpgo", sorted(pgo_fps.values())),
            )
            # Verify the split actually held: clang/lld must have linked the
            # staged shipped libLLVM, not the live /usr one. Abort before install
            # (no sentinel, no rollback) if a std::-bound-to-LLVM ref leaked.
            profdata.assert_pass_links_shipped_libllvm(
                non_pgo_map, label="Pass 4b", dry_run=options.dry_run,
            )
            # Stage the new clang/lld so a lib32 sub-pass can resolve them too.
            if lib32_map:
                profdata.extract_built_to_staging(non_pgo_map, staging3, options.dry_run)

        # 4c — lib32 against staging3 (usually empty; lib32 dropped from PGO in
        # d191a89). Same CMAKE_PREFIX_PATH steering.
        if lib32_map:
            pass3c_env = {
                "LLVM_PROFILE_FILE": "",
                "CMAKE_PREFIX_PATH": f"{staging3}/usr",
            }
            passes.build_pass(
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
                reuse_ctx=_mk_reuse_ctx(
                    "build-lib32", sorted([*pgo_fps.values(), *nonpgo_fps.values()]),
                ),
            )
            profdata.assert_pass_links_shipped_libllvm(
                lib32_map, label="Pass 4c (lib32)", dry_run=options.dry_run,
            )

        # Pass 4 is built but NOT installed here — the caller runs the Gate-2
        # ABI audit on the built packages, snapshots the current suite, then
        # installs inside the sentinel via pgo_install. Keeping the install
        # out of the build function means a Gate-2 abort leaves nothing
        # installed and no sentinel.
        if skip_profgen:
            _log.info("[PGO] Optimized build complete (profdata reused) — pending audit + install")
        else:
            _log.info("[PGO] 4/4 build complete — pending audit + install")

    finally:
        sudo_stack.close()

    # Sidecar is written after Pass 3 (above), not here — see comment there.
    # Staging is intentionally NOT removed here: the caller's Gate-3
    # verify_llvm_install runs after install, and on a verify failure the
    # staging3 prefix is needed by dump_stage_dynsym_evidence to contrast the
    # libLLVM Pass 4b linked against with the (bricked) installed one. Staging
    # is removed by the caller after Gate 3 passes (or a successful rollback).
    return all_pass3, "/usr/bin/clang", "/usr/bin/clang++", "lld", "pgo_llvm"
