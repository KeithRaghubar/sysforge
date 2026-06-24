# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
mesa_pgo.py — mesa instrumentation-PGO orchestration (the ``build --pgo`` flow)

The one home for sysforge's *runtime*-profiled mesa optimization. Unlike the LLVM
toolchain PGO (where an instrumented clang profiles itself while building LLVM in
one controlled process), mesa is a runtime-exercised library: an instrumented
mesa only emits profile data when desktop/GPU applications later call into it. So
the store path is baked into the build at compile time — ``-fprofile-generate=<store>``
in CFLAGS/CXXFLAGS/LDFLAGS — and *any* GPU app that loads mesa appends its
``.profraw`` to the sysforge store with no per-session env setup.

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
# what the instrumented mesa writes at runtime; ``mesa.profdata`` is the merged
# artifact ``-fprofile-use`` consumes.
PROFDATA_NAME = "mesa.profdata"

# The optimization build_mode this flow records (see profile.is_optimized_build_mode).
# The -sysforge package rename it triggers is applied generically in
# makepkg_wrapper._run_build (gated on is_optimized_build_mode), not here.
BUILD_MODE = "pgo_mesa"


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


def resolve_store(tcfg: dict | None = None) -> Path:
    """Resolve mesa's PGO store dir (``<profile_store_root>/pgo-mesa``).

    ``tcfg`` defaults to a fresh toolchain.toml load so callers that only have a
    pkgbuild path don't have to thread config through. The directory itself is
    provisioned by the caller via ``fs_provision.ensure_writable_dir`` — this is
    pure path math, no I/O.
    """
    return resolve_method_store(tcfg if tcfg is not None else _load_tcfg(), "pgo-mesa")


def profdata_path(tcfg: dict | None = None) -> Path:
    """Path to the merged ``mesa.profdata`` inside the store."""
    return resolve_store(tcfg) / PROFDATA_NAME


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


def reuse_profdata(tcfg: dict | None = None) -> Path | None:
    """Return the merged ``mesa.profdata`` if a prior ``--pgo=use`` left one.

    Durability hook for the ``update`` / plain ``build mesa`` path. Mesa is
    source-tracked, so every ``update`` rebuilds it; without re-applying the
    profile the user already collected (via :func:`use_flags`) the rebuild
    would silently regress to a stock, unprofiled mesa — contradicting the
    one-shot ``build mesa --pgo=use`` the user ran. The *existence* of a merged
    ``mesa.profdata`` in the store is the durable signal that this host opted
    into mesa PGO, so a no-``--pgo`` rebuild reuses it.

    Returns ``None`` when no merged profile exists (never PGO-built, or only
    ``record``-instrumented — bare ``.profraw`` is not consumable), so the
    caller falls back to a normal build. No re-merge happens here: once ``use``
    swaps the instrumented mesa for the optimized one, no new ``.profraw``
    accrues between updates, so the existing merged profile is current. Pure —
    just a path existence check.
    """
    pd = profdata_path(tcfg)
    return pd if pd.is_file() else None


def merge_profraw(store: Path, *, profdata_tool: str = "llvm-profdata") -> Path:
    """Merge every ``.profraw`` in ``store`` into ``store/mesa.profdata``.

    Returns the profdata path on success. Raises :class:`MesaPgoError` when no
    profraw was collected (the user ran ``use`` before exercising the
    instrumented mesa) or when ``llvm-profdata`` is missing or fails — all three
    are clean aborts with an actionable hint, never a silent unprofiled build.
    """
    profraw = list_profraw(store)
    if not profraw:
        raise MesaPgoError(
            f"no .profraw files in {store} — build+install the instrumented mesa "
            "with `sysforge build mesa --pgo=record`, run your graphics workload "
            "(games, compositor, glmark2, …) to exercise it, then re-run "
            "`--pgo=use`."
        )
    if shutil.which(profdata_tool) is None:
        raise MesaPgoError(
            f"{profdata_tool!r} not found on PATH — mesa PGO needs the LLVM "
            "toolchain (llvm-profdata ships with llvm)."
        )
    out = store / PROFDATA_NAME
    _log.ui(f"Merging {len(profraw)} mesa .profraw file(s) → {out}")
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
