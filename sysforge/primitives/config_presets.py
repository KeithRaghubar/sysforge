# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
config_presets.py — the ``sysforge setup`` presets, as data (3.5.0-F9)

A preset is a one-shot bundle of ordinary config values that ``setup`` writes;
nothing records which preset was chosen, so afterwards the values are edited
like any other. Each row carries the key's shipped ``default`` (pinned against
etc/sysforge by tests/test_config_presets.py), which stands in for the live
value when the key is unset, so a preset that already matches writes nothing.

Pure: no I/O. Path resolution, guards and writing live in ``setup_cmd``.

Public API
----------
    PresetRow, Preset, PRESETS, PRESET_NAMES, TARGET_FILES,
    LOW_FRICTION_REVIEW_WARNING
"""
from dataclasses import dataclass

# Symbolic row target -> shipped file name.
TARGET_FILES = {
    "profiles": "profiles.toml",
    "packages": "packages.toml",
    "sysforge": "sysforge.toml",
    "toolchain": "toolchain.toml",
    "kernel": "kernel.toml",
}


@dataclass(frozen=True)
class PresetRow:
    target: str
    section: str | None  # None = top-level key
    key: str
    value: bool | str
    default: bool | str


@dataclass(frozen=True)
class Preset:
    name: str
    summary: str
    rows: tuple[PresetRow, ...] = ()
    notes: tuple[str, ...] = ()


LOW_FRICTION_REVIEW_WARNING = (
    "  With review = false, changed AUR/git code is built and installed with\n"
    "  nobody having looked at it, and the changes are not logged or listed in\n"
    "  the update summary either."
)

PRESETS = {
    "defaults": Preset("defaults", "keep the shipped defaults (writes nothing)"),
    "performance": Preset(
        "performance", "faster binaries: LLVM toolchain and the optimized profile",
        rows=(
            PresetRow("profiles", "defaults", "profile", "optimized", "standard"),
            PresetRow("profiles", "defaults", "toolchain", "llvm", "gcc"),
        ),
        notes=("  For more, a PGO-built LLVM toolchain: set enabled = true and "
               'compiler = "llvm" in toolchain.toml, then run `sysforge run toolchain`.',),
    ),
    "low-friction": Preset(
        "low-friction", "fewest prompts: no plan confirmation, no source review",
        rows=(
            PresetRow("packages", "build", "confirm_plan", False, True),
            PresetRow("packages", "build", "review", False, True),
            PresetRow("sysforge", "security", "sandbox_builds", False, False),
            PresetRow("sysforge", "security", "auto_fetch_pgp_keys", True, False),
            PresetRow("toolchain", None, "reuse_unchanged", True, False),
        ),
        notes=(LOW_FRICTION_REVIEW_WARNING,),
    ),
    "secure": Preset(
        "secure", "hardened builds, reviewed sources, clean-chroot builds, hardened kernel",
        rows=(
            PresetRow("profiles", "defaults", "profile", "hardened", "standard"),
            PresetRow("packages", "build", "confirm_plan", True, True),
            PresetRow("packages", "build", "review", True, True),
            PresetRow("sysforge", "security", "sandbox_builds", True, False),
            PresetRow("sysforge", "security", "auto_fetch_pgp_keys", False, False),
            PresetRow("kernel", None, "hardening", True, False),
        ),
    ),
}

PRESET_NAMES = tuple(PRESETS)
