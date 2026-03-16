"""
stages/configure.py — stage 4: bootstrap configuration (stub)

Runs once during initial system install, after hardware detection and before
the pre-build reconfiguration checkpoint (stage 5).

Responsibilities (not yet implemented):
  1. System identity — set hostname, locale, timezone, keymap
  2. Pacman mirrorlist — run reflector, set ParallelDownloads

These steps are intentionally separated from reconfigure (stage 5) because
they are destructive to a live running system if re-applied carelessly.
Stage 5 (reconfigure) handles the safe-to-repeat pre-build checks.

Use --start-from reconfigure to skip this stage on a live system.
"""
from sysforge.pipeline.stages.base import Stage


class ConfigureStage(Stage):
    name = "configure"
    description = "Bootstrap configuration — system identity, mirrorlist"
    depends_on = ["hardware"]

    def run(self, config, state, options):
        raise NotImplementedError(
            f"Stage {self.name!r} is not yet implemented. "
            f"Use --start-from reconfigure to skip bootstrap stages on a live system."
        )
