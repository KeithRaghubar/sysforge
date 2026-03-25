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

import sysforge.log as _log
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import BootstrapConfig, load_bootstrap


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


def _root_partition(device: str) -> str:
    """Derive the root partition path from the block device path."""
    if "nvme" in device or "mmcblk" in device:
        return f"{device}p2"
    return f"{device}2"


def _is_already_mounted(device: str, target: str) -> bool:
    """
    Return True if a partition of device is already mounted at target.
    This indicates the partition stage already ran — safe to skip on resume.
    Uses --mountpoint for an exact match so the ISO root overlayfs is not
    mistaken for a mount at /mnt.
    """
    root_part = _root_partition(device)
    result = subprocess.run(
        ["findmnt", "--source", root_part, "--mountpoint", target, "--noheadings"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


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

    try:
        answer = input("  Type 'yes' to proceed, anything else to abort: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""

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

    _log.ui("[PARTITION]", f"Partitioning {device} (GPT)")

    # Single sgdisk call: --clear replaces --zap-all, +NMiB is relative sizing
    # (avoids ambiguous absolute-with-unit syntax), 0:0 = first-free:last-free.
    cmd = [
        "sgdisk",
        "--clear",
        f"--new=1:1MiB:+{cfg.esp_size_mib}MiB", "--typecode=1:ef00", "--change-name=1:ESP",
        "--new=2:0:0",                            "--typecode=2:8300", "--change-name=2:root",
        device,
    ]

    _log.info("[PARTITION]", f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"[PARTITION] sgdisk failed (exit {result.returncode}): {' '.join(cmd)}"
        )

    # Inform kernel of partition table changes
    subprocess.run(["partprobe", device], capture_output=True)

    # Derive partition device paths
    # Handles both /dev/sda → /dev/sda1 and /dev/nvme0n1 → /dev/nvme0n1p1
    if "nvme" in device or "mmcblk" in device:
        esp_part  = f"{device}p1"
        root_part = f"{device}p2"
    else:
        esp_part  = f"{device}1"
        root_part = f"{device}2"

    _log.ui("[PARTITION]", f"ESP:  {esp_part}")
    _log.ui("[PARTITION]", f"Root: {root_part}")
    return esp_part, root_part


def _format_partitions(cfg: BootstrapConfig, esp_part: str, root_part: str) -> None:
    """Format ESP as fat32 and root as the configured filesystem."""
    _log.ui("[PARTITION]", f"Formatting {esp_part} as fat32")
    result = subprocess.run(["mkfs.fat", "-F", "32", "-n", "ESP", esp_part])
    if result.returncode != 0:
        raise RuntimeError(f"[PARTITION] mkfs.fat failed on {esp_part}")

    _log.ui("[PARTITION]", f"Formatting {root_part} as {cfg.root_fs}")
    if cfg.root_fs == "ext4":
        result = subprocess.run(["mkfs.ext4", "-L", "root", root_part])
    else:  # btrfs
        result = subprocess.run(["mkfs.btrfs", "-L", "root", root_part])

    if result.returncode != 0:
        raise RuntimeError(
            f"[PARTITION] mkfs.{cfg.root_fs} failed on {root_part}"
        )


def _mount_partitions(cfg: BootstrapConfig, esp_part: str, root_part: str) -> None:
    """Mount root to target and ESP to target/boot."""
    target = cfg.target
    boot = f"{target}/boot"

    _log.ui("[PARTITION]", f"Mounting {root_part} → {target}")
    Path(target).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["mount", root_part, target])
    if result.returncode != 0:
        raise RuntimeError(f"[PARTITION] mount failed: {root_part} → {target}")

    _log.ui("[PARTITION]", f"Mounting {esp_part} → {boot}")
    Path(boot).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["mount", "--mkdir", esp_part, boot])
    if result.returncode != 0:
        raise RuntimeError(f"[PARTITION] mount failed: {esp_part} → {boot}")


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
            _log.ui("[PARTITION]", f"[dry-run] device:      {cfg.device}")
            _log.ui("[PARTITION]", f"[dry-run] target:      {cfg.target}")
            _log.ui("[PARTITION]", f"[dry-run] ESP:         {cfg.esp_size_mib} MiB, fat32")
            _log.ui("[PARTITION]", f"[dry-run] root:        remaining, {cfg.root_fs}")
            return

        _check_tools(cfg.root_fs)
        _check_device(cfg.device)

        if _is_already_mounted(cfg.device, cfg.target):
            _log.ui("[PARTITION]", f"{cfg.device} already partitioned and mounted at {cfg.target} — skipping.")
            return

        _check_not_mounted(cfg.device, cfg.target)

        _confirm(cfg)

        esp_part, root_part = _partition_disk(cfg)
        _format_partitions(cfg, esp_part, root_part)
        _mount_partitions(cfg, esp_part, root_part)

        _log.ui("[PARTITION]", f"Disk ready. {cfg.device} mounted at {cfg.target}.")
