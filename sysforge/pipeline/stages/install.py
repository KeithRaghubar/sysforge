# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""stages/install.py — bootstrap stage 1: disk + base install via archinstall.

Generates the archinstall JSON config from bootstrap.toml and runs
``archinstall --silent``, which partitions, formats, mounts, pacstraps,
genfstabs, installs the bootloader, and creates users/services/identity.

archinstall --silent never prompts, so this stage keeps sysforge's own
destructive-operation confirmation before handing off.
"""
from sysforge import log
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import load_bootstrap
from sysforge.pipeline.stages._partition_plan import _confirm, probe_disk_size_bytes
from sysforge.primitives.archinstall_config import build_archinstall_config
from sysforge.primitives.archinstall_invoke import run_archinstall

_log = log.get_logger("INSTALL")

# Nominal disk size used only to render a --dry-run preview when the target
# device can't be probed (e.g. previewing off the live ISO). Never reaches a
# real partition operation.
_PREVIEW_DISK_BYTES = 40 * 1024**3


class InstallStage(Stage):
    name = "install"
    description = "Disk + base install via archinstall"
    depends_on = []

    def run(self, config, state, options):  # noqa: ARG002
        cfg = load_bootstrap()

        disk_size_bytes = probe_disk_size_bytes(cfg.device)
        if disk_size_bytes is None:
            if options.dry_run:
                _log.warn(
                    f"could not probe size of {cfg.device}; using a nominal "
                    f"{_PREVIEW_DISK_BYTES // 1024**3} GiB for this dry-run preview"
                )
                disk_size_bytes = _PREVIEW_DISK_BYTES
            else:
                raise RuntimeError(
                    f"[INSTALL] could not determine the size of {cfg.device}. "
                    f"Is it a valid block device? (checked with lsblk)"
                )
        cfg_dict = build_archinstall_config(cfg, disk_size_bytes=disk_size_bytes)

        if not options.dry_run:
            _confirm(cfg)  # destructive-op gate; raises to abort

        run_archinstall(cfg_dict, dry_run=options.dry_run)
        _log.info("Base install complete.")
