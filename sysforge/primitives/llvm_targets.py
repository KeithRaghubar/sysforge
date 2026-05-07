"""
llvm_targets.py — resolve the LLVM_TARGETS_TO_BUILD list for a build.

Used by pkgbuild_patcher when patching LLVM PKGBUILDs (llvm, clang,
compiler-rt, lld, …) to filter the cmake `-DLLVM_TARGETS_TO_BUILD=` flag
down to the backends this host actually uses.

Resolution order (first source that yields a non-None decision wins):
  1. toolchain.toml [llvm] targets   — explicit user override.
     - List provided   → use as-is (incl. empty list = "force all targets").
     - Section absent  → fall through.
  2. hardware_profile.toml [hardware] llvm_targets — autodetect from
     uname -m + lspci-derived gpu_vendors. Written by the hardware stage.
  3. Nothing resolved  → return None ("no filtering, build all targets").

Public API:
    resolve_llvm_targets(toolchain_toml_path, hardware_profile_path)
        -> list[str] | None
"""
import tomllib
from pathlib import Path

from sysforge import log

_log = log.get_logger("LLVM")


_DISABLE_FILTERING = "_disable_filtering"


def _read_toolchain_targets(path: Path):
    """Return a list[str], the sentinel _DISABLE_FILTERING for an explicit
    empty list, or None when the file/section/key is absent."""
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warn(f"failed to read {path}: {e} — falling back to autodetect")
        return None
    section = data.get("llvm")
    if not isinstance(section, dict):
        return None
    if "targets" not in section:
        return None
    targets = section["targets"]
    if not isinstance(targets, list):
        _log.warn(f"{path}: [llvm] targets is not a list — ignoring")
        return None
    if not targets:
        return _DISABLE_FILTERING
    return [str(t) for t in targets]


def _read_hardware_targets(path: Path):
    """Return a list[str] from hardware_profile.toml, or None when absent
    or empty."""
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("hardware", {})
    targets = section.get("llvm_targets")
    if not isinstance(targets, list) or not targets:
        return None
    return [str(t) for t in targets]


def resolve_llvm_targets(
    toolchain_toml_path: Path,
    hardware_profile_path: Path,
) -> list[str] | None:
    """Resolve the LLVM_TARGETS_TO_BUILD list for this build, or return
    None when no filtering should be applied (all targets built).
    """
    explicit = _read_toolchain_targets(toolchain_toml_path)
    if explicit is _DISABLE_FILTERING:
        # User asked to disable filtering — propagate as None so the
        # patcher leaves the upstream cmake invocation untouched.
        return None
    if explicit is not None:
        return explicit  # type: ignore[return-value]
    return _read_hardware_targets(hardware_profile_path)
