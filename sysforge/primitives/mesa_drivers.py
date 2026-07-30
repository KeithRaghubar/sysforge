# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
mesa_drivers.py — resolve the mesa gallium/vulkan driver lists for a build.

The mesa analogue of :mod:`sysforge.primitives.llvm_targets`. Used by
``pkgbuild_patcher.patch_mesa_drivers`` to trim mesa's
``-D gallium-drivers=all`` / ``-D vulkan-drivers=<every-driver>`` meson options
down to the drivers this host actually runs, the way the LLVM path trims
``LLVM_TARGETS_TO_BUILD``.

**Opt-in.** Unlike LLVM target filtering (which is always on once hardware is
detected), mesa filtering ships OFF and is gated by a master switch —
``[mesa] filter_drivers = true`` in sysforge.toml. With the switch off (the
default), every resolver returns ``None`` ("no filtering, build all drivers")
and the build is byte-identical to upstream.

Resolution order, per axis (gallium / vulkan), once the switch is on:
  1. ``[mesa] gallium`` / ``[mesa] vulkan`` in sysforge.toml — explicit
     override list. A *non-empty* list is used as-is; an empty/absent list
     falls through (empty is NOT "build all" here — the master switch is the
     on/off control, so an empty override just means "autodetect this axis").
  2. ``hardware_profile.toml [hardware] mesa_gallium_drivers`` /
     ``mesa_vulkan_drivers`` — autodetected from lspci by the hardware stage.
  3. ``resolve_or_detect_*`` only: live hardware detection (lspci) via
     ``hardware.derive_mesa_drivers`` — so the patcher works when no
     hardware_profile.toml exists at the resolved state dir.
  4. Nothing resolved → ``None`` (no filtering).

Invariant (any non-None result): the mandatory software baseline
(``hardware_tables.MESA_MANDATORY_GALLIUM`` — llvmpipe/softpipe/zink — and
``MESA_MANDATORY_VULKAN`` — swrast/lavapipe) is always present, the *inverse*
of the LLVM AMDGPU invariant. ``derive_mesa_drivers`` bakes it into freshly
derived lists; ``_ensure_mesa_software_baseline`` re-applies it here so a cached
or hand-edited ``hardware_profile.toml`` — or an explicit ``[mesa]`` override
that omits a software driver — can't ship a mesa that has no working software
fallback (headless / VM / GPU-reset recovery would break).

Public API:
    resolve_mesa_drivers(sysforge_toml_path, hardware_profile_path)
        -> dict[str, list[str]] | None
    resolve_or_detect_mesa_drivers(sysforge_toml_path, hardware_profile_path)
        -> dict[str, list[str]] | None
"""
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.hardware_tables import (
    MESA_MANDATORY_GALLIUM,
    MESA_MANDATORY_VULKAN,
)

_log = log.get_logger("MESA")


def _read_switch_and_overrides(path: Path):
    """Return ``(enabled, gallium_override, vulkan_override)`` from sysforge.toml.

    ``enabled`` is the master ``[mesa] filter_drivers`` switch (default False).
    Each override is a ``list[str]`` when the key holds a non-empty list, else
    None. A missing file/section yields ``(False, None, None)`` so filtering
    stays off.
    """
    if not path.is_file():
        return (False, None, None)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warn(f"failed to read {path}: {e} — mesa driver filtering disabled")
        return (False, None, None)
    section = data.get("mesa")
    if not isinstance(section, dict):
        return (False, None, None)
    enabled = section.get("filter_drivers") is True

    def _override(key):
        val = section.get(key)
        if not isinstance(val, list) or not val:
            return None
        return [str(v) for v in val]

    return (enabled, _override("gallium"), _override("vulkan"))


def _read_hardware_drivers(path: Path):
    """Return ``(gallium, vulkan)`` from hardware_profile.toml, each a
    ``list[str]`` or None when absent/empty."""
    if not path.is_file():
        return (None, None)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return (None, None)
    section = data.get("hardware", {})

    def _field(key):
        val = section.get(key)
        if not isinstance(val, list) or not val:
            return None
        return [str(v) for v in val]

    return (_field("mesa_gallium_drivers"), _field("mesa_vulkan_drivers"))


def _ensure_mesa_software_baseline(
    gallium: list[str], vulkan: list[str]
) -> dict[str, list[str]]:
    """Append the mandatory software drivers to resolved gallium/vulkan lists,
    preserving order and de-duplicating.

    The inverse of ``llvm_targets._ensure_system_consumer_targets``: where that
    stops a libLLVM from dropping a backend mesa needs, this stops a mesa build
    from dropping the software rasterizers every host needs as a fallback
    (gallium llvmpipe/softpipe/zink, vulkan swrast=lavapipe). Enforced no matter
    which source produced the list — explicit override, cached profile, or live
    detection. Logs at INFO when it augments a list that omitted a driver, so a
    too-aggressive override is discoverable. Idempotent.
    """
    def _augment(resolved: list[str], mandatory, axis: str) -> list[str]:
        result = list(resolved)
        added = [d for d in mandatory if d not in result]
        if added:
            result.extend(added)
            _log.info(
                f"added mandatory software {axis} driver(s) {', '.join(added)} "
                f"to resolved set {list(resolved)} — mesa needs a software "
                "fallback (headless / VM / GPU-reset recovery); a reduced build "
                "that drops them leaves no working renderer"
            )
        return result

    return {
        "gallium": _augment(gallium, MESA_MANDATORY_GALLIUM, "gallium"),
        "vulkan": _augment(vulkan, MESA_MANDATORY_VULKAN, "vulkan"),
    }


def resolve_mesa_drivers(
    sysforge_toml_path: Path,
    hardware_profile_path: Path,
) -> dict[str, list[str]] | None:
    """Resolve the mesa gallium/vulkan driver lists for this build from files
    only, or return None when no filtering should be applied.

    Returns None when the ``[mesa] filter_drivers`` switch is off, or when an
    axis can't be resolved from override/profile (the caller's
    ``resolve_or_detect_*`` then tries live detection). Any non-None result
    carries the mandatory software baseline.
    """
    enabled, gallium_override, vulkan_override = _read_switch_and_overrides(
        sysforge_toml_path
    )
    if not enabled:
        return None
    hw_gallium, hw_vulkan = _read_hardware_drivers(hardware_profile_path)
    gallium = gallium_override if gallium_override is not None else hw_gallium
    vulkan = vulkan_override if vulkan_override is not None else hw_vulkan
    if not gallium or not vulkan:
        # An axis is undetermined — defer to live detection in the caller rather
        # than ship a half-resolved (and possibly baseline-only) reduction.
        return None
    return _ensure_mesa_software_baseline(gallium, vulkan)


def _detect_mesa_drivers_live() -> dict[str, list[str]]:
    """Run GPU detection inline (lspci) and derive the mesa driver lists.

    A missing/failing ``lspci`` is non-fatal — no vendor drivers get detected,
    but ``derive_mesa_drivers`` still returns the mandatory software baseline.
    """
    from sysforge.pipeline.stages.hardware import (
        derive_mesa_drivers,
        parse_gpu_vendors,
    )
    try:
        lspci = subprocess.run(["lspci"], capture_output=True, text=True)
        gpu_vendors = parse_gpu_vendors(lspci.stdout) if lspci.returncode == 0 else []
    except (FileNotFoundError, OSError):
        gpu_vendors = []
    return derive_mesa_drivers(gpu_vendors)


def resolve_or_detect_mesa_drivers(
    sysforge_toml_path: Path,
    hardware_profile_path: Path,
) -> dict[str, list[str]] | None:
    """File-based resolution first; live hardware detection as fallback.

    Returns None when ``[mesa] filter_drivers`` is off (short-circuits before
    any detection) — so the master switch alone controls whether mesa is ever
    patched. Live detection covers the common case where the hardware stage
    hasn't written a profile at the current state dir.
    """
    enabled, gallium_override, vulkan_override = _read_switch_and_overrides(
        sysforge_toml_path
    )
    if not enabled:
        return None
    resolved = resolve_mesa_drivers(sysforge_toml_path, hardware_profile_path)
    if resolved is not None:
        return resolved
    # Fall back to live detection, then re-apply any explicit per-axis override
    # on top (override beats autodetect) and re-enforce the baseline.
    live = _detect_mesa_drivers_live()
    gallium = gallium_override if gallium_override is not None else live["gallium"]
    vulkan = vulkan_override if vulkan_override is not None else live["vulkan"]
    return _ensure_mesa_software_baseline(gallium, vulkan)
