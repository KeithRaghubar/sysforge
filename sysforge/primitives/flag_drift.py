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
    drift_candidate(entry) -> bool
    resolve_flag_drift(entry, config, conflict_groups, system_conf_path=None,
                       system_assignments=None,
                       preserved_system_tokens=None,
                       sandbox_policy=None, state_dir=None) -> FlagDriftResult
    current_sandboxed(entry, config, conflict_groups, policy) -> bool | None
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field
from pathlib import Path

from sysforge.primitives.build_state import is_source_build_mode
from sysforge.primitives.makepkg_conf import serialize_effective_flags
from sysforge.primitives.pkgbuild_meta import option_disabled, parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
from sysforge.primitives.profile import (
    KERNEL_CLEAN_KEYS,
    build_mode_uses_extracted_profile,
    get_build_mode,
    match_rules,
    resolve_profile,
)
from sysforge.primitives.render import arrow

# Outcome statuses for a single recorded package.
STATUS_DRIFTED = "DRIFTED"            # stored flags differ from a fresh resolution
STATUS_IN_SYNC = "IN_SYNC"           # stored flags match the current resolution
STATUS_NOT_PROFILED = "NOT_PROFILED"  # build_mode != "source_built" — no flags to drift
STATUS_BUILDFLAGS_IGNORED = "BUILDFLAGS_IGNORED"  # PKGBUILD options=('!buildflags'):
# makepkg ignores conf flags
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
    ``  -KEY: <old>``. The arrow resolves through :func:`render.arrow` so the
    lines stay safe for non-log emitters under the ASCII glyph gate.
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
            diffs.append(f"  {key}: {old[key]!r} {arrow()} {new[key]!r}")
    return diffs


def _is_pgo_mode(build_mode: str | None) -> bool:
    from sysforge.primitives import mesa_pgo
    return build_mode in (mesa_pgo.BUILD_MODE, mesa_pgo.GENERIC_BUILD_MODE)


def drift_candidate(entry: dict) -> bool:
    """Whether ``entry`` is judged by flag drift at all.

    A ``source_built`` entry (or ``bolt_llvm``, the same build BOLTed,
    3.5.0-B31) is judged on every sub-axis. A PGO build
    (``pgo`` / ``pgo_mesa``) is judged on the build-inputs sub-axis only, and
    only once it has recorded ``build_inputs`` (3.2.0-F18): its profile flags
    were never compared, and that stays unchanged.
    """
    mode = entry.get("build_mode")
    return is_source_build_mode(mode) or (
        _is_pgo_mode(mode) and "build_inputs" in entry)


def _inputs_diff(entry: dict, pkgmeta: dict, resolved: dict,
                 state_dir) -> list[str]:
    """Per-key diff of the recorded vs re-derived out-of-profile inputs.

    The build-inputs sub-axis (3.2.0-F18): re-derived through the same
    ``build_inputs.resolve_build_inputs`` the record site calls, with the
    compiler resolved the same way. A record without the field is unknown and
    never drifts, so an upgrade does not flag every pre-existing entry.
    """
    if state_dir is None or "build_inputs" not in entry:
        return []
    from sysforge.primitives.build_inputs import (
        resolve_build_inputs, serialize_build_inputs,
    )
    try:
        current = serialize_build_inputs(resolve_build_inputs(
            pkgmeta, state_dir=state_dir,
            cc=resolved.get("CC") or os.environ.get("CC", ""),
            owner_stage=entry.get("owner_stage"),
        ))
    except Exception:  # noqa: BLE001 — unresolvable now is unknown, not drift
        return []
    return diff_flags(entry["build_inputs"] or "", current)


def _sandboxed_for(entry: dict, resolved: dict, policy, build_mode=None) -> bool:
    """Whether this build would run sandboxed now: the profile's override of
    the global policy, except that kernel and toolchain-stage builds always run
    on the host. One predicate for the drift sub-axis and the update plan."""
    from sysforge.primitives.build_sandbox import for_profile
    suppressed = (build_mode == "kernel"
                  or entry.get("owner_stage") in ("kernel", "toolchain"))
    return False if suppressed else for_profile(policy, resolved).enabled


def current_sandboxed(entry: dict, config: dict, conflict_groups, policy) -> bool | None:
    """:func:`_sandboxed_for` for a recorded package, resolving its profile
    from the PKGBUILD on disk. None when the PKGBUILD is missing or unparsable."""
    raw = entry.get("pkgbuild_dir")
    if not raw:
        return None
    pkgbuild_path = Path(raw) / "PKGBUILD"
    if not pkgbuild_path.exists():
        return None
    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception:  # noqa: BLE001 — unknown, not an error, for a plan mark
        return None
    matched = match_rules(pkgmeta, config.get("rules", []))
    build_mode = get_build_mode(matched, config)
    extracted = (extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
                 if build_mode_uses_extracted_profile(build_mode) else None)
    resolved = resolve_profile(pkgmeta, matched, config, conflict_groups,
                               extracted_profile=extracted)
    return _sandboxed_for(entry, resolved, policy, build_mode)


def _sandbox_diff(entry: dict, resolved: dict, policy, build_mode=None) -> list[str]:
    """``  sandboxed: <old> → <new>`` when the recorded sandbox state differs.

    The ``sandboxed`` sub-axis (3.4.0-B3). Current state comes from the same
    ``for_profile`` resolution the invocation seam uses; a stage-owned build is
    always suppressed onto the host. A record without the field is unknown and
    never drifts, so an upgrade does not flag every pre-existing entry.
    """
    if policy is None or "sandboxed" not in entry:
        return []
    # Mirrors makepkg_wrapper's suppression predicate exactly (see
    # _sandboxed_for): a kernel or toolchain-stage build always runs on the
    # host, so it is recorded sandboxed=False.
    current = _sandboxed_for(entry, resolved, policy, build_mode)
    stored = bool(entry["sandboxed"])
    if stored == current:
        return []
    return [f"  sandboxed: {stored!r} {arrow()} {current!r}"]


def resolve_flag_drift(entry: dict, config: dict, conflict_groups,
                       system_conf_path=None, system_assignments=None,
                       preserved_system_tokens=None,
                       sandbox_policy=None, state_dir=None) -> FlagDriftResult:
    """Compare one recorded build's stored flags against a fresh resolution.

    ``entry`` is a ``build_state.toml`` record for a pkgbase; it must carry
    ``build_mode`` and ``pkgbuild_dir``, and — to detect drift — ``flags_string``.
    Re-resolves the current profile for the package's PKGBUILD and diffs the
    serialized flags against the stored string. Patched-PKGBUILD and kernel
    builds carry their flags embedded in the PKGBUILD, so the embedded profile is
    extracted before resolution (matching the build-time path).

    The comparison is against the *effective* flags — the resolved profile after
    ``makepkg_conf.serialize_effective_flags`` applies the
    ``[preserved_system_tokens]`` pass, exactly as the conf-emission seam does
    at build time (3.1.0-B11). ``system_conf_path`` / ``system_assignments`` /
    ``preserved_system_tokens`` are injection points for that pass, loaded from
    the system conf and profiles.toml when None; callers looping over many
    packages should hoist them. Pass ``preserved_system_tokens={}`` to compare
    raw resolved profiles.

    ``sandbox_policy`` (the process-wide ``build_sandbox`` policy) enables the
    ``sandboxed`` sub-axis (3.4.0-B3): a recorded ``sandboxed`` that differs
    from what a fresh build would do is a drift line in the same vocabulary.
    It is judged even for ``!buildflags`` and pre-flag-tracking entries, because
    the sandbox changes output where conf flags do not reach. ``None`` disables
    it.

    Never raises on a bad PKGBUILD: a parse failure is reported as
    ``STATUS_PARSE_ERROR`` with the message in ``error`` so callers can decide how
    to surface it.
    """
    inputs_only = _is_pgo_mode(entry.get("build_mode"))
    if not drift_candidate(entry) or (inputs_only and state_dir is None):
        return FlagDriftResult(status=STATUS_NOT_PROFILED)

    pkgbuild_path = Path(entry["pkgbuild_dir"]) / "PKGBUILD"
    if not pkgbuild_path.exists():
        return FlagDriftResult(status=STATUS_NO_PKGBUILD, pkgbuild_path=pkgbuild_path)

    stored_flags = None if inputs_only else entry.get("flags_string")
    # The no-flags short-circuit used to come first; it now waits for the
    # sandbox and build-inputs sub-axes below, which need the resolved
    # profile. A record with neither flags nor either sub-axis field still
    # returns NO_FLAGS without parsing.
    sandbox_judged = (not inputs_only and sandbox_policy is not None
                      and "sandboxed" in entry)
    inputs_judged = state_dir is not None and "build_inputs" in entry
    if not stored_flags and not sandbox_judged and not inputs_judged:
        return FlagDriftResult(status=STATUS_NO_FLAGS, pkgbuild_path=pkgbuild_path)

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:  # noqa: BLE001 — best-effort; reported, not raised
        if not stored_flags:
            return FlagDriftResult(status=STATUS_NO_FLAGS, pkgbuild_path=pkgbuild_path)
        return FlagDriftResult(
            status=STATUS_PARSE_ERROR, pkgbuild_path=pkgbuild_path, error=str(e),
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
    # 3.4.0-B3 / 3.2.0-F18: judged before the !buildflags / no-flags returns,
    # because the sandbox and the out-of-profile inputs change output even
    # where makepkg ignores conf flags. The sub-axes share one diff list.
    sub_diffs = (_sandbox_diff(entry, resolved, sandbox_policy, build_mode=build_mode)
                    if sandbox_judged else [])
    sub_diffs += _inputs_diff(entry, pkgmeta, resolved, state_dir)

    if not stored_flags:
        if sub_diffs:
            return FlagDriftResult(status=STATUS_DRIFTED, diffs=sub_diffs,
                                   pkgbuild_path=pkgbuild_path)
        return FlagDriftResult(status=STATUS_NO_FLAGS, pkgbuild_path=pkgbuild_path)

    # options=('!buildflags'): makepkg discards CFLAGS/CXXFLAGS/CPPFLAGS/LDFLAGS
    # from the conf entirely, so the resolved profile flags never reach the
    # build. A change in those flags cannot affect the built package, so flag
    # drift must not fire a rebuild here (F9) — but the sandbox still can.
    if option_disabled(pkgmeta, "buildflags"):
        if sub_diffs:
            return FlagDriftResult(status=STATUS_DRIFTED, diffs=sub_diffs,
                                   stored_flags=stored_flags,
                                   pkgbuild_path=pkgbuild_path)
        return FlagDriftResult(
            status=STATUS_BUILDFLAGS_IGNORED, pkgbuild_path=pkgbuild_path,
            stored_flags=stored_flags,
        )
    # The kernel verdict must match makepkg_wrapper's at record time
    # (`build_mode == "kernel" or owner_stage == "kernel"`), or a stage-owned
    # kernel build would drift against itself; owner_stage is persisted in
    # build_state for exactly this kind of replay.
    kernel_build = build_mode == "kernel" or entry.get("owner_stage") == "kernel"
    current_flags = serialize_effective_flags(
        resolved, kernel_build=kernel_build,
        system_conf_path=system_conf_path, system_assignments=system_assignments,
        preserved_system_tokens=preserved_system_tokens,
        conflict_groups=conflict_groups,
    )

    if kernel_build:
        # A record from before 3.2.0-B39 still carries the keys a kernel build
        # never receives; drop them so it converges without one false drift.
        stored_flags = "\n".join(
            line for line in stored_flags.splitlines()
            if line.partition("=")[0].strip() not in KERNEL_CLEAN_KEYS
        )
    diffs = diff_flags(stored_flags, current_flags) + sub_diffs
    return FlagDriftResult(
        status=STATUS_DRIFTED if diffs else STATUS_IN_SYNC,
        diffs=diffs,
        stored_flags=stored_flags,
        current_flags=current_flags,
        pkgbuild_path=pkgbuild_path,
    )
