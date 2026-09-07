# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
hardware_probe.py — host hardware detection and the derivations built on it.

The leaf-layer home for "what GPU/arch is this box, and what does that imply
for LLVM targets and mesa drivers". It was in ``pipeline/stages/hardware.py``,
which made ``llvm_targets`` and ``mesa_drivers`` — both primitives, both needing
live detection when no ``hardware_profile.toml`` exists at the current state dir
— import *upward* into a stage (3.2.0-F1c). ``stages/__init__`` eagerly
instantiates every stage, so that one import pulled the whole pipeline in behind
a driver-name lookup.

Split of responsibility:

  - ``hardware_tables.py`` — the mandatory baselines that must survive any
    reduction (``SYSTEM_LIBLLVM_CONSUMER_TARGETS``, ``MESA_MANDATORY_*``);
  - this module — detection (``detect_host_arch``, ``parse_gpu_vendors``) and
    the vendor/arch derivations that consume those baselines;
  - ``stages/hardware.py`` — the stage: kconfig emission, profile writing,
    drift reporting. It re-exports these names, so
    ``sysforge.pipeline.stages.hardware.derive_llvm_targets`` stays a valid
    import path.

Detection is pure-ish: the parsers take text, and only ``detect_host_arch``
touches the system (``uname``). Running ``lspci`` is the caller's job.
"""
from __future__ import annotations

import os
import re

from sysforge import log
from sysforge.primitives.hardware_tables import (
    MESA_MANDATORY_GALLIUM,
    MESA_MANDATORY_VULKAN,
    SYSTEM_LIBLLVM_CONSUMER_TARGETS,
)

_log = log.get_logger("HARDWARE")


# ---------------------------------------------------------------------------
# GPU detection (via lspci)
# ---------------------------------------------------------------------------

_LSPCI_VGA_RE = re.compile(
    r"(?:VGA compatible controller|3D controller|Display controller).*?:\s*(.+)",
    re.IGNORECASE,
)


def parse_gpu_vendors(lspci_text: str) -> list[str]:
    """
    Extract GPU vendor names from lspci output.
    Returns a deduplicated list of lowercase vendor tags: "amd", "nvidia",
    "intel", or "other".
    """
    seen = []
    for line in lspci_text.splitlines():
        m = _LSPCI_VGA_RE.search(line)
        if not m:
            continue
        desc = m.group(1).lower()
        if "nvidia" in desc:
            tag = "nvidia"
        elif "amd" in desc or "advanced micro" in desc or "radeon" in desc:
            tag = "amd"
        elif "intel" in desc:
            tag = "intel"
        else:
            tag = "other"
        if tag not in seen:
            seen.append(tag)
    return seen


# ---------------------------------------------------------------------------
# LLVM target / mesa driver derivation
# ---------------------------------------------------------------------------

# Host arch → LLVM CPU backend. Unknown architectures fall through and
# yield no autodetected target list — the user must override via
# toolchain.toml [llvm] targets.
_HOST_ARCH_TO_LLVM = {
    "x86_64":  "X86",
    "amd64":   "X86",
    "i686":    "X86",
    "aarch64": "AArch64",
    "arm64":   "AArch64",
    "armv7l":  "ARM",
    "armv6l":  "ARM",
    "riscv64": "RISCV",
    "ppc64le": "PowerPC",
}

# GPU vendor (as emitted by parse_gpu_vendors) → the LLVM backend that GPU's
# OWN compute path wants. Intel Mesa drivers (iris/anv) don't use an LLVM
# backend, so intel GPUs contribute no entry here. This map is NOT the whole
# story: every host also gets SYSTEM_LIBLLVM_CONSUMER_TARGETS below, so an
# intel/nvidia-only host still ends up with AMDGPU in its target set.
_GPU_VENDOR_TO_LLVM = {
    "amd":    "AMDGPU",
    "nvidia": "NVPTX",
}

# The mandatory-baseline tables (SYSTEM_LIBLLVM_CONSUMER_TARGETS,
# MESA_MANDATORY_*) live in primitives/hardware_tables.py — the resolvers that
# re-apply them at build time are primitives, and they must not import upward
# into this stage to reach them (2.6.1-F8). Imported at module top.


def detect_host_arch() -> str:
    """Return the running kernel arch as reported by uname -m."""
    return os.uname().machine


def derive_llvm_targets(host_arch: str, gpu_vendors: list[str]) -> list[str]:
    """Build the autodetected LLVM_TARGETS_TO_BUILD list for this host.

    Order: CPU backend first, then GPU backends in vendor-detection order, then
    the mandatory system-libLLVM-consumer baseline (AMDGPU — see
    ``SYSTEM_LIBLLVM_CONSUMER_TARGETS``). Returns an empty list when the host
    arch is unrecognised — callers treat empty as "no filtering" (i.e. preserve
    upstream defaults), which also keeps mesa safe because all targets get built.
    """
    cpu = _HOST_ARCH_TO_LLVM.get(host_arch)
    targets: list[str] = []
    if cpu:
        targets.append(cpu)
    else:
        _log.warn(
            f"host arch {host_arch!r} has no LLVM target mapping — "
            "llvm_targets left empty (no filtering will be applied)",
        )
        return []
    for vendor in gpu_vendors:
        backend = _GPU_VENDOR_TO_LLVM.get(vendor)
        if backend and backend not in targets:
            targets.append(backend)
    # Always carry the backends system consumers (mesa's libgallium) link, even
    # when this host's GPU wouldn't otherwise pull them in — otherwise a reduced
    # system libLLVM bricks the desktop. See SYSTEM_LIBLLVM_CONSUMER_TARGETS.
    for backend in SYSTEM_LIBLLVM_CONSUMER_TARGETS:
        if backend not in targets:
            targets.append(backend)
    return targets


# GPU vendor (as emitted by parse_gpu_vendors) → the mesa gallium / vulkan
# drivers that vendor's hardware needs. The software rasterizers are NOT here —
# they come from the mandatory baseline below so every host keeps a working
# fallback regardless of GPU vendor. This is the mesa analogue of
# _GPU_VENDOR_TO_LLVM, used to trim mesa's `gallium-drivers=all` /
# `vulkan-drivers=<every-driver>` down to what the box can actually run.
_GPU_VENDOR_TO_MESA_GALLIUM = {
    "amd":    ["radeonsi"],
    "intel":  ["iris", "crocus"],
    "nvidia": ["nouveau"],
}
_GPU_VENDOR_TO_MESA_VULKAN = {
    "amd":    ["amd"],
    "intel":  ["intel", "intel_hasvk"],
    "nvidia": ["nouveau"],
}

def derive_mesa_drivers(gpu_vendors: list[str]) -> dict[str, list[str]]:
    """Build the autodetected mesa gallium/vulkan driver lists for this host.

    Returns ``{"gallium": [...], "vulkan": [...]}``: vendor drivers (in
    detection order) first, then the mandatory software baseline
    (``MESA_MANDATORY_*``) appended and de-duplicated. Unlike
    ``derive_llvm_targets`` there is no arch gate — GPU drivers are vendor- not
    arch-determined, and the software baseline is valid on every arch. An empty
    ``gpu_vendors`` yields baseline-only (software rendering), the correct
    minimum for a headless or undetected host.
    """
    gallium: list[str] = []
    vulkan: list[str] = []
    for vendor in gpu_vendors:
        for drv in _GPU_VENDOR_TO_MESA_GALLIUM.get(vendor, []):
            if drv not in gallium:
                gallium.append(drv)
        for drv in _GPU_VENDOR_TO_MESA_VULKAN.get(vendor, []):
            if drv not in vulkan:
                vulkan.append(drv)
    for drv in MESA_MANDATORY_GALLIUM:
        if drv not in gallium:
            gallium.append(drv)
    for drv in MESA_MANDATORY_VULKAN:
        if drv not in vulkan:
            vulkan.append(drv)
    return {"gallium": gallium, "vulkan": vulkan}
