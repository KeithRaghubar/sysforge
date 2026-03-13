"""
stages/toolchain.py — stage stub

Not yet implemented. Raises NotImplementedError when called.
Use --start-from to bypass this stage during development.
"""
from sysforge.pipeline.stages.base import Stage


class ToolchainStage(Stage):
    name = "toolchain"
    description = "LLVM toolchain build"
    depends_on = ["hardware"]

    def run(self, config, state, options):
        raise NotImplementedError(
            f"Stage {self.name!r} is not yet implemented. "
            f"Use --start-from to bypass stages 1-4 during development."
        )
