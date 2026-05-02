"""
stages/toolchain.py — stage 6: LLVM/GCC toolchain build

Opt-in: stage is a clean no-op if /etc/sysforge/toolchain.toml is absent or
has enabled = false.  Systems that skip this stage use whatever compiler is
already installed; packages and kernel stages proceed normally.

toolchain.toml structure:
  enabled     = true     # must be true to activate the stage
  compiler    = "llvm"   # "llvm" or "gcc"
  pgo         = true     # only meaningful when compiler = "llvm"; ignored for gcc
  skip_build  = false    # if true: skip build, just register compiler paths in state
  pgo_staging = "/var/tmp/sysforge-llvm-stage2"   # staging dir for pass-2 binaries
  pgo_store   = "/var/tmp/sysforge-llvm-pgo"      # dir for profraw/profdata files

  [packages]
  pgo     = ["llvm", "llvm-libs"]
  non_pgo = ["clang", "lld", "polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
  lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", ...]

LLVM PGO bootstrap (3 passes, only when pgo = true):
  Pass 1 — system compiler + -fprofile-generate=<pgo_store>/; builds ONLY the
            llvm/llvm-libs PKGBUILD (pgo list).  clang and lld are NOT built with
            instrumentation here — they link against the official libLLVM.so at
            build time, so the resulting libclang-cpp.so would embed a versioned
            dependency on _M_assign@LLVM_22.1 (a weak symbol the official libLLVM.so
            exports via its version script by inlining std::string::_M_assign).
            The instrumented libLLVM.so does not export that symbol (inlining is
            suppressed by -fprofile-generate), so loading the instrumented
            libclang-cpp.so against the instrumented libLLVM.so causes a symbol
            lookup error at runtime.  Keeping clang/lld in non_pgo avoids this.
            makepkg runs without --install; _pgo_pass1_install() calls sudo pacman -U
            directly so the sudo keepalive's timestamp entry applies.  Only the
            shared-lib package (llvm-libs) is installed; cmake-config/static-lib
            packages (llvm, which contains instrumented .a archives) are excluded so
            that subsequent find_package(LLVM) calls still use the uninstrumented
            cmake config and don't link instrumented static libs.
            Spurious profraw from CMake feature probes purged before Pass 2.
  Pass 2 — CC=/usr/bin/clang (the system, non-instrumented binary); builds
            pgo + non_pgo packages (lib32 excluded).  clang/lld/compiler-rt/openmp/
            polly/spirv-llvm-translator exercise clang frontend, linker, OpenMP
            structured blocks, compiler-rt intrinsics, and polyhedral analysis.
            The system clang calls into the instrumented libLLVM.so (installed in
            Pass 1), generating profraw from LLVM's core optimization and codegen
            work — the most performance-critical hot paths for a compiler.
            CCACHE_DISABLE=1 / SCCACHE_DISABLE=1 injected so cache tools cannot
            bypass the instrumented compiler and silently produce no profraw.
            LLVM_PROFILE_FILE uses %m_%p (per-module-hash + per-PID) so parallel
            make -j clang processes each write their own profraw without
            contending on one file.  Background daemon merges profraw every
            _PGO_MERGE_INTERVAL seconds with adaptive batch sizing
            (_PROFRAW_MERGE_BATCH_MAX → _PROFRAW_MERGE_BATCH_MIN on OOM).
            llvm-profdata invoked with RLIMIT_AS lifted (lift_for_child) so it
            is not constrained by the sysforge controller's 2 GiB cap.
            No system install; pgo-package binaries extracted to staging.
            Merged profdata size logged at [INFO]; warns if below
            _PGO_PROFDATA_MIN_BYTES (likely indicates bypassed compilation).
  Pass 3 — CC=staged clang if available (clang in pgo list), else system clang.
            CFLAGS += -fprofile-use=<profdata>; install all packages
            (pgo + non_pgo + lib32) via _pgo_install(); staging + profdata
            removed on success.

  A sudo keepalive thread refreshes credentials every _SUDO_KEEPALIVE_INTERVAL
  seconds throughout all three passes.

Compiler propagation:
  On completion writes cc/cxx/ld to pipeline_state.toml [stages.toolchain.result]
"""

import subprocess
import sys
import threading
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("TOOLCHAIN")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.config import find_pkgbuild
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives.makepkg_wrapper import run as makepkg_run, BuildOptions
from sysforge.primitives.prompt import is_interactive, prompt_choice
from sysforge.primitives.resource_guard import lift_for_child

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
_DEFAULT_GCC = ["gcc", "gcc-libs"]
_DEFAULT_STAGING = "/var/tmp/sysforge-llvm-stage2"
_DEFAULT_PGO_STORE = "/var/tmp/sysforge-llvm-pgo"

# Makepkg flags permitted through to PGO builds from user -m input.
# Only force-rebuild is safe; flags that alter build flow (e.g. --noextract,
# --nobuild, --noprepare) would corrupt the instrumentation/use sequence.
_PGO_ALLOWED_MAKEPKG_FLAGS = {"-f", "--force"}

# Interval (seconds) between intermediate profraw merges during Pass 2.
_PGO_MERGE_INTERVAL = 15

# How often (seconds) to refresh sudo credentials during the PGO build sequence.
# The 3-pass build can run for 2+ hours unattended. The keepalive calls sudo
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
    Return (pgo_pkgs, non_pgo_pkgs, lib32_pkgs) from toolchain config.
    For GCC, non_pgo_pkgs = gcc packages, pgo_pkgs and lib32_pkgs = [].
    """
    compiler = tcfg.get("compiler", "llvm")
    pkgs_cfg = tcfg.get("packages", {})

    if compiler == "gcc":
        gcc_pkgs = pkgs_cfg.get("non_pgo", _DEFAULT_GCC)
        return [], gcc_pkgs, []

    pgo_pkgs = pkgs_cfg.get("pgo", _DEFAULT_LLVM_PGO)
    non_pgo_pkgs = pkgs_cfg.get("non_pgo", _DEFAULT_LLVM_NON_PGO)
    lib32_pkgs = pkgs_cfg.get("lib32", _DEFAULT_LLVM_LIB32)
    return pgo_pkgs, non_pgo_pkgs, lib32_pkgs


# ---------------------------------------------------------------------------
# PKGBUILD resolution
# ---------------------------------------------------------------------------


def _resolve_all_pkgbuilds(names: list[str], config: dict) -> dict[str, Path]:
    """
    Resolve PKGBUILD paths for all package names.

    Three-pass strategy to handle split packages (e.g. llvm-libs comes from the
    llvm PKGBUILD and has no standalone clone target):
      1. Local direct: check pkgbuild_src_dir/<name>/PKGBUILD without cloning.
      2. Split scan: parse already-found PKGBUILDs for their pkgname arrays;
         reuse the path if a match is found.
      3. Full resolve: fall back to find_pkgbuild() which may clone from AUR/repo.

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
    return resolved


def _check_pkgver_consistency(pkgbuild_map: dict[str, Path]) -> None:
    """
    Parse all resolved PKGBUILDs and warn if they have inconsistent pkgver values.

    Toolchain packages (e.g. llvm, clang, lld) all come from the same upstream
    release and must share the same pkgver. A mismatch causes dependency resolution
    failures at build time (e.g. clang requires llvm=22.1.0 but llvm is 22.1.1).
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    # Parse each unique PKGBUILD directory once
    dir_info: dict[Path, dict] = {}  # dir -> {pkgver, names}
    for name, path in pkgbuild_map.items():
        d = path.parent
        if d not in dir_info:
            try:
                meta = parse_pkgbuild(path)
                pkgver = meta.get("globals", {}).get("pkgver", "")
            except Exception as e:
                _log.info(f"  pkgver consistency: parse failed for {path}: {e}")
                pkgver = ""
            dir_info[d] = {"pkgver": pkgver, "names": []}
        dir_info[d]["names"].append(name)

    versions = {info["pkgver"] for info in dir_info.values() if info["pkgver"]}
    if len(versions) <= 1:
        return

    # Determine the dominant (most common) version to identify stale outliers
    from collections import Counter

    version_counts = Counter(
        info["pkgver"] for info in dir_info.values() if info["pkgver"]
    )
    dominant = version_counts.most_common(1)[0][0]

    _log.warn(
        "PKGBUILD version mismatch detected — dependency resolution will likely fail:",
    )
    for d, info in dir_info.items():
        ver = info["pkgver"] or "unknown"
        names = ", ".join(info["names"])
        marker = "  ← stale" if ver != dominant else ""
        _log.warn(f"  {ver:<16}  {d}  ({names}){marker}")
    _log.warn("Sync stale directories before building:")
    for d, info in dir_info.items():
        if info["pkgver"] != dominant:
            _log.warn(f"  git -C {d} pull --rebase")


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
) -> None:
    """Build one package via makepkg_wrapper.run()."""
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
    makepkg_run(pkgbuild_path, options=BuildOptions(
        extra_flags=combined_flags,
        compiler_flags_extra=compiler_flags_extra,
        linker_flags_extra=linker_flags_extra,
        pkg_log=not options.no_pkg_logs,
        persist_log=options.persist_log,
        cc_override=cc,
        cxx_override=cxx,
        init_session=init_session,
        update=not options.no_update,
        strip_full_lto=pgo_build,
        extra_env=pgo_env,
        abi_check=getattr(options, "abi_check", False),
        pgo_managed=True,
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
) -> None:
    """Build all packages in pkgbuild_map for one pass.

    Deduplicates by PKGBUILD directory: split packages that share a directory
    (e.g. llvm, llvm-libs, clang from the same PKGBUILD) are only built once.
    """
    extra = ["--install"] if install else []
    if pgo_build:
        extra = ["--cleanbuild"] + extra
    _log.ui(f"─── {label} ──────────────────────────────────────────")
    seen_dirs: set[Path] = set()
    first = True
    for name, pkgbuild_path in pkgbuild_map.items():
        pkg_dir = pkgbuild_path.parent
        if pkg_dir in seen_dirs:
            _log.ui(f"  {name} (split — built with {pkg_dir.name})")
            continue
        seen_dirs.add(pkg_dir)
        _log.ui(f"  {name}")
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
    seconds throughout the 3-pass PGO build sequence.

    The 3-pass build can run unattended for 2+ hours. _pgo_install() calls
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


def _pgo_pass1_install(pkgbuild_map: dict[str, Path], dry_run: bool) -> None:
    """Install Pass 1 packages to system, excluding packages that provide LLVM cmake config.

    The pgo list should only contain the llvm/llvm-libs PKGBUILD (not clang or lld).
    clang and lld use a separate PKGBUILD that calls find_package(LLVM) and links
    against libLLVM.so.  When built with -fprofile-generate, they pick up a versioned
    symbol dependency on _M_assign@LLVM_22.1 (a weak inlined symbol that the official
    libLLVM.so exports via its version script).  The instrumented libLLVM.so does not
    export this symbol (inlining is suppressed by -fprofile-generate) so loading the
    instrumented libclang-cpp.so at Pass 2 runtime causes a symbol lookup crash.

    Within the llvm/llvm-libs PKGBUILD, the cmake-config package ('llvm') is excluded:
    it contains instrumented static archives that would cause __llvm_profile_* link
    errors in any package that calls find_package(LLVM) and uses component targets.
    Only the shared-lib package ('llvm-libs') is installed, making the instrumented
    libLLVM.so available at runtime without exposing the instrumented .a files.
    """
    if dry_run:
        _log.ui("[dry-run] would install Pass 1 packages (excluding static/cmake)")
        return

    all_pkgs = _collect_pgo_packages(pkgbuild_map)
    if not all_pkgs:
        raise RuntimeError(
            "[TOOLCHAIN] No built packages found for Pass 1 — "
            "check that the build completed successfully"
        )

    install_pkgs = []
    excluded = []
    for pkg_file in all_pkgs:
        if _has_llvm_cmake_config(pkg_file):
            excluded.append(pkg_file.name)
        else:
            install_pkgs.append(pkg_file)

    if excluded:
        _log.info(
            f"[PGO] Pass 1: excluding {len(excluded)} static/cmake package(s) from "
            "system install (instrumented .a archives would break Pass 2 "
            f"find_package(LLVM)): {', '.join(excluded)}",
        )

    if not install_pkgs:
        _log.warn(
            "[PGO] Pass 1: all packages excluded from system install — "
            "no shared-lib or binary packages found; Pass 2 will use the "
            "pre-PGO system compiler and may produce no profraw",
        )
        return

    _log.ui(f"[PGO] Installing {len(install_pkgs)} package(s) (Pass 1):")
    for p in install_pkgs:
        _log.ui(f"  {p.name}")
    result = subprocess.run(
        ["sudo", "pacman", "-U", "--noconfirm"] + [str(p) for p in install_pkgs]
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] pacman -U failed (exit {result.returncode}) for Pass 1"
        )


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


def _write_profdata_version(pgo_store: Path) -> None:
    """
    Write a version sidecar (clang.profdata.version) containing the installed
    LLVM major version.  Called after a successful PGO build so that
    sysforge update can check compatibility before reusing the profdata.
    Failures are non-fatal — a missing sidecar just causes updates to skip
    the profdata rather than crashing.
    """
    try:
        result = subprocess.run(
            ["pacman", "-Q", "llvm"], capture_output=True, text=True
        )
        if result.returncode != 0:
            _log.warn(
                "[PGO] Could not query LLVM version via pacman — profdata version sidecar not written",
            )
            return
        # "llvm 22.1.0-1" → major "22"
        ver_str = result.stdout.split()[1]
        major = ver_str.split(".")[0]
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
    # All pgo PKGBUILDs should share the same pkgver (enforced by
    # _check_pkgver_consistency); use the first one.
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
# Build paths
# ---------------------------------------------------------------------------


def _build_gcc(pkgbuild_map: dict[str, Path], options) -> tuple[str, str, None]:
    """Single-pass GCC build. Returns (cc, cxx, ld=None)."""
    _build_pass("GCC build (single pass)", pkgbuild_map, options, install=True)
    return "/usr/bin/gcc", "/usr/bin/g++", None


def _build_llvm_single(
    pkgbuild_map: dict[str, Path],
    non_pgo_map: dict[str, Path],
    lib32_map: dict[str, Path],
    options,
) -> tuple[str, str, str]:
    """Single-pass LLVM build (pgo = false). Returns (cc, cxx, ld)."""
    all_pkgs = {**pkgbuild_map, **non_pgo_map, **lib32_map}
    _build_pass("LLVM build (single pass, no PGO)", all_pkgs, options, install=True)
    return "/usr/bin/clang", "/usr/bin/clang++", "lld"


def _build_llvm_pgo(
    pgo_map: dict[str, Path],
    non_pgo_map: dict[str, Path],
    lib32_map: dict[str, Path],
    staging: Path,
    pgo_store: Path,
    options,
) -> tuple[str, str, str]:
    """
    3-pass LLVM PGO build.

    Pass 1: system compiler + -fprofile-generate; builds ONLY llvm/llvm-libs
            (pgo list).  clang and lld live in non_pgo to avoid a versioned-symbol
            ABI issue: building them with -fprofile-generate against the official
            libLLVM.so embeds a runtime dependency on _M_assign@LLVM_22.1 (a weak
            symbol only the official build exports via inlining).  The instrumented
            libLLVM.so does not export it → symbol lookup crash in Pass 2.
            _pgo_pass1_install() installs llvm-libs (shared lib) but skips the
            llvm package (cmake-config + instrumented .a archives) so that
            subsequent find_package(LLVM) calls still resolve the uninstrumented
            cmake config and avoid linking instrumented static libs.
    Pass 2: CC=/usr/bin/clang (system, non-instrumented); builds pgo + non_pgo
            packages (lib32 excluded).  The system clang calls into the instrumented
            libLLVM.so, generating profraw from LLVM core hot paths.  CCACHE_DISABLE=1 and
            SCCACHE_DISABLE=1 injected so cache tools cannot bypass the
            instrumented compiler and silently produce no profraw.  Background
            daemon merges profraw periodically; final sweep after build.
            Profdata size checked; warns if suspiciously small.
            Pgo-package binaries extracted to staging; no system install.
    Pass 3: CC=staged clang (if clang in pgo list) else system clang.
            CFLAGS/LDFLAGS += -fprofile-use; LTO disabled via LTOFLAGS=""
            (ThinLTO + IR PGO causes non-PIC vtable relocations in lld's
            ThinLTO codegen for libLLVM.so); install pgo + non_pgo + lib32.
            Staging prefix removed on success.  Profdata preserved with a
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
    # skip passes 1-2 and go straight to Pass 3 (the optimized build).
    # --rebuild-profdata forces a full 3-pass build regardless.
    skip_profgen = False
    profdata_path = pgo_store / "clang.profdata"
    if not options.rebuild_profdata and not options.dry_run:
        pgo_state, pgo_info = _check_existing_profdata(pgo_store, pgo_map)
        if pgo_state == "ready":
            skip_profgen = True
            profdata_path = Path(pgo_info)
            _log.ui(
                f"[PGO] Reusing existing profdata: {profdata_path}  "
                f"(use --rebuild-profdata to force a full 3-pass build)",
            )
        elif pgo_state == "mismatch":
            _log.info(f"[PGO] Existing profdata incompatible: {pgo_info}")
        else:
            _log.info(f"[PGO] No existing profdata: {pgo_info}")

    if skip_profgen:
        _log.ui(
            f"[PGO] Skipping passes 1-2, building with existing profdata  "
            f"({n_total} package(s) across {len(set({**pgo_map, **non_pgo_map, **lib32_map}.values()))} PKGBUILD(s))  "
            f"pgo_store={pgo_store}",
        )
    else:
        _log.ui(
            f"[PGO] Starting 3-pass LLVM PGO build  "
            f"({n_pgo} pgo PKGBUILD(s), {n_total} total across all passes)  "
            f"pgo_store={pgo_store}",
        )

    if not skip_profgen and not options.dry_run:
        import shutil as _shutil

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
            # Pass 1 — build pgo packages with system compiler + instrumentation flags.
            # The system compiler must be clang: -fprofile-generate produces LLVM-format
            # .profraw files (consumed by llvm-profdata); GCC would produce GCOV format.
            # On a running Arch system with LLVM installed this is always clang.
            # makepkg runs WITHOUT --install; _pgo_install() issues `sudo pacman -U`
            # directly from sysforge so the keepalive's timestamp entry applies.
            _build_pass(
                "Pass 1/3 [PGO] instrumented build → pgo packages",
                pgo_map,
                options,
                cc=None,
                cxx=None,
                install=False,
                pgo_build=True,
                compiler_flags_extra=f"-fprofile-generate={pgo_store}/",
            )
            _pgo_pass1_install(pgo_map, options.dry_run)
            _log.ui("[PGO] Pass 1/3 complete")

            # Purge any profraw accumulated during Pass 1. CMake feature-test programs
            # compiled with -fprofile-generate run during configuration and deposit
            # spurious profraw files (and emit "Running out of static counters" warnings).
            # Those files represent tiny probe programs, not clang doing real work — they
            # would contaminate the training profile if kept. Pass 2 generates the real data.
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

            # Pass 2 — use the instrumented Pass-1 clang as CC; profraw is generated
            # as a side effect of running it. Background daemon merges periodically.
            #
            # LLVM_PROFILE_FILE uses %m_%p so each parallel clang process writes to its
            # own file (module-hash + PID) instead of all contending on default_%m.profraw.
            # Without this, N parallel `make -j` clang invocations corrupt each other's
            # profraw via concurrent writes, causing SIGBUS crashes in llvm-profdata.
            pass2_env = {
                "LLVM_PROFILE_FILE": f"{pgo_store}/default_%m_%p.profraw",
                # Prevent ccache/sccache from serving cached objects during the
                # training run.  If either tool intercepts a compilation it skips
                # running the instrumented clang entirely, producing no profraw.
                # _DISABLE=1 makes each tool act as a transparent pass-through so
                # the instrumented binary always executes and writes profraw data.
                "CCACHE_DISABLE": "1",
                "SCCACHE_DISABLE": "1",
            }
            _log.info(
                f"[PGO] Pass 2: LLVM_PROFILE_FILE={pass2_env['LLVM_PROFILE_FILE']}",
            )

            # Safety net: if system LLVM static libs are still instrumented (from a
            # prior Pass 1 run before _pgo_pass1_install excluded cmake-config packages),
            # packages that call find_package(LLVM) in Pass 2 or Pass 3 would link
            # against them and fail to resolve __llvm_profile_* symbols.  Inject the
            # profile runtime into LDFLAGS for both passes.  The check is done once
            # here — system LLVM state does not change between Pass 2 and Pass 3
            # (neither pass installs to the system until _pgo_install at the end).
            if not options.dry_run and _system_llvm_is_instrumented():
                _log.info(
                    "[PGO] System libLLVMSupport.a is instrumented (residual from a prior "
                    "Pass 1 install). Injecting profile runtime into Pass 2 and Pass 3 LDFLAGS.",
                )
                residual_linker_flags = _profile_runtime_ldflag()

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
            try:
                _build_pass(
                    "Pass 2/3 [PGO] training run → profraw generation (no system install)",
                    pass2_map,
                    options,
                    cc="/usr/bin/clang",
                    cxx="/usr/bin/clang++",
                    install=False,
                    linker_flags_extra=residual_linker_flags,
                    pgo_build=True,
                    pgo_env=pass2_env,
                )
            finally:
                stop_event.set()
                if not options.dry_run:
                    monitor.join()
            _log.ui("[PGO] Pass 2/3 complete")

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
            _log.ui(f"[PGO] Profile data ready: {profdata_path}")
            _extract_pass2_to_staging(pgo_map, staging, options.dry_run)

        # Pass 3 (or sole pass when reusing profdata) — PGO-optimized build.
        # Use staged clang from Pass 2 if available; otherwise fall back to
        # system clang (which, after a prior successful run, is already PGO-optimized).
        if not skip_profgen and not options.dry_run and not Path(staged_cc).exists():
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
            "[PGO] optimized build → all packages (reusing profdata)"
            if skip_profgen
            else "Pass 3/3 [PGO] optimized build → all packages"
        )
        _build_pass(
            pass3_label,
            all_pass3,
            options,
            cc=pass3_cc,
            cxx=pass3_cxx,
            install=False,
            compiler_flags_extra=f"-fprofile-use={profdata_path}",
            linker_flags_extra=residual_linker_flags,
            pgo_build=True,
        )
        _pgo_install(pass3_label, all_pass3, options.dry_run)
        if skip_profgen:
            _log.ui("[PGO] Optimized build complete (profdata reused)")
        else:
            _log.ui("[PGO] Pass 3/3 complete — PGO build finished")

    finally:
        sudo_stop.set()
        if not options.dry_run:
            sudo_keepalive.join()

    if not options.dry_run:
        _write_profdata_version(pgo_store)
        if not skip_profgen:
            _remove_staging(staging)

    return "/usr/bin/clang", "/usr/bin/clang++", "lld"


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
    description = "LLVM/GCC toolchain build (experimental — post-1.0)"
    depends_on = ["reconfigure"]

    def run(self, config, state, options):
        tcfg = _load_toolchain_config()
        if tcfg is None or not tcfg.get("enabled", False):
            _log.ui(
                "toolchain.toml absent or disabled — stage is a no-op"
            )
            return

        _log.warn(
            "toolchain stage is experimental and deferred to post-1.0 — "
            "PGO bootstrap has known sharp edges; proceed with caution",
        )

        compiler = tcfg.get("compiler", "llvm")
        pgo = tcfg.get("pgo", True) if compiler == "llvm" else False
        staging = Path(tcfg.get("pgo_staging", _DEFAULT_STAGING))
        pgo_store = Path(tcfg.get("pgo_store", _DEFAULT_PGO_STORE))

        # skip_build: register compiler paths without building anything
        if tcfg.get("skip_build", False):
            cc, cxx, ld = _compiler_paths(compiler)
            _log.ui(
                f"skip_build=true — skipping build, registering {compiler}: cc={cc}  cxx={cxx}",
            )
            result = {"cc": cc, "cxx": cxx}
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

        if compiler == "gcc" and not options.dry_run:
            _log.warn(
                "Building GCC from source is error-prone and yields no meaningful performance gains. "
                "Set skip_build = true in toolchain.toml to use the system GCC instead.",
            )
            # eof_default="y": preserve the deliberate "EOF means proceed
            # unattended" semantic this site has always had.
            choice = prompt_choice(
                "Proceed with GCC build anyway? [y/N]: ",
                choices=("y", "yes", "n"),
                default="n",
                eof_default="y",
                tag="TOOLCHAIN",
                level="WARN",
            )
            if choice not in ("y", "yes"):
                raise RuntimeError(
                    "[TOOLCHAIN] GCC build aborted. Set skip_build = true in toolchain.toml to use the system GCC."
                )

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

        # Resolve PKGBUILDs for all packages
        pkgbuild_map = _resolve_all_pkgbuilds(all_names, config)
        _check_pkgver_consistency(pkgbuild_map)

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

        # Prompt for confirmation (interactive only)
        try:
            import sys as _sys

            if _sys.stdin.isatty() and not options.dry_run:
                _confirm_or_abort(options.state_dir)
        except RuntimeError:
            raise

        # Dispatch to build path
        if compiler == "gcc":
            cc, cxx, ld = _build_gcc(non_pgo_map, options)
        elif pgo:
            cc, cxx, ld = _build_llvm_pgo(
                pgo_map, non_pgo_map, lib32_map, staging, pgo_store, options
            )
        else:
            cc, cxx, ld = _build_llvm_single(pgo_map, non_pgo_map, lib32_map, options)

        # Write compiler paths to pipeline state for downstream stages
        result = {"cc": cc, "cxx": cxx}
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
