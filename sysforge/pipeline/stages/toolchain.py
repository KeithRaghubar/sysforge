"""
stages/toolchain.py — stage 6: LLVM/GCC toolchain build

Opt-in: stage is a clean no-op if /etc/sysforge/toolchain.toml is absent.
Systems that skip this stage use whatever compiler is already installed;
packages and kernel stages proceed normally.

toolchain.toml structure:
  compiler    = "llvm"   # "llvm" or "gcc"
  pgo         = true     # only meaningful when compiler = "llvm"; ignored for gcc
  pgo_staging = "/var/tmp/sysforge-llvm-stage2"   # staging dir for pass-2 binaries
  pgo_store   = "/var/tmp/sysforge-llvm-pgo"      # dir for profraw/profdata files

  [packages]
  pgo     = ["llvm", "llvm-libs", "clang", "lld"]
  non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
  lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", ...]

LLVM PGO bootstrap (3 passes, only when pgo = true):
  Pass 1 — system compiler, standard flags; install pgo packages to system
  Pass 2 — CC=pass-1 clang, CFLAGS += -fprofile-generate=<pgo_store>;
            no system install; profraw merged → profdata; pass-2 binaries
            extracted to staging
  Pass 3 — CC=staged clang, CFLAGS += -fprofile-instr-use=<profdata>
            -fprofile-correction; install all packages to system;
            staging + profdata removed on success

Compiler propagation:
  On completion writes cc/cxx/ld to pipeline_state.toml [stages.toolchain.result]
"""
import subprocess
import sys
import tomllib
from pathlib import Path

import sysforge.log as _log
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.config import CONFIG_BASE, find_pkgbuild
from sysforge.primitives.makepkg_wrapper import run as makepkg_run


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

TOOLCHAIN_PATH = CONFIG_BASE / "etc/sysforge/toolchain.toml"

_DEFAULT_LLVM_PGO    = ["llvm", "llvm-libs", "clang", "lld"]
_DEFAULT_LLVM_NON_PGO = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
_DEFAULT_LLVM_LIB32  = [
    "lib32-llvm", "lib32-llvm-libs", "lib32-clang", "lib32-spirv-llvm-translator"
]
_DEFAULT_GCC         = ["gcc", "gcc-libs"]
_DEFAULT_STAGING     = "/var/tmp/sysforge-llvm-stage2"
_DEFAULT_PGO_STORE   = "/var/tmp/sysforge-llvm-pgo"

# Makepkg flags permitted through to PGO builds from user -m input.
# Only force-rebuild is safe; flags that alter build flow (e.g. --noextract,
# --nobuild, --noprepare) would corrupt the instrumentation/use sequence.
_PGO_ALLOWED_MAKEPKG_FLAGS = {"-f", "--force"}


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
        raise RuntimeError(f"[TOOLCHAIN] Failed to parse {TOOLCHAIN_PATH}: {e}") from None


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

    pgo_pkgs    = pkgs_cfg.get("pgo",     _DEFAULT_LLVM_PGO)
    non_pgo_pkgs = pkgs_cfg.get("non_pgo", _DEFAULT_LLVM_NON_PGO)
    lib32_pkgs  = pkgs_cfg.get("lib32",   _DEFAULT_LLVM_LIB32)
    return pgo_pkgs, non_pgo_pkgs, lib32_pkgs


# ---------------------------------------------------------------------------
# PKGBUILD resolution
# ---------------------------------------------------------------------------

def _resolve_all_pkgbuilds(names: list[str], config: dict) -> dict[str, Path]:
    """
    Resolve PKGBUILD paths for all package names.

    Three-pass strategy to handle split packages (e.g. llvm-libs comes from the
    llvm PKGBUILD and has no standalone clone target):
      1. Local direct: check pkgbuild_dir/<name>/PKGBUILD without cloning.
      2. Split scan: parse already-found PKGBUILDs for their pkgname arrays;
         reuse the path if a match is found.
      3. Full resolve: fall back to find_pkgbuild() which may clone from AUR/repo.

    Returns {name: pkgbuild_path}. Raises RuntimeError on any miss.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    resolved: dict[str, Path] = {}

    pkgbuild_dir: Path | None = None
    if config:
        raw = config.get("paths", {}).get("pkgbuild_dir")
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
            except Exception:
                coverage[path] = set()

        still_remaining = []
        for name in remaining:
            matched = next((p for p, provides in coverage.items() if name in provides), None)
            if matched:
                resolved[name] = matched
                _log.info("[TOOLCHAIN]", f"  {name} → split package in {matched.parent.name}/")
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
                for r in remaining[i + 1:]:
                    if r in covered:
                        resolved[r] = path
                        satisfied.append(r)
                        _log.info("[TOOLCHAIN]", f"  {r} → split package in {path.parent.name}/")
                for r in satisfied:
                    remaining.remove(r)
            except Exception:
                pass
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
            except Exception:
                pkgver = ""
            dir_info[d] = {"pkgver": pkgver, "names": []}
        dir_info[d]["names"].append(name)

    versions = {info["pkgver"] for info in dir_info.values() if info["pkgver"]}
    if len(versions) <= 1:
        return

    # Determine the dominant (most common) version to identify stale outliers
    from collections import Counter
    version_counts = Counter(info["pkgver"] for info in dir_info.values() if info["pkgver"])
    dominant = version_counts.most_common(1)[0][0]

    _log.warn("[TOOLCHAIN]",
        "PKGBUILD version mismatch detected — dependency resolution will likely fail:")
    for d, info in dir_info.items():
        ver = info["pkgver"] or "unknown"
        names = ", ".join(info["names"])
        marker = "  ← stale" if ver != dominant else ""
        _log.warn("[TOOLCHAIN]", f"  {ver:<16}  {d}  ({names}){marker}")
    _log.warn("[TOOLCHAIN]", "Sync stale directories before building:")
    for d, info in dir_info.items():
        if info["pkgver"] != dominant:
            _log.warn("[TOOLCHAIN]", f"  git -C {d} pull --rebase")


def _show_resolution_table(pkgbuild_map: dict[str, Path],
                           role_map: dict[str, str] | None = None) -> None:
    _log.ui("[TOOLCHAIN]", "─── PKGBUILD resolution ─────────────────────────────")
    for name, path in pkgbuild_map.items():
        role = f"  [{role_map[name]}]" if role_map and name in role_map else ""
        _log.ui("[TOOLCHAIN]", f"  {name:<36}  {path}{role}")
    _log.ui("[TOOLCHAIN]", "─────────────────────────────────────────────────────")


def _confirm_or_abort(state_dir) -> None:
    """Prompt user to confirm. On abort, print resume command and raise."""
    try:
        choice = input(_log.prompt_prefix("UI", "[TOOLCHAIN]") + "Proceed with toolchain build? [y/N]: ").strip().lower()
    except EOFError:
        # Non-interactive: proceed without prompt
        return
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

def _build_pkg(name: str, pkgbuild_path: Path, options,
               cc: str | None = None, cxx: str | None = None,
               extra_flags: list | None = None,
               init_session: bool = False,
               compiler_flags_extra: str | None = None,
               pgo_build: bool = False) -> None:
    """Build one package via makepkg_wrapper.run()."""
    if options.dry_run:
        cc_label = f" CC={cc}" if cc else ""
        _log.ui("[TOOLCHAIN]", f"[dry-run] would build {name}{cc_label}")
        return
    # Strip install flags — toolchain controls install/no-install via extra_flags.
    user_flags = [f for f in getattr(options, "makepkg_flags", []) if f not in ("-i", "--install")]
    if pgo_build:
        dropped = [f for f in user_flags if f not in _PGO_ALLOWED_MAKEPKG_FLAGS]
        user_flags = [f for f in user_flags if f in _PGO_ALLOWED_MAKEPKG_FLAGS]
        if dropped:
            _log.warn("[TOOLCHAIN]",
                      f"PGO build: ignoring -m flags that could corrupt the "
                      f"instrumentation sequence: {dropped}")
    combined_flags = list(extra_flags or []) + user_flags
    makepkg_run(
        pkgbuild_path,
        extra_flags=combined_flags,
        compiler_flags_extra=compiler_flags_extra,
        pkg_log=not options.no_pkg_logs,
        persist_log=options.persist_log,
        cc_override=cc,
        cxx_override=cxx,
        init_session=init_session,
        update=not options.no_update,
    )


def _build_pass(label: str, pkgbuild_map: dict[str, Path], options,
                cc: str | None = None, cxx: str | None = None,
                install: bool = True,
                compiler_flags_extra: str | None = None,
                pgo_build: bool = False) -> None:
    """Build all packages in pkgbuild_map for one pass.

    Deduplicates by PKGBUILD directory: split packages that share a directory
    (e.g. llvm, llvm-libs, clang from the same PKGBUILD) are only built once.
    """
    extra = ["--install"] if install else []
    _log.ui("[TOOLCHAIN]", f"─── {label} ──────────────────────────────────────────")
    seen_dirs: set[Path] = set()
    first = True
    for name, pkgbuild_path in pkgbuild_map.items():
        pkg_dir = pkgbuild_path.parent
        if pkg_dir in seen_dirs:
            _log.ui("[TOOLCHAIN]", f"  {name} (split — built with {pkg_dir.name})")
            continue
        seen_dirs.add(pkg_dir)
        _log.ui("[TOOLCHAIN]", f"  {name}")
        _build_pkg(name, pkgbuild_path, options, cc=cc, cxx=cxx,
                   extra_flags=extra, init_session=first,
                   compiler_flags_extra=compiler_flags_extra,
                   pgo_build=pgo_build)
        first = False


# ---------------------------------------------------------------------------
# PGO staging extraction
# ---------------------------------------------------------------------------

def _extract_pkg_to_staging(pkg_file: Path, staging: Path) -> None:
    """Extract a .pkg.tar.* file to the staging directory."""
    staging.mkdir(parents=True, exist_ok=True)
    _log.ui("[TOOLCHAIN]", f"  Extracting {pkg_file.name} → {staging}")
    result = subprocess.run(
        ["tar", "--warning=no-unknown-keyword", "-xf", str(pkg_file), "-C", str(staging)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] tar extraction failed for {pkg_file}: "
            f"{result.stderr.decode().strip()}"
        )


def _extract_pass2_to_staging(pkgbuild_map: dict[str, Path],
                               staging: Path, dry_run: bool) -> None:
    """
    After Pass 2 build (no install), find .pkg.tar.* in each build dir and
    extract to staging prefix. The staged binaries are used as CC/CXX in Pass 3.
    """
    if dry_run:
        _log.ui("[TOOLCHAIN]", f"[dry-run] would extract pass-2 packages to {staging}")
        return

    _log.ui("[TOOLCHAIN]", f"─── Pass 2: staging extraction → {staging} ────────")
    for name, pkgbuild_path in pkgbuild_map.items():
        build_dir = pkgbuild_path.parent
        pkgs = sorted(build_dir.glob(f"{name}-*.pkg.tar.*"))
        if not pkgs:
            raise RuntimeError(
                f"[TOOLCHAIN] No .pkg.tar.* found in {build_dir} for {name}. "
                "Pass 2 build may have failed or PKGDEST redirected output."
            )
        for pkg_file in pkgs:
            _extract_pkg_to_staging(pkg_file, staging)
        _log.ui("[TOOLCHAIN]", f"  {name}: staged")


def _remove_staging(staging: Path) -> None:
    import shutil
    if staging.exists():
        _log.ui("[TOOLCHAIN]", f"Removing staging prefix: {staging}")
        shutil.rmtree(staging)


def _merge_profraw(pgo_store: Path, dry_run: bool) -> Path:
    """
    Merge all .profraw files under pgo_store into a single .profdata file,
    then delete the raws to reclaim space (intermediate merge).

    Returns the path to the merged profdata file.
    Raises RuntimeError if no profraw files are found or the merge fails.
    """
    profdata_path = pgo_store / "clang.profdata"

    if dry_run:
        _log.ui("[TOOLCHAIN]", f"[dry-run] would merge .profraw files → {profdata_path}")
        return profdata_path

    profraw_files = list(pgo_store.glob("**/*.profraw"))
    if not profraw_files:
        raise RuntimeError(
            f"[TOOLCHAIN] No .profraw files found under {pgo_store} after Pass 2. "
            "Ensure the pgo packages are built with clang and -fprofile-generate "
            "was effective (check the build log)."
        )

    _log.ui("[TOOLCHAIN]",
            f"Merging {len(profraw_files)} .profraw file(s) → {profdata_path}")
    result = subprocess.run(
        ["llvm-profdata", "merge", "--output", str(profdata_path)]
        + [str(p) for p in profraw_files],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"[TOOLCHAIN] llvm-profdata merge failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    deleted = 0
    for f in profraw_files:
        try:
            f.unlink()
            deleted += 1
        except OSError:
            pass
    _log.info("[TOOLCHAIN]", f"Deleted {deleted} .profraw file(s) from {pgo_store}")
    return profdata_path


# ---------------------------------------------------------------------------
# Build paths
# ---------------------------------------------------------------------------

def _build_gcc(pkgbuild_map: dict[str, Path], options) -> tuple[str, str, None]:
    """Single-pass GCC build. Returns (cc, cxx, ld=None)."""
    _build_pass("GCC build (single pass)", pkgbuild_map, options, install=True)
    return "/usr/bin/gcc", "/usr/bin/g++", None


def _build_llvm_single(pkgbuild_map: dict[str, Path],
                       non_pgo_map: dict[str, Path],
                       lib32_map: dict[str, Path],
                       options) -> tuple[str, str, str]:
    """Single-pass LLVM build (pgo = false). Returns (cc, cxx, ld)."""
    all_pkgs = {**pkgbuild_map, **non_pgo_map, **lib32_map}
    _build_pass("LLVM build (single pass, no PGO)", all_pkgs, options, install=True)
    return "/usr/bin/clang", "/usr/bin/clang++", "lld"


def _build_llvm_pgo(pgo_map: dict[str, Path],
                    non_pgo_map: dict[str, Path],
                    lib32_map: dict[str, Path],
                    staging: Path,
                    pgo_store: Path,
                    options) -> tuple[str, str, str]:
    """
    3-pass LLVM PGO build.

    Pass 1: system compiler, standard flags; install pgo packages to system.
    Pass 2: CC=pass-1 clang; CFLAGS/CXXFLAGS/LDFLAGS += -fprofile-generate;
            no system install; profraw files merged → profdata (raws deleted);
            pass-2 binaries extracted to staging.
    Pass 3: CC=staged clang; CFLAGS/CXXFLAGS/LDFLAGS += -fprofile-instr-use
            + -fprofile-correction; install pgo + non_pgo + lib32 to system.
            Staging prefix and profdata removed on success.

    Returns (cc, cxx, ld).
    """
    staged_cc  = str(staging / "usr/bin/clang")
    staged_cxx = str(staging / "usr/bin/clang++")

    if not options.dry_run:
        pgo_store.mkdir(parents=True, exist_ok=True)

    # Pass 1 — build pgo packages with system compiler, install to system
    _build_pass("Pass 1: system compiler → install pgo packages", pgo_map, options,
                cc=None, cxx=None, install=True, pgo_build=True)

    # Pass 2 — instrumented build; generates profraw data under pgo_store
    _build_pass("Pass 2: instrumented build → profraw generation (no system install)",
                pgo_map, options,
                cc="/usr/bin/clang", cxx="/usr/bin/clang++", install=False,
                compiler_flags_extra=f"-fprofile-generate={pgo_store}",
                pgo_build=True)

    # Merge profraw → profdata, delete raws to reclaim space
    profdata_path = _merge_profraw(pgo_store, options.dry_run)
    _extract_pass2_to_staging(pgo_map, staging, options.dry_run)

    # Pass 3 — PGO-optimized build using merged profile data, install all
    all_pass3 = {**pgo_map, **non_pgo_map, **lib32_map}
    _build_pass("Pass 3: PGO-optimized build → install all", all_pass3, options,
                cc=staged_cc, cxx=staged_cxx, install=True,
                compiler_flags_extra=(
                    f"-fprofile-instr-use={profdata_path} -fprofile-correction"
                ),
                pgo_build=True)

    if not options.dry_run:
        try:
            profdata_path.unlink()
            _log.info("[TOOLCHAIN]", f"Removed profdata: {profdata_path}")
        except OSError:
            pass
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
    description = "LLVM/GCC toolchain build"
    depends_on = ["reconfigure"]

    def run(self, config, state, options):
        tcfg = _load_toolchain_config()
        if tcfg is None or not tcfg.get("enabled", False):
            _log.ui("[TOOLCHAIN]", "toolchain.toml absent or disabled — stage is a no-op")
            return

        compiler  = tcfg.get("compiler", "llvm")
        pgo       = tcfg.get("pgo", True) if compiler == "llvm" else False
        staging   = Path(tcfg.get("pgo_staging", _DEFAULT_STAGING))
        pgo_store = Path(tcfg.get("pgo_store", _DEFAULT_PGO_STORE))

        # skip_build: register compiler paths without building anything
        if tcfg.get("skip_build", False):
            cc, cxx, ld = _compiler_paths(compiler)
            _log.ui("[TOOLCHAIN]", f"skip_build=true — skipping build, registering {compiler}: cc={cc}  cxx={cxx}")
            result = {"cc": cc, "cxx": cxx}
            if ld is not None:
                result["ld"] = ld
            state.set_stage_result("toolchain", result)
            try:
                state.save()
            except PermissionError:
                _log.warn("[TOOLCHAIN]", "Cannot write state — toolchain results will not be checkpointed")
            return

        if compiler == "gcc" and not options.dry_run:
            _log.warn("[TOOLCHAIN]",
                "Building GCC from source is error-prone and yields no meaningful performance gains. "
                "Set skip_build = true in toolchain.toml to use the system GCC instead."
            )
            try:
                choice = input(_log.prompt_prefix("WARN", "[TOOLCHAIN]") + "Proceed with GCC build anyway? [y/N]: ").strip().lower()
            except (EOFError, OSError):
                choice = "y"
            if choice not in ("y", "yes"):
                raise RuntimeError("[TOOLCHAIN] GCC build aborted. Set skip_build = true in toolchain.toml to use the system GCC.")

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
        _log.ui("[TOOLCHAIN]", f"Compiler: {compiler}  |  PGO: {pgo}  |  Packages: {pkg_summary}")

        # Resolve PKGBUILDs for all packages
        pkgbuild_map = _resolve_all_pkgbuilds(all_names, config)
        _check_pkgver_consistency(pkgbuild_map)

        pgo_map     = {n: pkgbuild_map[n] for n in pgo_pkgs}
        non_pgo_map = {n: pkgbuild_map[n] for n in non_pgo_pkgs}
        lib32_map   = {n: pkgbuild_map[n] for n in lib32_pkgs}

        role_map = (
            {n: "pgo"     for n in pgo_pkgs}
            | {n: "non-pgo" for n in non_pgo_pkgs}
            | {n: "lib32"   for n in lib32_pkgs}
        ) if pgo else {n: "lib32" for n in lib32_pkgs}
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
            cc, cxx, ld = _build_llvm_pgo(pgo_map, non_pgo_map, lib32_map,
                                           staging, pgo_store, options)
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
            _log.warn("[TOOLCHAIN]", "Cannot write state — toolchain results will not be checkpointed")

        _log.ui("[TOOLCHAIN]",
            f"Toolchain stage complete. cc={cc}  cxx={cxx}" +
            (f"  ld={ld}" if ld else "")
        )
