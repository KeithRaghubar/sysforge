# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sysforge")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
