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
import contextlib
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_pgo import resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives.version import vercmp
from sysforge.primitives import run

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
        with TOOLCHAIN_PATH.open("rb") as f:
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
    """The flag for the optimized (``use``) rebuild: consume the merged profile.

    No warning suppression rides along. This is IR PGO, where a function whose
    control flow changed since collection is reported per function as
    ``function control flow change detected (hash mismatch) … [-Wbackend-plugin]``
    and its counts dropped; the ``-Wno-profile-instr-*`` group only governs
    front-end instrumentation and silenced nothing here. Mesa promotes neither
    ``backend-plugin`` nor a blanket ``-Werror``, so the warnings cannot fail
    the build — and :func:`observe_line` counts them (3.2.0-F9).
    """
    return f"-fprofile-use={profdata}"


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
    accrues between updates. The profile therefore ages as the package moves on;
    :func:`profile_version_verdict` (via :func:`reuse_notice`) says by how much,
    without ever refusing it (3.2.0-B16). Pure — just a path existence check.
    """
    pd = profdata_path(tcfg, pkgbase)
    return pd if pd.is_file() else None


def version_sidecar(profdata: Path) -> Path:
    """``<pkgbase>.profdata.version`` — the version the profile was collected
    against, mirroring the toolchain store's ``clang.profdata.version``."""
    return profdata.with_name(profdata.name + ".version")


def profile_version_verdict(
    profdata: Path, target_version: str
) -> tuple[str, str | None]:
    """How far the package has moved since its profile was collected (3.2.0-B16).

    Returns ``("current", None)`` when the sidecar matches ``target_version``,
    ``("older"|"newer", msg)`` when it differs, and ``("unknown", msg)`` when
    there is no sidecar (every profile collected before it existed). Unlike the
    LLVM store's hard ``mismatch`` — there the profile format tracks the
    compiler — mesa skew is gradual source drift, so every verdict still reuses
    the profile; the message only says how stale it is. An unknown-age profile
    is never discarded: it cost a workload to collect.
    """
    refresh = f"`sysforge build {profdata.stem} --pgo=record`"
    sidecar = version_sidecar(profdata)
    try:
        collected = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        collected = ""
    if not collected:
        return "unknown", (
            f"profile age unknown (collected before version tracking); "
            f"refresh with {refresh}"
        )
    cmp = vercmp(collected, target_version)
    if cmp == 0:
        return "current", None
    return ("older" if cmp < 0 else "newer"), (
        f"profile collected against {collected}, building {target_version}; "
        f"refresh with {refresh}"
    )


def reuse_notice(profdata: Path, pkgbase: str, target_version: str) -> str:
    """The ``ui()`` line for the reuse path, carrying the staleness verdict.

    One line so the answer stays at default verbosity: the profile is
    re-applied either way, and when it is not ``current`` the line says how far
    the package has moved since collection (3.2.0-B16).
    """
    line = (
        f"PGO (reuse) {pkgbase}: re-applying {profdata} from a prior --pgo=use "
        "(source rebuild stays profiled)"
    )
    _status, detail = profile_version_verdict(profdata, target_version)
    return f"{line} — {detail}" if detail else line


def merge_profraw(
    store: Path, *, pkgbase: str | None = "mesa", profdata_tool: str = "llvm-profdata",
    collected_version: str | None = None,
) -> Path:
    """Merge every ``.profraw`` in ``store`` into ``store/<pkgbase>.profdata``.

    Returns the profdata path on success. Raises :class:`MesaPgoError` when no
    profraw was collected (the user ran ``use`` before exercising the
    instrumented mesa) or when ``llvm-profdata`` is missing or fails — all three
    are clean aborts with an actionable hint, never a silent unprofiled build.

    ``collected_version`` — the installed version of the instrumented package
    that produced the ``.profraw`` — is recorded in :func:`version_sidecar` on a
    fresh merge, so :func:`profile_version_verdict` can later say how stale a
    reused profile is (3.2.0-B16). Reusing an existing merge leaves it as is.
    """
    profraw = list_profraw(store)
    out = store / profdata_name(pkgbase)
    if not profraw:
        # No fresh raw — but a prior merge may have already produced (and pruned
        # down to) a consumable profdata. That is the durable artifact, so a
        # re-run of `use` (e.g. after the consuming build failed downstream)
        # reuses it rather than dead-ending. Only abort when there is genuinely
        # nothing collected.
        if out.is_file():
            _log.info(f"Reusing existing merged {pkgbase} profile: {out}")
            return out
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
    # Fold any prior merged profile into the inputs so the accumulated signal
    # survives pruning the raw below (the toolchain stage does the same — the
    # profdata is the durable store, the .profraw is transient). Write to a
    # temp first so `out` can safely also be an input.
    tmp = out.with_suffix(out.suffix + ".tmp")
    inputs = ([str(out)] if out.exists() else []) + [str(p) for p in profraw]
    _log.ui(f"Merging {len(profraw)} {pkgbase} .profraw file(s) → {out}")
    result = run.probe([profdata_tool, "merge", "--output", str(tmp), *inputs])
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MesaPgoError(
            f"{profdata_tool} merge failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    tmp.replace(out)
    if collected_version:
        version_sidecar(out).write_text(collected_version + "\n", encoding="utf-8")
    # Prune the consumed raw — without this every record→use cycle leaks its
    # .profraw into the store unbounded (Q5). The signal now lives in `out`.
    for p in profraw:
        with contextlib.suppress(OSError):
            p.unlink()
    _log.info(f"Merged mesa profile: {out} ({out.stat().st_size} bytes)")
    return out


# ---------------------------------------------------------------------------
# Profile staleness — how much of a reused profile no longer matches (3.2.0-F9)
# ---------------------------------------------------------------------------

_SKEW_RE = re.compile(
    r"(?:warning|error): (?P<file>.+?): function control flow change detected "
    r"\(hash mismatch\) (?P<fn>\S+) Hash"
)
_TOTAL_RE = re.compile(r"^Total functions:\s*(\d+)\s*$", re.MULTILINE)

# Armed by the wrapper for one PGO-consuming build; None = not counting.
_skew_seen: set[tuple[str, str]] | None = None
_skew_reports: list["SkewReport"] = []


def arm_skew_count() -> None:
    """Start counting hash-mismatch warnings for the build about to run."""
    global _skew_seen
    _skew_seen = set()


def observe_line(line: str) -> None:
    """Fed every build-output line by ``makepkg_invoke``; a no-op unless armed.
    De-duplicates by (file, function): mesa compiles some files more than
    once, and ccache replays a cached TU's warnings."""
    if _skew_seen is None or "hash mismatch" not in line:
        return
    m = _SKEW_RE.search(line)
    if m:
        _skew_seen.add((m.group("file"), m.group("fn")))


def take_skew_count() -> set[tuple[str, str]] | None:
    """Return the mismatches seen since :func:`arm_skew_count` and disarm."""
    global _skew_seen
    seen, _skew_seen = _skew_seen, None
    return seen


@dataclass(frozen=True)
class SkewReport:
    pkgbase: str
    mismatched: int
    total: int | None

    @property
    def ratio(self) -> float | None:
        return self.mismatched / self.total if self.total else None


def profile_function_total(profdata: Path, profdata_tool: str = "llvm-profdata") -> int | None:
    """``Total functions`` from ``llvm-profdata show`` — the staleness denominator."""
    r = run.capture([profdata_tool, "show", str(profdata)])
    if r is None or r.returncode != 0:
        return None
    m = _TOTAL_RE.search(r.stdout or "")
    return int(m.group(1)) if m else None


def is_stale(report: SkewReport, threshold: float) -> bool:
    return report.ratio is not None and report.ratio >= threshold


def staleness_line(report: SkewReport, threshold: float) -> str:
    """The ``ui()`` companion to :func:`reuse_notice`."""
    if report.total:
        body = (f"{report.mismatched:,} of {report.total:,} profiled functions "
                f"no longer match the source ({report.ratio:.1%})")
    else:
        body = f"{report.mismatched:,} profiled functions no longer match the source"
    line = f"PGO profile {report.pkgbase}: {body}"
    if is_stale(report, threshold):
        line += (f" — profile is stale; refresh with "
                 f"`sysforge build {report.pkgbase} --pgo=record`")
    return line


def record_skew_report(report: SkewReport) -> None:
    _skew_reports.append(report)


def take_skew_reports() -> list[SkewReport]:
    out = list(_skew_reports)
    _skew_reports.clear()
    return out


def reset_skew_session() -> None:
    global _skew_seen
    _skew_seen = None
    _skew_reports.clear()
