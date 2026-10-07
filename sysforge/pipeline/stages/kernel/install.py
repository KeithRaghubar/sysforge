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

from sysforge.primitives import boot_entries
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
    result = subprocess.run(privileged_argv(["mkinitcpio", "-P"]))  # noqa: TID251 — privileged; mkinitcpio output streams, status inspected
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
    result = subprocess.run(cmd)  # noqa: TID251 — privileged bootloader update streams, status inspected
    if result.returncode != 0:
        _log.warn(
            f"{label} exited {result.returncode} — bootloader may already be current; "
            "kernel is installed, continuing",
        )


# ---------------------------------------------------------------------------


# Managed systemd-boot entries (3.3.0-F5)
# ---------------------------------------------------------------------------


def fdo_role(fdo_mode, propeller):
    """The boot-entry role (title) for this kernel build."""
    if fdo_mode == "record":
        return "profiling"
    if fdo_mode == "use":
        return "propeller" if propeller else "autofdo"
    return "plain"


def _load_or_raise():
    try:
        return boot_entries.load_entries(boot_entries.entries_dir())
    except OSError as e:
        raise RuntimeError(
            f"[KERNEL] cannot read {boot_entries.entries_dir()}: {e} — is the boot "
            'partition mounted? (or set boot_entries = "off" in kernel.toml)'
        ) from e


def preflight_boot_entries(pkgname, *, manage):
    """Pre-build: refuse early if managed entries cannot be produced (3.3.0-F5).

    Returns the resolved template (None when entries are not managed).
    """
    if not manage:
        return None
    try:
        boot_entries.check_boot_path()
        return boot_entries.resolve_template(
            _load_or_raise(), selected=boot_entries.read_selected_entry(), pkgname=pkgname)
    except boot_entries.BootEntryError as e:
        raise RuntimeError(f"[KERNEL] {e}") from e


def prune_boot_entries(*, manage, dry_run):
    """Drop sysforge-managed entries whose kernel image is gone."""
    if not manage:
        return
    try:
        entries = boot_entries.load_entries(boot_entries.entries_dir())
    except OSError:
        return
    plan = boot_entries.plan_prune(entries, boot_entries.existing_images())
    for line in boot_entries.apply_plan(plan, dry_run=dry_run):
        _log.ui(line)
    for name in plan.deletes:
        _log.info(f"pruned stale boot entry {name}")


def sync_boot_entries(pkgname, kernel, role, version, *, manage, prefix, dry_run):
    """Post-install: write this kernel's entry and re-render every managed entry."""
    if not manage:
        return
    entries = _load_or_raise()
    try:
        template = boot_entries.resolve_template(
            entries, selected=boot_entries.read_selected_entry(), pkgname=pkgname)
    except boot_entries.BootEntryError as e:
        raise RuntimeError(f"[KERNEL] {e}") from e
    images = boot_entries.existing_images()
    if dry_run:
        images = images | {kernel}  # not installed in a dry run
    plan = boot_entries.plan_sync(
        entries, template=template,
        wanted=[boot_entries.WantedKernel(kernel, role, version)],
        existing_images=images, prefix=prefix,
        pretty_name=boot_entries.read_pretty_name(),
    )
    for line in plan.info:
        _log.info(line)
    for line in plan.notes:
        _log.ui(line)
    for line in boot_entries.apply_plan(plan, dry_run=dry_run):
        _log.ui(line)
    for name, _ in plan.writes:
        _log.info(f"boot entry {name} (template {template.entry.filename})")
    if kernel in plan.skipped:
        want = boot_entries.entry_filename(
            kernel, boot_entries.effective_prefix(template, prefix))
        note = next(
            (n for n in plan.notes if n.startswith(f"cannot write {want}:")),
            f"cannot write {want}",
        )
        raise RuntimeError(f"[KERNEL] {note}")
