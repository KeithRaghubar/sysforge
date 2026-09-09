# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/install.py — the post-install steps that make a kernel bootable.

Regenerating the initramfs and updating the bootloader. Small, but the ordering
is the whole point: an installed kernel image with no matching initramfs is an
unbootable system, so this is also where the recovery command that repairs an
interrupted install is defined.
"""
import subprocess

from sysforge.primitives.privilege import privileged_argv

from sysforge import log

_log = log.get_logger("KERNEL")


# Post-install steps
# ---------------------------------------------------------------------------


def run_mkinitcpio(dry_run):
    """Regenerate all initramfs presets."""
    if dry_run:
        _log.ui("[dry-run] would run: sudo mkinitcpio -P")
        return
    _log.info("Running mkinitcpio -P")
    result = subprocess.run(privileged_argv(["mkinitcpio", "-P"]))
    if result.returncode != 0:
        raise RuntimeError(f"[KERNEL] mkinitcpio -P failed (exit {result.returncode})")


def update_bootloader(bootloader, dry_run):
    """Update the bootloader config to pick up the new kernel."""
    if bootloader == "none" or not bootloader:
        _log.info("Bootloader update skipped (bootloader = 'none')")
        return

    if bootloader == "grub":
        cmd = privileged_argv(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
        label = "grub-mkconfig"
    elif bootloader == "systemd-boot":
        cmd = privileged_argv(["bootctl", "update"])
        label = "bootctl update"
    else:
        _log.warn(f"Unknown bootloader {bootloader!r} — skipping update")
        return

    if dry_run:
        _log.ui(f"[dry-run] would run: {' '.join(cmd)}")
        return

    _log.info(f"Updating bootloader: {label}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _log.warn(
            f"{label} exited {result.returncode} — bootloader may already be current; "
            "kernel is installed, continuing",
        )


# ---------------------------------------------------------------------------
