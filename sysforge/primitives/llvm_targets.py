# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
llvm_targets.py — resolve the LLVM_TARGETS_TO_BUILD list for a build.

Used by pkgbuild_patcher when patching LLVM PKGBUILDs (llvm, clang,
compiler-rt, lld, …) to filter the cmake `-DLLVM_TARGETS_TO_BUILD=` flag
down to the backends this host actually uses.

Resolution order (first source that yields a non-None decision wins):
  1. toolchain.toml [llvm] targets   — explicit user override.
     - List provided   → use as-is.
     - Empty list      → "force all targets" (disables filtering).
     - Section absent  → fall through.
  2. hardware_profile.toml [hardware] llvm_targets — autodetect from
     uname -m + lspci-derived gpu_vendors. Written by the hardware stage.
  3. Live hardware detection — uname -m + lspci, same logic the hardware
     stage runs but inline, no file I/O. Used by
     ``resolve_or_detect_llvm_targets`` so the patcher works even when
     no hardware_profile.toml exists at the resolved state dir (which
     happens when ``sysforge run toolchain`` runs alone — hardware stage
     isn't a transitive dep, so its profile may not have been written
     for the current state dir).
  4. Nothing resolved  → return None ("no filtering, build all targets").

Invariant (any non-None, non-empty result): the mandatory system-libLLVM
consumer baseline (``hardware._SYSTEM_LIBLLVM_CONSUMER_TARGETS`` — AMDGPU,
which Arch's mesa links from libgallium unconditionally) is always present,
regardless of which source above won. ``derive_llvm_targets`` bakes it into
freshly-derived lists; ``_ensure_system_consumer_targets`` re-applies it here
so a *cached or hand-edited* ``hardware_profile.toml`` (source 2) that bypasses
derivation — or an explicit ``toolchain.toml`` list (source 1) that omits it —
can't ship a system libLLVM that bricks the desktop. The single opt-out is
``[llvm] targets = []`` (build all), which resolves to None and never reaches
the enforcement.

Live detection requires ``lspci`` (pciutils package, in Arch base) on
PATH. Missing/failed lspci is non-fatal — GPU backends just won't be
in the list.

Public API:
    resolve_llvm_targets(toolchain_toml_path, hardware_profile_path)
        -> list[str] | None
    resolve_or_detect_llvm_targets(toolchain_toml_path, hardware_profile_path)
        -> list[str] | None
"""
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.hardware_probe import (
    derive_llvm_targets,
    detect_host_arch,
    parse_gpu_vendors,
)
from sysforge.primitives.hardware_tables import SYSTEM_LIBLLVM_CONSUMER_TARGETS
from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE
from sysforge.primitives import toolchain_safety
from sysforge.primitives import run

_log = log.get_logger("LLVM")


_DISABLE_FILTERING = "_disable_filtering"


def _read_toolchain_targets(path: Path):
    """Return a list[str], the sentinel _DISABLE_FILTERING for an explicit
    empty list, or None when the file/section/key is absent."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warn(f"failed to read {path}: {e}; falling back to autodetect")
        return None
    section = data.get("llvm")
    if not isinstance(section, dict):
        return None
    if "targets" not in section:
        return None
    targets = section["targets"]
    if not isinstance(targets, list):
        _log.warn(f"{path}: [llvm] targets is not a list; ignoring")
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
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    section = data.get("hardware", {})
    targets = section.get("llvm_targets")
    if not isinstance(targets, list) or not targets:
        return None
    return [str(t) for t in targets]


def _ensure_system_consumer_targets(targets: list[str]) -> list[str]:
    """Append the mandatory system-libLLVM-consumer backends to a resolved,
    non-empty target list, preserving order and de-duplicating.

    The system libLLVM we install must carry these (AMDGPU — mesa's libgallium
    links them unconditionally) no matter which source produced the list:
    explicit ``toolchain.toml`` targets, a *cached or hand-edited*
    ``hardware_profile.toml``, or live detection. A reduced set that drops them
    bricks every EGL/GL consumer (the whole desktop) with
    ``undefined symbol: LLVMInitializeAMDGPU...``. ``derive_llvm_targets``
    already bakes the baseline into freshly-derived lists; enforcing it again
    here closes the gap for a stale profile that bypasses derivation. Logs at
    INFO when it augments a list that omitted a backend, so the situation is
    discoverable. Idempotent — safe to apply to an already-compliant list.
    """
    result = list(targets)
    added = [
        b for b in SYSTEM_LIBLLVM_CONSUMER_TARGETS if b not in result
    ]
    if added:
        result.extend(added)
        _log.info(
            f"added mandatory system-consumer LLVM target(s) {', '.join(added)} "
            f"to resolved set {list(targets)}: mesa's libgallium links them "
            "unconditionally; a reduced system libLLVM that drops them bricks "
            "the desktop"
        )
    return _ensure_installed_consumer_targets(result)


def _ensure_installed_consumer_targets(targets: list[str]) -> list[str]:
    """Append every backend an installed libLLVM consumer imports (3.5.0-B14).

    The AMDGPU baseline above covers one known consumer. Any package that calls
    ``InitializeAll*()`` (lldb, bpftool, opencl-mesa, faust) imports the init
    symbols of *every* backend its libLLVM had, and fails to load against a
    filtered one, so the filter has to keep them. Found by scanning the
    installed consumers (``toolchain_safety.consumer_required_targets``) rather
    than a list; the lockstep suite is excluded because it is rebuilt with the
    new target list in the same pass. Warns naming each consumer, because it
    overrides a target list the user may have written by hand.
    """
    required = toolchain_safety.consumer_required_targets(
        exclude=set(LLVM_LOCKSTEP_SUITE),
    )
    added = {t: pkgs for t, pkgs in required.items() if t not in targets}
    if not added:
        return targets
    # Short form, once per run (3.5.0-B27): the consumers usually import every
    # backend, so naming them per target repeated the same list ~17 times, and
    # the resolution runs several times a stage.
    consumers = sorted({p for pkgs in added.values() for p in pkgs})
    if log.first_or_changed("llvm-consumer-targets", (tuple(added), tuple(consumers))):
        _log.warn(
            f"keeping {len(added)} LLVM target(s) installed packages need "
            f"({', '.join(added)}; needed by {', '.join(consumers)}): they import "
            "these backends' init symbols. Set [llvm] targets = [] to build all "
            "targets, or remove those packages to filter further."
        )
    return [*targets, *added]


def resolve_llvm_targets(
    toolchain_toml_path: Path,
    hardware_profile_path: Path,
) -> list[str] | None:
    """Resolve the LLVM_TARGETS_TO_BUILD list for this build, or return
    None when no filtering should be applied (all targets built).

    Any non-None, non-empty result carries the mandatory system-consumer
    baseline (see :func:`_ensure_system_consumer_targets`).
    """
    explicit = _read_toolchain_targets(toolchain_toml_path)
    if explicit is _DISABLE_FILTERING:
        # User asked to disable filtering — propagate as None so the
        # patcher leaves the upstream cmake invocation untouched.
        return None
    if explicit is not None:
        return _ensure_system_consumer_targets(explicit)  # type: ignore[arg-type]
    hw = _read_hardware_targets(hardware_profile_path)
    return _ensure_system_consumer_targets(hw) if hw else hw


def _detect_llvm_targets_live() -> list[str]:
    """Run hardware detection inline (uname -m + lspci) and derive the
    LLVM target list. Returns [] on unsupported arch.

    A missing/failing ``lspci`` is non-fatal (per the module docstring) — the
    GPU backends just don't get detected, but the CPU target + the mandatory
    AMDGPU baseline still come through ``derive_llvm_targets``. Guard the binary
    being absent too (``FileNotFoundError``), not only a non-zero exit, so
    callers on a machine without pciutils don't raise."""
    try:
        lspci = run.capture(["lspci"])
        gpu_vendors = (parse_gpu_vendors(lspci.stdout)
                       if lspci is not None and lspci.returncode == 0 else [])
    except OSError:
        gpu_vendors = []
    return derive_llvm_targets(detect_host_arch(), gpu_vendors)


def resolve_or_detect_llvm_targets(
    toolchain_toml_path: Path,
    hardware_profile_path: Path,
) -> list[str] | None:
    """File-based resolution first; live hardware detection as fallback.

    Calling sites (the toolchain stage's makepkg path, the
    ``update --patch-pkgbuild`` path) shouldn't have to depend on
    ``hardware_profile.toml`` being present — ``sysforge run toolchain``
    runs alone and won't write a profile for the current state dir, and
    env-var-driven state dir resolution can disagree across invocations.
    Live detection sidesteps all of that.

    Honours an explicit ``[llvm] targets = []`` in toolchain.toml as
    "force all targets" — short-circuits before live detection so the
    user's override isn't silently downgraded to the autodetected set.
    """
    if _read_toolchain_targets(toolchain_toml_path) is _DISABLE_FILTERING:
        return None
    targets = resolve_llvm_targets(toolchain_toml_path, hardware_profile_path)
    if targets:
        return targets  # already baseline-enforced by resolve_llvm_targets
    live = _detect_llvm_targets_live()
    # _detect_llvm_targets_live goes through derive_llvm_targets, which already
    # bakes in the baseline; re-applying is idempotent and future-proofs the
    # path against a derive change.
    return _ensure_system_consumer_targets(live) if live else None
