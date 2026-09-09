# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/identity.py — which toolchain is active, and what changed.

``ToolchainIdentity`` is the before/after shape behind the change summary
(2.6.1-F26), and ``compiler_paths``/``propagate_default_toolchain`` are the
register-only half of the stage — the whole of what the GCC path does.

No building happens here, which is the point: downstream stages need to know
what compiler to use whether or not this stage built anything.
"""
from dataclasses import dataclass
from dataclasses import field

from sysforge.primitives import build_fingerprint

from sysforge.primitives import config as prim_config
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Compiler path lookup (no build)
# ---------------------------------------------------------------------------


def compiler_paths(compiler: str) -> tuple[str, str, str | None]:
    """Return (cc, cxx, ld) for a named compiler without building anything."""
    if compiler == "gcc":
        return "/usr/bin/gcc", "/usr/bin/g++", None
    return "/usr/bin/clang", "/usr/bin/clang++", "lld"


def propagate_default_toolchain(compiler: str, options) -> None:
    """Sync ``profiles.toml [defaults] toolchain`` to ``toolchain.toml compiler``.

    Called only on a successful register/build so the package-compiler default
    tracks the toolchain this stage just registered. ``compiler`` is the
    ``toolchain.toml`` value (``"gcc"``/``"llvm"``) — never the build variant
    (stock_llvm/pgo_llvm), which is a different axis. No-op on dry runs;
    tolerant of an unwritable config (warns, does not fail the stage).
    """
    if getattr(options, "dry_run", False):
        return
    try:
        prim_config.set_default_toolchain(compiler)
    except OSError as exc:
        _log.warn(f"Could not update profiles.toml [defaults] toolchain: {exc}")

# ---------------------------------------------------------------------------
# Change-summary extras: toolchain identity and flag delta (2.6.1-F26)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolchainIdentity:
    """What a future build will be compiled with, at one point in time.

    Every field is a read of state the stage already computes or records —
    deliberately not a benchmark. An uncontrolled timing figure printed as a
    summary row would read as signal when it is noise (``kernel-bench.sh``
    clears ccache/sccache and drops caches precisely because of this), so no
    timing is collected here.
    """

    cc: str = ""
    cxx: str = ""
    ld: str = ""
    variant: str = ""
    fingerprint: str | None = None
    # pkgbase -> flags_string, restricted to toolchain-owned build_state entries.
    flags: dict[str, str] = field(default_factory=dict)


def probe_toolchain_identity(state, options) -> ToolchainIdentity:
    """Snapshot the active toolchain identity. Never raises.

    Called on both sides of the stage so the summary can render a transition.
    Any unobtainable field degrades to its empty default rather than losing
    the whole block — this is advisory output and must never fail a build.
    """
    from sysforge.pipeline.state import (
        get_toolchain_fingerprint,
        get_toolchain_variant,
    )

    result = {}
    variant = ""
    fingerprint = None
    try:
        result = state.get_stage_result("toolchain") or {}
        variant = get_toolchain_variant(state)
        fingerprint = get_toolchain_fingerprint(state)
    except Exception as exc:  # noqa: BLE001 — advisory only
        _log.debug(f"toolchain identity: stage result unreadable — {exc}")

    # Fall back to the configured compiler's stock paths when the stage has no
    # recorded result yet, so the "before" side of a first run is not blank.
    cc = result.get("cc") or ""
    cxx = result.get("cxx") or ""
    ld = result.get("ld") or ""

    flags: dict[str, str] = {}
    try:
        from sysforge.primitives.build_state import BuildState

        for name, entry in BuildState(options.state_dir).all_packages().items():
            if entry.get("owner_stage") == "toolchain" and entry.get("flags_string"):
                flags[name] = entry["flags_string"]
    except Exception:  # noqa: BLE001 — advisory only
        flags = {}

    return ToolchainIdentity(
        cc=build_fingerprint.compiler_version_line(cc) if cc else "",
        cxx=build_fingerprint.compiler_version_line(cxx) if cxx else "",
        # ld is a linker *name* ("lld"), not a path — report it verbatim rather
        # than probing, which is what makes this block free of new probes.
        ld=ld,
        variant=variant,
        fingerprint=fingerprint,
        flags=flags,
    )


def toolchain_identity_lines(
    before: ToolchainIdentity, after: ToolchainIdentity
) -> list[str]:
    """Render the identity delta as pre-formatted lines. Pure; never raises.

    A field that did not move prints once; a field that moved prints as
    ``old → new``. Fields empty on both sides are omitted entirely rather than
    printing a row of blanks.
    """
    from sysforge.primitives.flag_drift import diff_flags
    from sysforge.primitives.render import arrow

    lines: list[str] = []

    def _field(label: str, old, new) -> None:
        old, new = old or "", new or ""
        if not old and not new:
            return
        if old == new or not old:
            lines.append(f"{label}: {new}")
        else:
            lines.append(f"{label}: {old} {arrow()} {new}")

    _field("cc", before.cc, after.cc)
    _field("cxx", before.cxx, after.cxx)
    _field("ld", before.ld, after.ld)
    _field("variant", before.variant, after.variant)
    _field("fingerprint", before.fingerprint, after.fingerprint)

    # Flag delta per toolchain-owned pkgbase. diff_flags already emits the
    # +added / -removed / changed vocabulary and indents its own lines.
    for pkgbase in sorted(set(before.flags) | set(after.flags)):
        diffs = diff_flags(before.flags.get(pkgbase, ""), after.flags.get(pkgbase, ""))
        if diffs:
            lines.append(f"flags ({pkgbase}):")
            lines.extend(diffs)

    return lines
