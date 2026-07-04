# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
flag_drift.py — detect profile/flag drift for a recorded package build.

A source build resolves a set of compiler/linker flags from the active profile;
that resolved flags string is recorded in ``build_state.toml`` at build time
(``flags_string``). When the profile/flag configuration later changes, the flags
a fresh build *would* apply diverge from what was recorded — "flag drift". This
module re-resolves the current profile for one recorded package and diffs it
against the stored string.

The engine behind ``sysforge update``'s Phase 4.3, the canonical flag-drift
surface. Pure primitive: it re-resolves through the
profile / PKGBUILD primitives, never imports the pipeline layer, and never logs —
the caller decides how to surface each outcome (``update`` under ``[UPDATE]``).

Public API:
    diff_flags(stored, current) -> list[str]
    resolve_flag_drift(entry, config, conflict_groups) -> FlagDriftResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sysforge.primitives.build_state import BUILD_MODE_SOURCE
from sysforge.primitives.pkgbuild_meta import option_disabled, parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
from sysforge.primitives.profile import (
    build_mode_uses_extracted_profile,
    get_build_mode,
    match_rules,
    resolve_profile,
    serialize_flags,
)

# Outcome statuses for a single recorded package.
STATUS_DRIFTED = "DRIFTED"            # stored flags differ from a fresh resolution
STATUS_IN_SYNC = "IN_SYNC"           # stored flags match the current resolution
STATUS_NOT_PROFILED = "NOT_PROFILED"  # build_mode != "source_built" — no flags to drift
STATUS_BUILDFLAGS_IGNORED = "BUILDFLAGS_IGNORED"  # PKGBUILD options=('!buildflags'): makepkg ignores conf flags
STATUS_NO_PKGBUILD = "NO_PKGBUILD"   # recorded pkgbuild_dir has no PKGBUILD
STATUS_NO_FLAGS = "NO_FLAGS"         # built before flag tracking; nothing stored
STATUS_PARSE_ERROR = "PARSE_ERROR"   # parse_pkgbuild raised


@dataclass
class FlagDriftResult:
    """Outcome of comparing a recorded build's flags against the current profile."""

    status: str
    diffs: list[str] = field(default_factory=list)
    stored_flags: str | None = None
    current_flags: str | None = None
    pkgbuild_path: Path | None = None
    error: str | None = None  # exception text when status == STATUS_PARSE_ERROR

    @property
    def drifted(self) -> bool:
        return self.status == STATUS_DRIFTED


def diff_flags(stored: str, current: str) -> list[str]:
    """Return human-readable per-key diff lines between two flags strings.

    Format per changed key: ``  KEY: <old> → <new>`` or ``  +KEY: <new>`` /
    ``  -KEY: <old>``.
    """
    def _parse(s: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in s.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v
        return result

    old = _parse(stored)
    new = _parse(current)
    diffs: list[str] = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            diffs.append(f"  +{key}: {new[key]!r}")
        elif key not in new:
            diffs.append(f"  -{key}: {old[key]!r}")
        elif old[key] != new[key]:
            diffs.append(f"  {key}: {old[key]!r} → {new[key]!r}")
    return diffs


def resolve_flag_drift(entry: dict, config: dict, conflict_groups) -> FlagDriftResult:
    """Compare one recorded build's stored flags against a fresh resolution.

    ``entry`` is a ``build_state.toml`` record for a pkgbase; it must carry
    ``build_mode`` and ``pkgbuild_dir``, and — to detect drift — ``flags_string``.
    Re-resolves the current profile for the package's PKGBUILD and diffs the
    serialized flags against the stored string. Patched-PKGBUILD and kernel
    builds carry their flags embedded in the PKGBUILD, so the embedded profile is
    extracted before resolution (matching the build-time path).

    Never raises on a bad PKGBUILD: a parse failure is reported as
    ``STATUS_PARSE_ERROR`` with the message in ``error`` so callers can decide how
    to surface it.
    """
    if entry.get("build_mode") != BUILD_MODE_SOURCE:
        return FlagDriftResult(status=STATUS_NOT_PROFILED)

    pkgbuild_path = Path(entry["pkgbuild_dir"]) / "PKGBUILD"
    if not pkgbuild_path.exists():
        return FlagDriftResult(status=STATUS_NO_PKGBUILD, pkgbuild_path=pkgbuild_path)

    stored_flags = entry.get("flags_string")
    if not stored_flags:
        return FlagDriftResult(status=STATUS_NO_FLAGS, pkgbuild_path=pkgbuild_path)

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:  # noqa: BLE001 — best-effort; reported, not raised
        return FlagDriftResult(
            status=STATUS_PARSE_ERROR, pkgbuild_path=pkgbuild_path, error=str(e),
        )

    # options=('!buildflags'): makepkg discards CFLAGS/CXXFLAGS/CPPFLAGS/LDFLAGS
    # from the conf entirely, so the resolved profile flags never reach the
    # build. A change in those flags cannot affect the built package, so flag
    # drift must not fire a rebuild here (F9).
    if option_disabled(pkgmeta, "buildflags"):
        return FlagDriftResult(
            status=STATUS_BUILDFLAGS_IGNORED, pkgbuild_path=pkgbuild_path,
            stored_flags=stored_flags,
        )

    matched = match_rules(pkgmeta, config.get("rules", []))
    build_mode = get_build_mode(matched, config)
    extracted_profile = None
    if build_mode_uses_extracted_profile(build_mode):
        extracted_profile = extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
    resolved = resolve_profile(
        pkgmeta, matched, config, conflict_groups,
        extracted_profile=extracted_profile,
    )
    current_flags = serialize_flags(resolved)

    diffs = diff_flags(stored_flags, current_flags)
    return FlagDriftResult(
        status=STATUS_DRIFTED if diffs else STATUS_IN_SYNC,
        diffs=diffs,
        stored_flags=stored_flags,
        current_flags=current_flags,
        pkgbuild_path=pkgbuild_path,
    )
