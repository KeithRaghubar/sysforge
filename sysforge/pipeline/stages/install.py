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
from sysforge.pipeline.stages._partition_plan import _confirm
from sysforge.primitives.archinstall_config import build_archinstall_config
from sysforge.primitives.archinstall_invoke import run_archinstall

_log = log.get_logger("INSTALL")


class InstallStage(Stage):
    name = "install"
    description = "Disk + base install via archinstall"
    depends_on = []

    def run(self, config, state, options):  # noqa: ARG002
        cfg = load_bootstrap()
        cfg_dict = build_archinstall_config(cfg)

        if not options.dry_run:
            _confirm(cfg)  # destructive-op gate; raises to abort

        run_archinstall(cfg_dict, dry_run=options.dry_run)
        _log.ui("Base install complete.")
