"""
stages/toolchain.py — stage stub

Not yet implemented. Raises NotImplementedError when called.
Use --start-from to bypass this stage during development.
"""
from sysforge.pipeline.stages.base import Stage


class ToolchainStage(Stage):
    name = "toolchain"
    description = "LLVM toolchain build"
    depends_on = ["reconfigure"]

    def run(self, config, state, options):
        raise NotImplementedError(
            f"Stage {self.name!r} is not yet implemented. "
            f"Use --start-from reconfigure for pre-build checks, or --start-from packages to skip straight to builds."
        )
