"""
stages/toolchain.py — stage 6: LLVM/GCC toolchain build

Opt-in: stage is a clean no-op if /etc/sysforge/toolchain.toml is absent.
Systems that skip this stage use whatever compiler is already installed;
packages and kernel stages proceed normally.

toolchain.toml structure:
  compiler = "llvm"   # "llvm" or "gcc"
  pgo = true          # only meaningful when compiler = "llvm"; ignored for gcc

  [packages]
  pgo     = ["llvm", "llvm-libs", "clang", "lld"]
  non_pgo = ["polly", "compiler-rt", "openmp", "spirv-llvm-translator"]
  lib32   = ["lib32-llvm", "lib32-llvm-libs", "lib32-clang", ...]

  pgo_staging = "/var/tmp/sysforge-llvm-stage2"

LLVM PGO bootstrap (3 passes, only when pgo = true):
  Pass 1 — system compiler, pgo_llvm_toolchain profile, installs to system
  Pass 2 — pgo packages only, CC=pass1 clang, no system install, extract to staging
  Pass 3 — CC/CXX from staged binary, install to system; staging removed on success

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
    For GCC, pgo_pkgs = gcc packages, others = [].
    """
    compiler = tcfg.get("compiler", "llvm")
    pkgs_cfg = tcfg.get("packages", {})

    if compiler == "gcc":
        gcc_pkgs = pkgs_cfg.get("non_pgo", _DEFAULT_GCC)
        return gcc_pkgs, [], []

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
    Returns {name: pkgbuild_path}. Raises RuntimeError on any miss.
    """
    resolved = {}
    errors = []
    for name in names:
        try:
            resolved[name] = find_pkgbuild(name, config)
        except FileNotFoundError as e:
            errors.append(str(e))
    if errors:
        raise RuntimeError(
            "[TOOLCHAIN] Could not resolve PKGBUILDs:\n  " + "\n  ".join(errors)
        )
    return resolved


def _show_resolution_table(pkgbuild_map: dict[str, Path]) -> None:
    _log.ui("[TOOLCHAIN]", "─── PKGBUILD resolution ─────────────────────────────")
    for name, path in pkgbuild_map.items():
        _log.ui("[TOOLCHAIN]", f"  {name:<42}  {path}")
    _log.ui("[TOOLCHAIN]", "─────────────────────────────────────────────────────")


def _confirm_or_abort(state_dir) -> None:
    """Prompt user to confirm. On abort, print resume command and raise."""
    try:
        choice = input("[TOOLCHAIN] Proceed with toolchain build? [y/N]: ").strip().lower()
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
               init_session: bool = False) -> None:
    """Build one package via makepkg_wrapper.run()."""
    if options.dry_run:
        cc_label = f" CC={cc}" if cc else ""
        _log.ui("[TOOLCHAIN]", f"[dry-run] would build {name}{cc_label}")
        return
    makepkg_run(
        pkgbuild_path,
        extra_flags=extra_flags or [],
        pkg_log=not options.no_pkg_logs,
        persist_log=options.persist_log,
        cc_override=cc,
        cxx_override=cxx,
        init_session=init_session,
        update=not options.no_update,
    )


def _build_pass(label: str, pkgbuild_map: dict[str, Path], options,
                cc: str | None = None, cxx: str | None = None,
                install: bool = True) -> None:
    """Build all packages in pkgbuild_map for one pass."""
    extra = ["--install"] if install else []
    _log.ui("[TOOLCHAIN]", f"─── {label} ──────────────────────────────────────────")
    first = True
    for name, pkgbuild_path in pkgbuild_map.items():
        _log.ui("[TOOLCHAIN]", f"  {name}")
        _build_pkg(name, pkgbuild_path, options, cc=cc, cxx=cxx,
                   extra_flags=extra, init_session=first)
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
                    options) -> tuple[str, str, str]:
    """
    3-pass LLVM PGO build.

    Pass 1: system compiler, pgo_llvm_toolchain profile, installs to system
    Pass 2: CC=pass-1 clang, build pgo packages, no system install, extract to staging
    Pass 3: CC=staged clang from pass 2, install pgo + non_pgo + lib32 to system
            Remove staging prefix on success.

    Returns (cc, cxx, ld).
    """
    staged_cc  = str(staging / "usr/bin/clang")
    staged_cxx = str(staging / "usr/bin/clang++")

    # Pass 1 — build pgo packages with system compiler, install to system
    _build_pass("Pass 1: system compiler → install pgo packages", pgo_map, options,
                cc=None, cxx=None, install=True)

    # Pass 2 — rebuild pgo packages with instrumented Pass 1 clang, no system install,
    #           extract to staging prefix for use as CC/CXX in Pass 3
    _build_pass("Pass 2: instrumented build → staging (no system install)", pgo_map, options,
                cc="/usr/bin/clang", cxx="/usr/bin/clang++", install=False)
    _extract_pass2_to_staging(pgo_map, staging, options.dry_run)

    # Pass 3 — final PGO-optimized build with staged binary, install pgo + all extras
    all_pass3 = {**pgo_map, **non_pgo_map, **lib32_map}
    _build_pass("Pass 3: PGO-optimized build → install all", all_pass3, options,
                cc=staged_cc, cxx=staged_cxx, install=True)

    if not options.dry_run:
        _remove_staging(staging)

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
        if tcfg is None:
            _log.ui("[TOOLCHAIN]", "toolchain.toml absent — stage is a no-op")
            return

        compiler = tcfg.get("compiler", "llvm")
        pgo      = tcfg.get("pgo", True) if compiler == "llvm" else False
        staging  = Path(tcfg.get("pgo_staging", _DEFAULT_STAGING))

        pgo_pkgs, non_pgo_pkgs, lib32_pkgs = _package_lists(tcfg)

        _log.ui("[TOOLCHAIN]",
            f"Compiler: {compiler}  |  PGO: {pgo}  |  "
            f"Packages: {len(pgo_pkgs)} pgo / {len(non_pgo_pkgs)} non-pgo / {len(lib32_pkgs)} lib32"
        )

        # Resolve PKGBUILDs for all packages
        all_names = pgo_pkgs + non_pgo_pkgs + lib32_pkgs
        pkgbuild_map = _resolve_all_pkgbuilds(all_names, config)

        pgo_map     = {n: pkgbuild_map[n] for n in pgo_pkgs}
        non_pgo_map = {n: pkgbuild_map[n] for n in non_pgo_pkgs}
        lib32_map   = {n: pkgbuild_map[n] for n in lib32_pkgs}

        _show_resolution_table(pkgbuild_map)

        # Prompt for confirmation (interactive only)
        try:
            import sys as _sys
            if _sys.stdin.isatty() and not options.dry_run:
                _confirm_or_abort(options.state_dir)
        except RuntimeError:
            raise

        # Dispatch to build path
        if compiler == "gcc":
            cc, cxx, ld = _build_gcc(pgo_map, options)
        elif pgo:
            cc, cxx, ld = _build_llvm_pgo(pgo_map, non_pgo_map, lib32_map, staging, options)
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
