# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
snapshot.py — optional pre-build btrfs snapshot (opt-in, read-only detection).

Sanctioned by docs/design/20-scope.md: "an optional pre-build snapshot" is the
one snapshot adjacency inside scope. sysforge *takes* a snapshot but never owns
retention — a snapper config governs its own cleanup; a raw fallback snapshot is
NOT auto-reaped (the user owns it). All detection is safe when btrfs / snapper /
the btrfs binary are absent. Every failure is a non-fatal warn: a snapshot must
never block a build the user asked for.

Wired at the build-orchestrator seams (build_core.build_and_install,
PackagesStage.run, KernelStage.run) via ensure_pre_build_snapshot, whose
module-level once-guard means at most one snapshot per process.

Public API:
    ensure_pre_build_snapshot(config, *, dry_run=False, interactive=False)
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log

_log = log.get_logger("SNAPSHOT")
_done = False


def reset_guard() -> None:
    """Test helper: clear the once-per-process guard."""
    global _done
    _done = False


def root_is_btrfs(mounts_path: str = "/proc/mounts") -> bool:
    try:
        for line in Path(mounts_path).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "/":
                return parts[2] == "btrfs"
    except OSError:
        return False
    return False


def snapper_config_for_root(configs_dir: str = "/etc/snapper/configs") -> str | None:
    d = Path(configs_dir)
    if not d.is_dir():
        return None
    for cfg in sorted(d.iterdir()):
        try:
            text = cfg.read_text()
        except OSError:
            continue
        if 'SUBVOLUME="/"' in text:
            return cfg.name
    return None


def create_snapper_snapshot(config: str, description: str) -> str | None:
    if not shutil.which("snapper"):
        return None
    try:
        r = subprocess.run(
            ["snapper", "-c", config, "create", "-d", description, "-p"],
            capture_output=True, text=True, check=False,
        )
    except OSError as e:
        _log.warn(f"  snapper snapshot failed: {e}")
        return None
    if r.returncode != 0:
        _log.warn(f"  snapper snapshot failed: {r.stderr.strip()}")
        return None
    return r.stdout.strip() or None


def create_raw_snapshot() -> Path | None:
    if not shutil.which("btrfs"):
        _log.warn("  btrfs command not found — cannot take raw snapshot")
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path("/.snapshots") / f"sysforge-pre-build-{stamp}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", "/", str(dest)],
            capture_output=True, text=True, check=False,
        )
    except OSError as e:
        _log.warn(f"  raw btrfs snapshot failed: {e}")
        return None
    if r.returncode != 0:
        _log.warn(f"  raw btrfs snapshot failed: {r.stderr.strip()}")
        return None
    return dest


def ensure_pre_build_snapshot(config: dict, *, dry_run: bool = False,
                              interactive: bool = False) -> None:
    global _done
    if _done:
        return
    if not config.get("build", {}).get("pre_build_snapshot", False):
        return
    _done = True  # a single decision per process, even when we skip below

    if not root_is_btrfs():
        _log.info("  root is not btrfs — skipping pre-build snapshot")
        return

    snapper_cfg = snapper_config_for_root()
    plan = (f"snapper snapshot (config '{snapper_cfg}')" if snapper_cfg
            else "raw read-only btrfs snapshot under /.snapshots")

    if dry_run:
        _log.ui(f"  [dry-run] would take a pre-build {plan}")
        return

    if interactive:
        from sysforge.primitives.prompt import prompt_choice
        if prompt_choice(f"  Take a pre-build {plan}?", ["y", "n"], default="n") != "y":
            _log.ui("  Pre-build snapshot skipped by user")
            return

    if snapper_cfg:
        sid = create_snapper_snapshot(snapper_cfg, "sysforge pre-build")
        if sid:
            _log.ui(f"  Pre-build snapshot created (snapper #{sid})")
    else:
        dest = create_raw_snapshot()
        if dest:
            _log.ui(
                f"  Pre-build snapshot created: {dest} "
                "(not auto-reaped — you own its cleanup)"
            )
