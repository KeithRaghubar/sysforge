"""
stages/__init__.py — canonical ordered pipeline stage list

Import STAGES to get the full ordered list of Stage instances.
The runner uses this list; order here is the execution order.
"""
from sysforge.pipeline.stages.partition import PartitionStage
from sysforge.pipeline.stages.base_install import BaseInstallStage
from sysforge.pipeline.stages.hardware import HardwareStage
from sysforge.pipeline.stages.toolchain import ToolchainStage
from sysforge.pipeline.stages.packages import PackagesStage
from sysforge.pipeline.stages.kernel import KernelStage
from sysforge.pipeline.stages.configure import ConfigureStage

STAGES = [
    PartitionStage(),
    BaseInstallStage(),
    HardwareStage(),
    ConfigureStage(),
    ToolchainStage(),
    PackagesStage(),
    KernelStage(),
]

STAGE_NAMES = [s.name for s in STAGES]
