# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/profdata.py — profile data: generating, merging, staging, reusing.

The PGO half that is *not* the pass sequencing. Extracting a built package into
a staging prefix so the next pass can link against it; merging profraw into
profdata (with the settle window and adaptive batch sizing that keep
llvm-profdata from dying on partial writes and OOM); validating that the
environment can actually produce profiles; writing and checking the version
sidecar that decides whether an existing profile may be reused.

Kept apart from ``pgo.py`` because the pass sequence reads as a narrative and
these are the mechanisms it calls — mixing them was most of why the original
module was hard to follow.
"""
from pathlib import Path
import os
import subprocess
import threading

from sysforge.primitives import toolchain_safety
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives import run
from sysforge.primitives.resource_guard import make_child_preexec

from sysforge.pipeline.stages.toolchain import constants

from sysforge.primitives import prompt
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# PGO staging extraction
# ---------------------------------------------------------------------------


def extract_pkg_to_staging(pkg_file: Path, staging: Path) -> None:
    """Extract a .pkg.tar.* file to the staging directory."""
    staging.mkdir(parents=True, exist_ok=True)
    _log.info(f"  Extracting {pkg_file.name} → {staging}")
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


def extract_built_to_staging(
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
            extract_pkg_to_staging(pkg_file, staging)
        _log.info(f"  {name}: staged")


def assert_staging_has_llvm_cmake(staging: Path) -> None:
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


def remove_staging(staging: Path) -> None:
    import shutil

    if staging.exists():
        _log.info(f"Removing staging prefix: {staging}")
        shutil.rmtree(staging)


def do_profraw_merge(pgo_store: Path, label: str) -> tuple[int, int]:
    """
    Merge all .profraw files under pgo_store into clang.profdata using an
    atomic tmp→rename so concurrent readers always see a complete file.
    If clang.profdata already exists it is included as an input (incremental).

    Only merges files that have not been modified in the last constants.PROFRAW_SETTLE_SECS
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
        f for f in all_profraw if (now - f.stat().st_mtime) >= constants.PROFRAW_SETTLE_SECS
    ]
    if not profraw_files:
        return 0, 0

    profdata_path = pgo_store / "clang.profdata"
    tmp_path      = pgo_store / "clang.profdata.tmp"

    deleted    = 0
    n_batches  = 0
    batch_size = constants.PROFRAW_MERGE_BATCH_MAX
    i          = 0
    while i < len(profraw_files):
        batch  = profraw_files[i : i + batch_size]
        inputs = [str(profdata_path)] if profdata_path.exists() else []
        inputs += [str(f) for f in batch]

        result = subprocess.run(
            ["llvm-profdata", "merge", "--output", str(tmp_path)] + inputs,
            capture_output=True,
            text=True,
            # lift-only (no mem cap): a memory ceiling on the profdata merge would
            # masquerade as a merge failure and trip the batch-size backoff below.
            preexec_fn=make_child_preexec(None),
        )
        if result.returncode != 0:
            if batch_size > constants.PROFRAW_MERGE_BATCH_MIN:
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


def collect_pgo_packages(pkgbuild_map: dict[str, Path]) -> list[Path]:
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


def assert_pass_links_shipped_libllvm(
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
    pkgs = collect_pgo_packages(pkgbuild_map)
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


def pgo_install(label: str, pkgbuild_map: dict[str, Path], dry_run: bool) -> None:
    """
    Install packages built by a PGO pass via a direct 'sudo pacman -U' call.

    makepkg is run WITHOUT --install for PGO passes so that the sudo credential
    prompt (if any) occurs here — immediately after the build — rather than
    buried inside a multi-hour makepkg run.  The build may have outlasted the
    sudoers timestamp_timeout, and keepalive approaches are unreliable across
    sudo timestamp_type configurations (tty, ppid, global).  By issuing the
    sudo call here we guarantee it happens at a clean, predictable point.

    The ABI-hazard scan that used to live here is now Gate 2
    (``gate2_audit`` → ``toolchain_safety.scan_abi_hazards``), which runs
    *before* the snapshot + sentinel so a hazardous build aborts with nothing
    installed. This install is reached only after Gate 2 has cleared the built
    packages, and runs inside the sentinel; a ``pacman -U`` failure here raises
    so the caller can roll back to the snapshot.
    """
    if dry_run:
        _log.ui(f"[dry-run] would install packages from {label}")
        return
    pkgs = collect_pgo_packages(pkgbuild_map)
    if not pkgs:
        raise RuntimeError(
            f"[TOOLCHAIN] No built packages found for {label} — "
            "check that the build completed successfully"
        )
    _log.info(f"[PGO] Installing {len(pkgs)} package(s) ({label}):")
    for p in pkgs:
        _log.info(f"  {p.name}")
    result = subprocess.run(
        privileged_argv(["pacman", "-U", "--noconfirm"]) + [str(p) for p in pkgs]
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] pacman -U failed (exit {result.returncode}) for {label}"
        )


def pgo_stage_instrumented(
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
    via ``linker_flags_extra = profile_runtime_ldflag()``.
    """
    if dry_run:
        _log.ui(f"[dry-run] would extract Pass 1 packages to {staging1}")
        return

    all_pkgs = collect_pgo_packages(pkgbuild_map)
    if not all_pkgs:
        raise RuntimeError(
            "[TOOLCHAIN] No built packages found for Pass 1 — "
            "check that the build completed successfully"
        )

    _log.info(f"[PGO] Staging {len(all_pkgs)} Pass 1 package(s) → {staging1}:")
    for pkg_file in all_pkgs:
        extract_pkg_to_staging(pkg_file, staging1)


def stage_env(staging: Path) -> dict[str, str]:
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


def system_llvm_is_instrumented() -> bool:
    """Return True if the system libLLVMSupport.a contains PGO instrumentation symbols.

    Used before Pass 3 to detect whether a previous Pass 1 install left instrumented
    LLVM static libs on the system.  If so, packages that call find_package(LLVM) and
    link against those libs (e.g. a separate clang PKGBUILD) will need the profile
    runtime in LDFLAGS to satisfy the linker.
    """
    llvm_support = Path("/usr/lib/libLLVMSupport.a")
    if not llvm_support.exists():
        return False
    result = run.capture(["nm", "--defined-only", str(llvm_support)])
    if result is None:
        return False       # no nm: cannot prove instrumentation, so do not claim it
    return "__llvm_profile_" in result.stdout


def profile_runtime_ldflag() -> str | None:
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
    rt_result = run.capture(["/usr/bin/clang", "--print-runtime-dir"])
    if rt_result is None or rt_result.returncode != 0 or not rt_result.stdout.strip():
        _log.warn(
            "[PGO] Could not determine clang runtime dir (clang --print-runtime-dir failed); "
            "Pass 3 and Pass 4 may fail with undefined __llvm_profile_* symbols. "
            "Run: sudo pacman -S llvm  to restore an uninstrumented system LLVM.",
        )
        return None

    arch_result = run.capture(["uname", "-m"])
    arch = arch_result.stdout.strip() if arch_result else ""
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


def validate_pgo_environment(dry_run: bool) -> None:
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
    clang_probe = run.capture(
        [str(clang_path), "-x", "c", "-", "-o", "/dev/null"],
        input="int main(void){return 0;}\n",
    )
    if clang_probe is None:
        raise RuntimeError(
            f"[PGO] {clang_path} is missing — cannot validate the PGO environment"
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
        readelf_so = run.capture(["readelf", "-S", str(llvm_sos[0])])
        if readelf_so is not None and "__llvm_prf_" in readelf_so.stdout:
            stale.append(
                f"{llvm_sos[0].name} is instrumented (has __llvm_prf_* sections) — "
                "from a prior Pass 1 install; restore with: sudo pacman -S llvm llvm-libs"
            )

    # Instrumented static libs: causes undefined __llvm_profile_* linker errors
    # in Pass 2/3 for packages that call find_package(LLVM).  The build injects
    # the profile runtime into LDFLAGS to compensate, but a clean install avoids
    # the complexity entirely.
    if system_llvm_is_instrumented():
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

        if not prompt.is_interactive():
            raise RuntimeError(
                "[TOOLCHAIN] Aborting unattended PGO build: system LLVM packages "
                "have residual instrumentation from a prior aborted Pass 1 install. "
                "Restore clean packages before retrying: "
                "sudo pacman -S llvm llvm-libs"
            )
        answer = prompt.prompt_choice(
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


def profraw_merge_daemon(pgo_store: Path, stop_event: threading.Event) -> None:
    """
    Background thread: wake every constants.PGO_MERGE_INTERVAL seconds during Pass 3
    and merge accumulated .profraw files into the rolling clang.profdata.
    Keeps peak disk usage bounded during long instrumented builds.
    """
    while not stop_event.wait(constants.PGO_MERGE_INTERVAL):
        deleted, n_batches = do_profraw_merge(pgo_store, "intermediate")
        if deleted:
            _log.newline()
            _log.info(
                f"[PGO] Intermediate merge: {deleted} .profraw file(s) merged"
                + (f" in {n_batches} batches" if n_batches > 1 else ""),
            )


def merge_profraw(pgo_store: Path, dry_run: bool) -> Path:
    """
    Final profraw sweep after Pass 3 completes (daemon already stopped).

    Merges any .profraw files still on disk together with the existing
    clang.profdata produced by intermediate merges. If the daemon consumed
    everything there may be no remaining raws, which is fine.

    Fresh-file handling: if all remaining profraw files are younger than
    constants.PROFRAW_SETTLE_SECS (written in the very last seconds of the build),
    do_profraw_merge skips them and returns 0.  When the background daemon
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

    deleted, n_batches = do_profraw_merge(pgo_store, "final")
    if deleted == 0:
        # Distinguish between llvm-profdata failure and settle-filter exclusion.
        now = time.time()
        fresh = [
            f for f in profraw_files
            if (now - f.stat().st_mtime) < constants.PROFRAW_SETTLE_SECS
        ]
        if fresh and has_profdata:
            _log.warn(
                f"[PGO] {len(fresh)} fresh .profraw file(s) skipped (written "
                f"< {constants.PROFRAW_SETTLE_SECS}s ago at end of Pass 3); "
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


def pgo_target_major(pgo_map: dict[str, Path]) -> str | None:
    """Return the LLVM major version of the in-tree PGO PKGBUILDs, or None.

    All pgo PKGBUILDs share the same pkgver (Gate 1's
    toolchain_safety.check_pkgver_lockstep aborts otherwise); the first one
    that parses cleanly wins.
    Mirrors the major extracted in check_existing_profdata so the
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


def write_profdata_version(pgo_store: Path, pgo_map: dict[str, Path]) -> None:
    """
    Write a version sidecar (clang.profdata.version) containing the LLVM
    major version the profdata was generated against — derived from the
    in-tree PGO PKGBUILDs (what Pass 3 instrumented), not from the system
    `pacman -Q llvm` which can disagree across a major bump.  Called right
    after Pass 3 completes so an aborted Pass 4 still leaves recoverable
    profdata that the next run can reuse via check_existing_profdata.

    Failures are non-fatal — a missing sidecar just causes the next run to
    fall through to a full 4-pass rebuild rather than crashing.
    """
    try:
        major = pgo_target_major(pgo_map)
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
# Profdata reuse check
# ---------------------------------------------------------------------------


def resolve_skip_build_variant(pgo_store: Path) -> str:
    """Best-effort variant detection for the ``skip_build = true`` LLVM path.

    The skip_build branch registers paths without resolving PKGBUILDs, so we
    can't run the strict major-version match in ``check_existing_profdata``.
    Fall back to a presence check: if both ``clang.profdata`` and the version
    sidecar exist in ``pgo_store``, the installed clang is the result of a
    prior PGO build and reports ``pgo_llvm``. Otherwise ``stock_llvm``.

    This reflects on-disk provenance (what the user is actually running),
    not just the stage's current action.
    """
    if (pgo_store / "clang.profdata").exists() and (pgo_store / "clang.profdata.version").exists():
        return "pgo_llvm"
    return "stock_llvm"


def check_existing_profdata(
    pgo_store: Path,
    pgo_map: dict[str, Path],
) -> tuple[str, str | Path]:
    """
    Check whether compatible profdata exists for reuse.

    Compares the version sidecar (written by write_profdata_version after a
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
