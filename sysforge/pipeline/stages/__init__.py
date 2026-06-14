# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/__init__.py — canonical ordered pipeline stage list

Import STAGES to get the full ordered list of Stage instances.
The runner uses this list; order here is the execution order.
"""
from sysforge.pipeline.stages.partition import PartitionStage
from sysforge.pipeline.stages.base_install import BaseInstallStage
from sysforge.pipeline.stages.hardware import HardwareStage
from sysforge.pipeline.stages.configure import ConfigureStage
from sysforge.pipeline.stages.reconfigure import ReconfigureStage
from sysforge.pipeline.stages.toolchain import ToolchainStage
from sysforge.pipeline.stages.packages import PackagesStage
from sysforge.pipeline.stages.kernel import KernelStage

STAGES = [
    PartitionStage(),
    BaseInstallStage(),
    HardwareStage(),
    ConfigureStage(),
    ReconfigureStage(),
    ToolchainStage(),
    PackagesStage(),
    KernelStage(),
]

STAGE_NAMES = [s.name for s in STAGES]
