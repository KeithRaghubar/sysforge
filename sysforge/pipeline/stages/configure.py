"""
stages/configure.py — stage 4: configuration checkpoint

Runs after hardware detection and before toolchain/packages/kernel.
Responsibilities:
  - Display a summary of completed prior stages
  - Offer interactive review and editing of sysforge config files
    (flag_profiles.toml, packages.toml, kernel.toml, hardware_profile.toml)
    before any build work begins
  - Validate edited configs (full profile resolution on flag_profiles.toml)
  - Persist editor preference to /etc/sysforge/sysforge.toml

Not yet implemented. Raises NotImplementedError when called.
Use --start-from to bypass this stage during development.
"""
from sysforge.pipeline.stages.base import Stage


class ConfigureStage(Stage):
    name = "configure"
    description = "Configuration checkpoint — review configs before building"
    depends_on = ["hardware"]

    def run(self, config, state, options):
        raise NotImplementedError(
            f"Stage {self.name!r} is not yet implemented. "
            f"Use --start-from to bypass this stage during development."
        )
