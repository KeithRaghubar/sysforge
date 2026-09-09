# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/fdo.py — sample-based feedback-directed optimization.

AutoFDO and Propeller, orchestrated through ``primitives/kernel_fdo.py``. Both
need the LLVM toolchain, so the gate here is a hard precondition rather than a
warning: a sample-based profile collected under one compiler is not applicable
to the other, and applying it silently would produce a kernel optimized against
the wrong codegen.
"""
from pathlib import Path
import os

from sysforge.primitives import kernel_fdo

from sysforge import log

_log = log.get_logger("KERNEL")


# Sample-based FDO (AutoFDO / Propeller) — kernel_fdo orchestration
#
# Three steps spanning reboots: `record` builds a profiling kernel
# (CONFIG_AUTOFDO_CLANG=y, stock name); `capture` is a read-only step that prints
# host-tailored perf + create_llvm_prof commands; `use` rebuilds consuming the
# collected profile (injected via the build's extra_env make-variables) and earns
# the -sysforge coexist rename. LLVM-only — there is no GCC path. See
# DESIGN.md §Kernel stage.
# ---------------------------------------------------------------------------


def resolve_fdo(options):
    """Resolve the kernel FDO request from CLI options.

    Returns ``(mode, propeller)`` — ``mode`` is one of ``kernel_fdo.VALID_MODES``
    (``record``/``capture``/``use``) or ``None`` when no FDO flag was passed.
    ``--propeller`` is a modifier that layers Propeller on the AutoFDO cycle, so
    it requires a mode. Raises on an invalid combination.
    """
    mode = getattr(options, "kernel_fdo", None)
    propeller = bool(getattr(options, "kernel_propeller", False))
    if mode is not None and mode not in kernel_fdo.VALID_MODES:
        raise RuntimeError(
            f"[KERNEL] invalid --autofdo value {mode!r}: must be one of "
            f"{kernel_fdo.VALID_MODES}"
        )
    if propeller and mode is None:
        raise RuntimeError(
            "[KERNEL] --propeller layers Propeller on the AutoFDO cycle and "
            "requires --autofdo=record|capture|use"
        )
    return mode, propeller


def fdo_is_llvm(compiler, cc):
    """Whether the resolved kernel compiler is Clang/LLVM (kernel FDO is LLVM-only).

    ``compiler`` is the explicit "gcc"/"llvm" choice (CLI/kernel.toml/pipeline);
    when it is ``None`` (inherited toolchain) we sniff the resolved ``cc`` and,
    failing that, the environment ``CC`` — the same basename the kernel build
    keys its LLVM=1 detection on, so a workstation whose ``CC=clang`` is honored.
    """
    if compiler == "llvm":
        return True
    if compiler == "gcc":
        return False
    probe = cc or os.environ.get("CC", "")
    return bool(probe) and Path(probe).name.startswith("clang")


def gate_fdo_llvm(mode, propeller, compiler, cc):
    """Hard-abort when kernel FDO is requested under a non-LLVM toolchain.

    AutoFDO/Propeller have no GCC path in sysforge, and the kernel's
    CONFIG_AUTOFDO_CLANG/CONFIG_PROPELLER_CLANG have no GCC equivalent at all — so
    this is a clean refusal before any build work. The single home for the
    kernel-FDO LLVM gate.
    """
    if fdo_is_llvm(compiler, cc):
        return
    from sysforge.primitives.profile import LLVM_REQUIRED_HINT
    feature = "kernel Propeller" if propeller else "kernel AutoFDO"
    raise RuntimeError(
        f"[KERNEL] {feature} (--autofdo={mode}) requires the LLVM toolchain. "
        f"{LLVM_REQUIRED_HINT}"
    )


def run_fdo_capture(pkgname, propeller, dry_run):
    """The ``--autofdo=capture`` step: print host-tailored perf + create_llvm_prof
    commands for the operator to run on the booted profiling kernel.

    Read-only and non-mutating — no build, no install, no sentinel. Provisions
    the store so the printed ``create_llvm_prof --out=<store>/…`` can write, then
    resolves this host's branch-sampling event and the matching vmlinux and
    prints the command block. ``--autofdo=use`` later consumes whatever profile
    landed in the store.
    """
    store = kernel_fdo.resolve_store(pkgname, propeller=propeller)
    sampling = kernel_fdo.detect_branch_sampling()
    # The profiling kernel is built+installed under its coexist record name
    # (F26), so its build tree — and the vmlinux create_llvm_prof needs — lives
    # under that name, not the stock pkgname. The store stays keyed on the stock
    # pkgname (record/capture/use share it).
    record_name = kernel_fdo.record_pkgname(pkgname)
    vmlinux = kernel_fdo.resolve_vmlinux(record_name)

    if dry_run:
        _log.ui(
            f"[dry-run] would print AutoFDO capture commands for {pkgname} "
            f"(store {store})"
        )
        return

    from sysforge.primitives import fs_provision
    try:
        fs_provision.ensure_writable_dir(store)
    except fs_provision.FsProvisionError as e:
        _log.warn(
            f"FDO store {store} could not be group-provisioned ({e}) — "
            "create_llvm_prof may be unable to write the profile there"
        )

    if sampling.supported:
        _log.info(f"branch sampling: {sampling.note}")
    else:
        _log.warn(f"branch sampling unsupported on this CPU: {sampling.note}")
    if vmlinux is None:
        _log.warn(
            "no uncompressed vmlinux found in the build tree — build the "
            "profiling kernel with `--autofdo=record` first, then substitute the "
            "real vmlinux path in the command below."
        )

    _log.ui(
        f"AutoFDO{' + Propeller' if propeller else ''} capture — reboot into "
        f"{record_name}, then run these while exercising the machine:"
    )
    for line in kernel_fdo.capture_commands(
        store, sampling=sampling, vmlinux=vmlinux, propeller=propeller
    ):
        _log.ui(f"  {line}")
    _log.ui(
        "Then rebuild the optimized kernel: "
        f"sysforge run kernel --autofdo=use{' --propeller' if propeller else ''}"
    )


# ---------------------------------------------------------------------------
# kernel.toml loading
# ---------------------------------------------------------------------------
