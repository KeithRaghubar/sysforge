"""
stages/partition.py — stage 1: disk partitioning

Partitions a block device and mounts it ready for pacstrap.

Layout (GPT):
  Partition 1 — EFI System Partition (ESP), fat32, mounted at <target>/boot
  Partition 2 — root, ext4 or btrfs, mounted at <target>

Reads device, esp_size_mib, root_fs, and target from bootstrap.toml.
Prompts for confirmation before any destructive operation (unless --dry-run).

Requires sgdisk (gptfdisk), mkfs.fat, mkfs.ext4 / mkfs.btrfs, and mount.
"""

import subprocess
from pathlib import Path

from sysforge import log
_log = log.get_logger("PARTITION")
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import BootstrapConfig, load_bootstrap
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.run import run_or_raise


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def _check_tools(root_fs: str) -> None:
    missing = []
    for tool in ["sgdisk", "mkfs.fat", "mount", "findmnt"]:
        result = subprocess.run(["which", tool], capture_output=True)
        if result.returncode != 0:
            missing.append(tool)

    mkfs_root = "mkfs.ext4" if root_fs == "ext4" else "mkfs.btrfs"
    result = subprocess.run(["which", mkfs_root], capture_output=True)
    if result.returncode != 0:
        missing.append(mkfs_root)

    if missing:
        raise RuntimeError(
            f"[PARTITION] Required tools not found: {', '.join(missing)}. "
            f"Install gptfdisk, dosfstools, and {'e2fsprogs' if root_fs == 'ext4' else 'btrfs-progs'}."
        )


def _check_device(device: str) -> None:
    if not Path(device).exists():
        raise RuntimeError(
            f"[PARTITION] Device {device!r} not found. "
            f"Check partition.device in bootstrap.toml."
        )


def _needs_p_separator(device: str) -> bool:
    """Return True if partition paths use a 'p' separator (e.g. nvme0n1p1)."""
    return "nvme" in device or "mmcblk" in device or "loop" in device or "md" in device


def _root_partition(device: str) -> str:
    """Derive the root partition path from the block device path."""
    if _needs_p_separator(device):
        return f"{device}p2"
    return f"{device}2"


def _esp_partition(device: str) -> str:
    """Derive the ESP partition path from the block device path."""
    if _needs_p_separator(device):
        return f"{device}p1"
    return f"{device}1"


def _mounted_at(source: str, mountpoint: str) -> bool:
    """Return True if `source` is mounted exactly at `mountpoint`."""
    result = subprocess.run(
        ["findmnt", "--source", source, "--mountpoint", mountpoint, "--noheadings"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _is_already_mounted(device: str, target: str) -> bool:
    """
    Return True iff *both* the root partition is mounted at target and the ESP
    is mounted at target/boot. A partial mount (root only) raises so the user
    can unmount and rerun cleanly — silently skipping in that state would
    cause later stages (bootloader install, etc.) to fail confusingly.
    """
    root_mounted = _mounted_at(_root_partition(device), target)
    esp_mounted = _mounted_at(_esp_partition(device), f"{target}/boot")

    if root_mounted and esp_mounted:
        return True
    if root_mounted ^ esp_mounted:
        which = "root" if root_mounted else "ESP"
        raise RuntimeError(
            f"[PARTITION] Partial mount detected ({which} only). "
            f"Unmount {target} and rerun, or pass --start-from to skip partitioning entirely."
        )
    return False


def _check_not_mounted(device: str, target: str) -> None:
    """Raise if the device or target is already in use by a different device."""
    result = subprocess.run(
        ["findmnt", "--source", device, "--noheadings"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        raise RuntimeError(
            f"[PARTITION] {device} is already mounted. Unmount it before running this stage."
        )

    result2 = subprocess.run(
        ["findmnt", "--mountpoint", target, "--noheadings"],
        capture_output=True, text=True,
    )
    if result2.stdout.strip():
        raise RuntimeError(
            f"[PARTITION] {target} already has something mounted. Unmount it first."
        )


# ---------------------------------------------------------------------------
# Confirmation prompt
# ---------------------------------------------------------------------------

def _confirm(cfg: BootstrapConfig) -> None:
    """Print the partition plan and require explicit confirmation."""
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  SysForge — Partition plan                              │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print(f"  │  Device : {cfg.device:<47}│")
    print(f"  │  Target : {cfg.target:<47}│")
    print("  ├───────┬────────────┬──────────┬──────────────────────────┤")
    print("  │  Part │ Size       │ FS       │ Mount                    │")
    print("  ├───────┼────────────┼──────────┼──────────────────────────┤")
    _size1 = f" {cfg.esp_size_mib} MiB"
    _boot  = f"{cfg.target}/boot"
    print(f"  │  1    │{_size1:<12}│ fat32    │ {_boot:<25}│")
    print(f"  │  2    │ remaining  │ {cfg.root_fs:<8} │ {cfg.target:<24} │")
    print("  └───────┴────────────┴──────────┴──────────────────────────┘")
    print()
    print(f"  WARNING: All data on {cfg.device} will be destroyed.")
    print()

    # Destructive: any non-confirming input must abort, never re-prompt.
    answer = prompt_choice(
        "  Type 'yes' to proceed, anything else to abort: ",
        choices=("yes",),
        default="",
        eof_default="",
        retry_on_invalid=False,
        tag="PARTITION",
    )
    if answer != "yes":
        raise RuntimeError("[PARTITION] Aborted by user.")


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

def _partition_disk(cfg: BootstrapConfig) -> tuple[str, str]:
    """
    Create GPT table with ESP + root partitions using sgdisk.
    Returns (esp_partition, root_partition) device paths.
    """
    device = cfg.device

    _log.ui(f"Partitioning {device} (GPT)")

    # Single sgdisk call: --clear replaces --zap-all, +NMiB is relative sizing
    # (avoids ambiguous absolute-with-unit syntax), 0:0 = first-free:last-free.
    cmd = [
        "sgdisk",
        "--clear",
        f"--new=1:1MiB:+{cfg.esp_size_mib}MiB", "--typecode=1:ef00", "--change-name=1:ESP",
        "--new=2:0:0",                            "--typecode=2:8300", "--change-name=2:root",
        device,
    ]

    _log.info(f"Running: {' '.join(cmd)}")
    run_or_raise(cmd, tag="PARTITION", operation="sgdisk", hint=" ".join(cmd))

    # Inform kernel of partition table changes
    probe = subprocess.run(["partprobe", device], capture_output=True, text=True)
    if probe.returncode != 0:
        _log.warn(
            f"partprobe failed (exit {probe.returncode}) — kernel may not see "
            f"new partitions yet: {probe.stderr.strip()}"
        )

    # Derive partition device paths
    # Handles both /dev/sda → /dev/sda1 and /dev/nvme0n1 → /dev/nvme0n1p1
    if _needs_p_separator(device):
        esp_part  = f"{device}p1"
        root_part = f"{device}p2"
    else:
        esp_part  = f"{device}1"
        root_part = f"{device}2"

    # Verify partition device nodes appeared
    for part in (esp_part, root_part):
        if not Path(part).exists():
            raise RuntimeError(
                f"[PARTITION] Expected partition device {part!r} not found after sgdisk. "
                f"partprobe may have failed or the kernel is slow to detect new partitions."
            )

    _log.ui(f"ESP:  {esp_part}")
    _log.ui(f"Root: {root_part}")
    return esp_part, root_part


def _format_partitions(cfg: BootstrapConfig, esp_part: str, root_part: str) -> None:
    """Format ESP as fat32 and root as the configured filesystem."""
    _log.ui(f"Formatting {esp_part} as fat32")
    run_or_raise(
        ["mkfs.fat", "-F", "32", "-n", "ESP", esp_part],
        tag="PARTITION", operation="mkfs.fat",
        hint=f"failed on {esp_part}", capture=False,
    )

    _log.ui(f"Formatting {root_part} as {cfg.root_fs}")
    mkfs_cmd = (
        ["mkfs.ext4", "-L", "root", root_part] if cfg.root_fs == "ext4"
        else ["mkfs.btrfs", "-L", "root", root_part]
    )
    run_or_raise(
        mkfs_cmd, tag="PARTITION", operation=f"mkfs.{cfg.root_fs}",
        hint=f"failed on {root_part}", capture=False,
    )


def _mount_partitions(cfg: BootstrapConfig, esp_part: str, root_part: str) -> None:
    """Mount root to target and ESP to target/boot."""
    target = cfg.target
    boot = f"{target}/boot"

    _log.ui(f"Mounting {root_part} → {target}")
    Path(target).mkdir(parents=True, exist_ok=True)
    run_or_raise(
        ["mount", root_part, target], tag="PARTITION", operation="mount",
        hint=f"{root_part} → {target}",
    )

    _log.ui(f"Mounting {esp_part} → {boot}")
    Path(boot).mkdir(parents=True, exist_ok=True)
    run_or_raise(
        ["mount", "--mkdir", esp_part, boot], tag="PARTITION", operation="mount",
        hint=f"{esp_part} → {boot}",
    )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class PartitionStage(Stage):
    name = "partition"
    description = "Disk partitioning — GPT, ESP + root, mount"
    depends_on = []

    def run(self, config, state, options):  # noqa: ARG002
        cfg = load_bootstrap()

        if options.dry_run:
            _log.ui(f"[dry-run] device:      {cfg.device}")
            _log.ui(f"[dry-run] target:      {cfg.target}")
            _log.ui(f"[dry-run] ESP:         {cfg.esp_size_mib} MiB, fat32")
            _log.ui(f"[dry-run] root:        remaining, {cfg.root_fs}")
            return

        _check_tools(cfg.root_fs)
        _check_device(cfg.device)

        if _is_already_mounted(cfg.device, cfg.target):
            _log.ui(f"{cfg.device} already partitioned and mounted at {cfg.target} — skipping.")
            return

        _check_not_mounted(cfg.device, cfg.target)

        _confirm(cfg)

        esp_part, root_part = _partition_disk(cfg)
        _format_partitions(cfg, esp_part, root_part)
        _mount_partitions(cfg, esp_part, root_part)

        _log.ui(f"Disk ready. {cfg.device} mounted at {cfg.target}.")
