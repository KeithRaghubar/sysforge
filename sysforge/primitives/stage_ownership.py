# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Stage-ownership registry — which pipeline stage owns a given package.

A package is *stage-owned* when a long-running pipeline stage (``run kernel`` /
``run toolchain``) is responsible for building it. The everyday ``sysforge
update`` sweep skips such packages by default and points the user at the owning
stage instead, so the two never fight over the same build.

The authoritative ownership signal is the ``owner_stage`` field each stage
stamps onto ``BuildState.record()``. This module supplies the *bootstrap
fallback*: before a stage's first build has stamped anything — and for entries
written by older code that predates the field — ownership is inferred from the
stage's on-disk config (``kernel.toml`` / ``toolchain.toml``). The update verb
unions this fallback with the recorded ``owner_stage`` markers.

Build a :func:`load_stage_ownership` snapshot once per sweep (it reads each
stage config exactly once), then call :meth:`StageOwnership.owner_of` per
candidate package. Module-level :func:`owner_of` / :func:`owned_pkgbases`
convenience wrappers snapshot-per-call for one-off callers.

Layering: this is a primitive. It reads TOML config and reuses
``is_llvm_pkgbase`` (also a primitive). It must never import the pipeline layer.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.paths import KERNEL_PATH, TOOLCHAIN_PATH
from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase

# Toolchain [packages] sub-lists whose entries are explicitly stage-owned. Kept
# in step with toolchain.toml's schema — a new build list must be added here to
# be honored as owned.
_TOOLCHAIN_PACKAGE_KEYS = ("pgo", "non_pgo", "lib32")


def _load_toml(path: Path) -> dict | None:
    """Parse ``path`` as TOML, or return None if absent/unreadable/invalid."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _kernel_pkgbase(data: dict) -> str | None:
    """The kernel stage's pkgbase from ``kernel.toml``'s ``pkgname``."""
    pkg = data.get("pkgname")
    return pkg if isinstance(pkg, str) and pkg else None


def _toolchain_enabled(data: dict) -> bool:
    """True when the toolchain stage owns the LLVM suite.

    Only when ``enabled`` *and* ``compiler = "llvm"``: ``compiler`` defaults to
    ``gcc`` when unset, and the gcc path is register-only (builds no LLVM), so
    stock pacman LLVM stays pacman-class and is left alone.
    """
    return data.get("enabled") is True and (data.get("compiler") or "gcc") == "llvm"


def _toolchain_configured(data: dict) -> set[str]:
    """Explicit package names the toolchain stage builds, from ``[packages]``.

    Covers members that ``is_llvm_pkgbase`` does not match by prefix — notably
    ``spirv-llvm-translator`` (and any custom-listed package).
    """
    pkgs = data.get("packages", {}) or {}
    owned: set[str] = set()
    for key in _TOOLCHAIN_PACKAGE_KEYS:
        for name in pkgs.get(key, []) or []:
            if isinstance(name, str) and name:
                owned.add(name)
    return owned


@dataclass(frozen=True)
class StageOwnership:
    """Snapshot of stage-ownership config (one read of each stage config).

    Construct via :func:`load_stage_ownership` so a single update sweep reads
    each config file once, then call :meth:`owner_of` per candidate package.
    """

    kernel_pkgbase: str | None
    toolchain_active: bool
    toolchain_configured: frozenset[str]

    @property
    def any_active(self) -> bool:
        """True if any stage claims ownership — lets callers skip per-package
        pkgbase resolution entirely when nothing is owned."""
        return self.kernel_pkgbase is not None or self.toolchain_active

    def owner_of(self, name: str, pkgbase: str | None = None) -> str | None:
        """Return the stage that owns ``name`` by config bootstrap, or None.

        Checks both the package name and its resolved ``pkgbase`` so split
        packages (e.g. ``llvm-libs``/``polly`` under pkgbase ``llvm``) are
        classified correctly. Kernel ownership takes precedence over toolchain;
        the two never overlap in practice.
        """
        base = pkgbase or name
        if self.kernel_pkgbase and self.kernel_pkgbase in (name, base):
            return "kernel"
        # Implicit LLVM-suite prefix ownership covers only the 64-bit suite.
        # lib32-* LLVM packages are NOT built by the toolchain stage by default
        # (they share the all-target 64-bit headers and must not have their
        # LLVM_TARGETS_TO_BUILD reduced — see pipeline/stages/toolchain.py
        # ::_DEFAULT_LLVM_LIB32). They build via `sysforge update` instead, so the
        # prefix match must not claim them or update would skip them with nothing
        # building them. They are toolchain-owned only when explicitly opted back
        # in via `[packages] lib32` (the toolchain_configured path below).
        if self.toolchain_active and (
            (is_llvm_pkgbase(name) and not name.startswith("lib32-"))
            or (is_llvm_pkgbase(base) and not base.startswith("lib32-"))
            or name in self.toolchain_configured
            or base in self.toolchain_configured
        ):
            return "toolchain"
        return None

    def owned_pkgbases(self) -> set[str]:
        """Statically-enumerable owned package names from stage config.

        The kernel pkgbase plus the toolchain's explicitly-configured packages.
        Does *not* include the dynamic ``is_llvm_pkgbase`` prefix set — that is
        matched per-candidate in :meth:`owner_of` against the installed list and
        is not enumerable here.
        """
        owned: set[str] = set()
        if self.kernel_pkgbase:
            owned.add(self.kernel_pkgbase)
        if self.toolchain_active:
            owned |= set(self.toolchain_configured)
        return owned


def load_stage_ownership() -> StageOwnership:
    """Read each stage config once and snapshot its ownership facts."""
    kdata = _load_toml(KERNEL_PATH)
    tdata = _load_toml(TOOLCHAIN_PATH)
    toolchain_active = tdata is not None and _toolchain_enabled(tdata)
    return StageOwnership(
        kernel_pkgbase=_kernel_pkgbase(kdata) if kdata else None,
        toolchain_active=toolchain_active,
        toolchain_configured=frozenset(
            _toolchain_configured(tdata) if (toolchain_active and tdata) else ()
        ),
    )


def owner_of(name: str, pkgbase: str | None = None) -> str | None:
    """Convenience wrapper: snapshot config and resolve ``name``'s owner.

    Reads both stage configs on each call — use :func:`load_stage_ownership`
    directly when classifying many packages in one pass.
    """
    return load_stage_ownership().owner_of(name, pkgbase)


def owned_pkgbases() -> set[str]:
    """Convenience wrapper: statically-enumerable stage-owned package names."""
    return load_stage_ownership().owned_pkgbases()
