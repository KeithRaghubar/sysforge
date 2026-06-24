# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
mesa_pgo.py — instrumentation-PGO orchestration (the ``build --pgo`` flow)

The one home for sysforge's *runtime*-profiled per-package optimization. Mesa is
the seeded/default target (and the only one with bespoke graphics handling), but
since PGO is "just a build flag" (the ``compiler_flags_extra`` seam) every
function here takes a ``pkgbase`` so ``--pgo`` works on any package (F5): mesa
keeps its back-compat ``<root>/pgo-mesa`` store, a generic target gets its own
``<root>/pgo/<pkgbase>``. Unlike the LLVM toolchain PGO (where an instrumented
clang profiles itself while building LLVM in one controlled process), a
runtime-exercised library only emits profile data when applications later call
into it. So the store path is baked into the build at compile time —
``-fprofile-generate=<store>`` in CFLAGS/CXXFLAGS/LDFLAGS — and *any* process that
loads the instrumented binary appends its ``.profraw`` to the sysforge store with
no per-session env setup.

Two steps, the same flag-injection seam the toolchain PGO uses
(``makepkg_wrapper`` ``compiler_flags_extra`` → emitted ``makepkg.conf``):

  * ``record`` — build+install an instrumented mesa with
    :func:`generate_flag`; the user then runs their normal graphics workload and
    ``.profraw`` accumulates in :func:`resolve_store`.
  * ``use`` — :func:`merge_profraw` folds the collected ``.profraw`` into one
    ``mesa.profdata``; the rebuild consumes it via :func:`use_flags` and earns
    the ``-sysforge`` rename (``build_mode = "pgo_mesa"``).

Store resolution defers to :func:`makepkg_pgo.resolve_method_store` (method
``"pgo-mesa"``) so the shared profile-store root / provisioning / purge path
covers it. The profraw merge shells out to ``llvm-profdata`` (LLVM only — this
whole feature is gated on the LLVM toolchain upstream); everything else is pure.
"""
import shutil
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_pgo import resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH

_log = log.get_logger("MESAPGO")

# Merged profile filename inside the store. ``.profraw`` (raw, per-process) is
# what the instrumented build writes at runtime; ``<pkgbase>.profdata`` is the
# merged artifact ``-fprofile-use`` consumes. ``mesa.profdata`` is the mesa
# special case (== the generic ``<pkgbase>.profdata`` pattern), kept as a named
# constant for back-compat references.
PROFDATA_NAME = "mesa.profdata"

# The optimization build_mode this flow records for mesa (see
# profile.is_optimized_build_mode); generic targets record ``"pgo"`` via
# build_mode_for(). The -sysforge package rename both trigger is applied
# generically in makepkg_wrapper._run_build (gated on is_optimized_build_mode).
BUILD_MODE = "pgo_mesa"
GENERIC_BUILD_MODE = "pgo"


def _is_mesa(pkgbase: str | None) -> bool:
    """Mesa-family check (lazy import to keep this module import-light)."""
    from sysforge.primitives.profile import is_mesa_pkgbase

    return is_mesa_pkgbase(pkgbase)


def build_mode_for(pkgbase: str | None = "mesa") -> str:
    """The recorded ``build_mode`` for a ``--pgo`` build of ``pkgbase``.

    Mesa keeps its established ``"pgo_mesa"`` value (back-compat with existing
    ``build_state.toml`` entries and the mesa-specific reuse path); every other
    package records the generic ``"pgo"``. Both are in ``_OPTIMIZED_BUILD_MODES``
    so they earn the ``-sysforge`` rename.
    """
    return BUILD_MODE if _is_mesa(pkgbase) else GENERIC_BUILD_MODE


def profdata_name(pkgbase: str | None = "mesa") -> str:
    """Merged-profile filename for ``pkgbase`` (``<pkgbase>.profdata``)."""
    return f"{pkgbase}.profdata"


class MesaPgoError(Exception):
    """A mesa-PGO step could not complete (no profraw collected, ``llvm-profdata``
    missing/failed). Raised so the build aborts cleanly *before* makepkg runs,
    with an actionable message, rather than silently producing an unprofiled build."""


def _load_tcfg() -> dict | None:
    """Best-effort load of toolchain.toml for store-path overrides (pure)."""
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with open(TOOLCHAIN_PATH, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def resolve_store(tcfg: dict | None = None, pkgbase: str | None = "mesa") -> Path:
    """Resolve a package's PGO store dir.

    Mesa-family keeps the back-compat ``<profile_store_root>/pgo-mesa`` location
    (so already-collected mesa profiles are never orphaned); every other package
    gets its own ``<profile_store_root>/pgo/<pkgbase>`` via the generic ``pgo``
    method's ``target`` namespace. ``tcfg`` defaults to a fresh toolchain.toml
    load so callers that only have a pkgbuild path don't have to thread config
    through. The directory itself is provisioned by the caller via
    ``fs_provision.ensure_writable_dir`` — this is pure path math, no I/O.
    """
    tc = tcfg if tcfg is not None else _load_tcfg()
    if _is_mesa(pkgbase):
        return resolve_method_store(tc, "pgo-mesa")
    return resolve_method_store(tc, "pgo", target=pkgbase)


def profdata_path(tcfg: dict | None = None, pkgbase: str | None = "mesa") -> Path:
    """Path to the merged ``<pkgbase>.profdata`` inside the store."""
    return resolve_store(tcfg, pkgbase) / profdata_name(pkgbase)


def list_profraw(store: Path) -> list[Path]:
    """Every ``.profraw`` currently in the store (recursive)."""
    return sorted(store.glob("**/*.profraw")) if store.is_dir() else []


def generate_flag(store: Path) -> str:
    """The compile+link flag that bakes the store path into the instrumented mesa.

    Returned as a single token appended to ``compiler_flags_extra`` (which
    makepkg_conf injects into CFLAGS, CXXFLAGS *and* LDFLAGS — covering both the
    instrumented codegen and the profile-runtime link in one shot)."""
    return f"-fprofile-generate={store}"


def use_flags(profdata: Path) -> str:
    """Flags for the optimized (``use``) rebuild: consume the merged profile and
    demote the inevitable instrumentation-vs-source skew warnings so a mesa built
    with ``-Werror`` doesn't fail on a slightly-stale profile (out-of-date /
    unprofiled functions are expected after any upstream churn)."""
    return (
        f"-fprofile-use={profdata} "
        "-Wno-profile-instr-out-of-date -Wno-profile-instr-unprofiled"
    )


def reuse_profdata(
    tcfg: dict | None = None, pkgbase: str | None = "mesa"
) -> Path | None:
    """Return the merged ``<pkgbase>.profdata`` if a prior ``--pgo=use`` left one.

    Durability hook for the ``update`` / plain ``build <pkg>`` path. A
    source-tracked package is rebuilt every ``update``; without re-applying the
    profile the user already collected (via :func:`use_flags`) the rebuild
    would silently regress to a stock, unprofiled build — contradicting the
    one-shot ``build <pkg> --pgo=use`` the user ran. The *existence* of a merged
    profile in the package's store is the durable signal that this host opted
    into PGO for it, so a no-``--pgo`` rebuild reuses it.

    Returns ``None`` when no merged profile exists (never PGO-built, or only
    ``record``-instrumented — bare ``.profraw`` is not consumable), so the
    caller falls back to a normal build. No re-merge happens here: once ``use``
    swaps the instrumented mesa for the optimized one, no new ``.profraw``
    accrues between updates, so the existing merged profile is current. Pure —
    just a path existence check.
    """
    pd = profdata_path(tcfg, pkgbase)
    return pd if pd.is_file() else None


def merge_profraw(
    store: Path, *, pkgbase: str | None = "mesa", profdata_tool: str = "llvm-profdata"
) -> Path:
    """Merge every ``.profraw`` in ``store`` into ``store/<pkgbase>.profdata``.

    Returns the profdata path on success. Raises :class:`MesaPgoError` when no
    profraw was collected (the user ran ``use`` before exercising the
    instrumented mesa) or when ``llvm-profdata`` is missing or fails — all three
    are clean aborts with an actionable hint, never a silent unprofiled build.
    """
    profraw = list_profraw(store)
    if not profraw:
        raise MesaPgoError(
            f"no .profraw files in {store} — build+install the instrumented "
            f"{pkgbase} with `sysforge build {pkgbase} --pgo=record`, run a "
            "representative workload to exercise it, then re-run `--pgo=use`."
        )
    if shutil.which(profdata_tool) is None:
        raise MesaPgoError(
            f"{profdata_tool!r} not found on PATH — PGO needs the LLVM "
            "toolchain (llvm-profdata ships with llvm)."
        )
    out = store / profdata_name(pkgbase)
    _log.ui(f"Merging {len(profraw)} {pkgbase} .profraw file(s) → {out}")
    result = subprocess.run(
        [profdata_tool, "merge", "--output", str(out), *(str(p) for p in profraw)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MesaPgoError(
            f"{profdata_tool} merge failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    _log.info(f"Merged mesa profile: {out} ({out.stat().st_size} bytes)")
    return out
