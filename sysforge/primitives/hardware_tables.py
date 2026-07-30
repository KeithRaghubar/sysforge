# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
primitives/hardware_tables.py — hardware baseline tables shared across layers.

Leaf module: it imports nothing from sysforge and must stay that way. It holds
the two *mandatory baseline* tables that both the hardware stage (at derivation
time) and the primitives that resolve a build's flags (at resolution time) have
to agree on. They live here rather than in ``pipeline/stages/hardware.py``
because the resolvers are primitives, and a primitive reaching up into a stage
for a constant drags the entire pipeline in behind it —
``pipeline/stages/__init__.py`` instantiates every stage at import
(2.6.1-F8; guarded by tests/test_module_layering.py).

The two tables are inverses of each other, and both encode the same lesson: a
*reduced* build must not drop what the rest of the system unconditionally
expects. Enforcement lives at both derivation (``hardware.derive_*``) and
resolution (``llvm_targets``/``mesa_drivers``), because a cached or hand-edited
hardware_profile.toml bypasses derivation entirely.
"""
from __future__ import annotations

# Targets the *system* libLLVM must always carry because installed system
# packages link them regardless of this host's GPU. Arch's mesa references the
# AMDGPU (radeonsi) and host-CPU (llvmpipe) target-init symbols from libgallium
# UNCONDITIONALLY — they are compiled in whatever GPU you own. If the toolchain
# stage rebuilds system llvm-libs with a reduced LLVM_TARGETS_TO_BUILD that drops
# AMDGPU, mesa — and therefore every EGL/GL consumer, i.e. the whole desktop —
# fails to load with `undefined symbol: LLVMInitializeAMDGPU...`. So AMDGPU is
# mandatory in any non-empty autodetected set, even on nvidia/intel-only hosts.
# (The host CPU backend is already supplied from hardware._HOST_ARCH_TO_LLVM.)
# Guards against reducing too LITTLE.
SYSTEM_LIBLLVM_CONSUMER_TARGETS = ("AMDGPU",)

# Mesa drivers that must always be built regardless of detected GPU — the
# *inverse* of the LLVM AMDGPU invariant. Where SYSTEM_LIBLLVM_CONSUMER_TARGETS
# guards against reducing too LITTLE, this guards against reducing too MUCH:
# dropping the software rasterizers (gallium llvmpipe/softpipe, vulkan
# swrast=lavapipe) would break headless sessions, VMs, GPU-reset recovery and
# the llvmpipe/software-Vulkan fallback. zink (GL-on-Vulkan) rides along as the
# portability path some stacks fall back to. Always present in any non-empty
# autodetected set, even when no GPU is detected at all.
MESA_MANDATORY_GALLIUM = ("llvmpipe", "softpipe", "zink")
MESA_MANDATORY_VULKAN = ("swrast",)
