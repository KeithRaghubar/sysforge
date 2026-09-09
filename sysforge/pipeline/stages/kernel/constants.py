# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/constants.py — fixed values and the canonical recovery command.

The valid-value tuples are here rather than beside their readers because they
are the *contract* with kernel.toml, not an implementation detail of whichever
resolver happens to check them first.
"""

from sysforge.primitives.source_sync import STATUS_FAILED
from sysforge.primitives.source_sync import STATUS_FROZEN
from sysforge.primitives.source_sync import STATUS_PURGE_REFUSED
from sysforge.primitives.source_sync import STATUS_RATE_LIMITED

from sysforge import log

_log = log.get_logger("KERNEL")


SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED, STATUS_FROZEN,
})


def kernel_recovery_command():
    """Canonical shell command to restore boot consistency after an interrupted
    kernel install. Always regenerates the initramfs — that's the step whose
    absence makes the system unbootable. Bootloader regen is omitted because
    the sentinel uses naive ``cmd.split()`` (no shell operators) and most
    bootloaders pick up new kernel images automatically once the initramfs is
    present; the operator can re-run ``sysforge run kernel`` for a full
    bootloader-aware regen.
    """
    return "sudo mkinitcpio -P"

VALID_COMPILERS = ("gcc", "llvm")
VALID_BOOTLOADERS = ("systemd-boot", "grub", "none")
VALID_SOURCES = ("local", "repo", "aur")
