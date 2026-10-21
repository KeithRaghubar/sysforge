# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT


def __getattr__(name: str):
    # __version__ resolves on first access: importlib.metadata costs ~40 ms,
    # and every `sysforge completions` call on a Tab press imports this
    # package without ever reading the version (3.5.0-B124).
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version
    try:
        value = version("sysforge")
    except PackageNotFoundError:
        value = "0.0.0+unknown"
    globals()["__version__"] = value
    return value
