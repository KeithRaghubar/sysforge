# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_pgo.py — profile-store resolution (instrumentation PGO + sample/post-link)

Pure helpers that answer "is a saved clang.profdata present and compatible with
the LLVM PKGBUILD about to be built?" for the ``pgo_llvm_toolchain`` build mode,
and — more generally — where each profile-guided/post-link optimization method
keeps its collected profile data.

Every method sysforge grows (instrumentation PGO, AutoFDO, Propeller, BOLT)
shares one shape: *build-for-profiling → collect a profile from a workload →
rebuild consuming the profile*. They therefore share one on-disk root
(``/var/cache/sysforge``) so a single ``fs_provision.ensure_writable_dir``
covers all of them, and one reader/reclaimer does too: ``list_profile_stores``,
``resolve_purge_target`` and ``stores_for_package`` back ``sysforge state
profiles`` and the orphan notice on revert/uninstall (3.2.0-F11). Deletion is
always an explicit user decision — a merged profile's existence is the durable
PGO opt-in (``mesa_pgo.reuse_profdata``), so nothing here purges on its own.
``resolve_pgo_store`` stays the (unchanged) accessor for the original
instrumentation-PGO store; ``resolve_method_store`` is the general accessor
that hands out per-method sibling subdirs.

Reads ``toolchain.toml`` (``pgo_store`` / ``profile_store``) and the profdata
version sidecar; no subprocess, no logging.  The PGO *emission* sites (``[PGO]``
tag) still live in the build orchestrator's conf/run paths and migrate here when
those split out.

Consumed by the build orchestrator (``makepkg_wrapper.run``) and the toolchain
provenance check (``llvm_state``); ``PGOBuildSkipped`` is re-raised up through
``run`` and caught by ``build_core``/``update``.
"""
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.paths import TOOLCHAIN_PATH

# Regenerable PGO profdata cache. FHS: /var/cache is for regenerable cached
# data; the profraw/profdata here is always reproducible by re-running the
# toolchain stage, so it belongs under /var/cache rather than /var/tmp.
_DEFAULT_PGO_STORE = "/var/cache/sysforge/llvm-pgo"

# Shared root for every profile-method store. ``llvm-pgo`` (instrumentation PGO)
# is the original tenant; the sample/post-link methods get sibling subdirs here
# so one provision/purge path covers the lot.
_DEFAULT_PROFILE_STORE_ROOT = "/var/cache/sysforge"

# Pass-1 staging prefix of the toolchain PGO build. Its *parent* holds the
# ``sysforge-pgo.lock`` the stage keeps for the whole build, which is how a
# concurrent build learns that ``pgo_store`` is live training data (3.3.0-B8).
# Lives here, not in the stage's ``constants.py`` (which re-exports it), because
# ``primitives/`` cannot import from ``pipeline/``.
DEFAULT_PGO_STAGING_1 = "/var/tmp/sysforge-llvm-stage1"  # noqa: S108 — stable multi-pass LLVM build path, not a temp file

# Logical method names accepted by ``resolve_method_store``. ``"instr-pgo"``
# aliases the legacy ``resolve_pgo_store`` location (back-compat with the
# ``pgo_store`` config key / ``SYSFORGE_PGO_STORE`` env); the rest map to a
# literal sibling subdir under the shared root. ``"pgo-mesa"`` is mesa's own
# instrumentation-PGO store (distinct from ``instr-pgo``, which is the *compiler*
# self-profile) — its runtime-collected ``.profraw`` and merged ``mesa.profdata``
# live in ``<root>/pgo-mesa``; see ``primitives/mesa_pgo.py``. ``"pgo"`` is the
# generic per-package instrumentation-PGO method (``--pgo`` on any non-mesa
# target): profiles live in ``<root>/pgo/<pkgbase>`` via the ``target`` namespace.
PROFILE_METHODS = frozenset(
    {"instr-pgo", "pgo-mesa", "pgo", "autofdo", "propeller", "bolt"}
)


def resolve_pgo_store(tcfg: dict | None) -> Path:
    """Resolve the PGO profdata store directory (single source of truth).

    Precedence: ``toolchain.toml [pgo_store]`` (explicit config wins) →
    ``SYSFORGE_PGO_STORE`` env override → the FHS default ``_DEFAULT_PGO_STORE``.
    """
    configured = (tcfg or {}).get("pgo_store")
    if configured:
        return Path(configured)
    env = os.environ.get("SYSFORGE_PGO_STORE")
    if env:
        return Path(env)
    return Path(_DEFAULT_PGO_STORE)


def pgo_lock_path(staging1: Path) -> Path:
    """Lock-file path guarding the PGO staging dirs + ``pgo_store``.

    Lives in the parent of staging1 (typically ``/var/tmp``) so neither the
    Pass-1 purge nor the post-build cleanup can delete it. The toolchain stage
    holds it (``toolchain.pgo.pgo_lock``) for the whole build → audit → install
    window; ``makepkg_wrapper.run`` probes it before touching ``pgo_store``.
    """
    return Path(staging1).parent / "sysforge-pgo.lock"


def resolve_pgo_lock_path(tcfg: dict | None) -> Path:
    """:func:`pgo_lock_path` for the configured (``toolchain.toml
    pgo_staging1``) or default Pass-1 staging prefix."""
    return pgo_lock_path(Path((tcfg or {}).get("pgo_staging1", DEFAULT_PGO_STAGING_1)))


def resolve_profile_store_root(tcfg: dict | None) -> Path:
    """Resolve the shared root that holds every profile-method store.

    Precedence: ``toolchain.toml [profile_store]`` (explicit config wins) →
    ``SYSFORGE_PROFILE_STORE`` env override → the FHS default
    ``_DEFAULT_PROFILE_STORE_ROOT``. This is the directory a caller provisions
    once (``fs_provision.ensure_writable_dir``) to cover AutoFDO/Propeller/BOLT.
    """
    configured = (tcfg or {}).get("profile_store")
    if configured:
        return Path(configured)
    env = os.environ.get("SYSFORGE_PROFILE_STORE")
    if env:
        return Path(env)
    return Path(_DEFAULT_PROFILE_STORE_ROOT)


def resolve_method_store(
    tcfg: dict | None, method: str, target: str | None = None
) -> Path:
    """Resolve the on-disk store for one optimization ``method``.

    ``method`` must be one of ``PROFILE_METHODS``. ``"instr-pgo"`` returns the
    legacy ``resolve_pgo_store`` location unchanged (so the existing
    ``pgo_store``/``SYSFORGE_PGO_STORE`` overrides keep working); every other
    method returns ``<profile_store_root>/<method>[/<target>]``.

    ``target`` namespaces per-package profiles within a method (e.g. the kernel
    AutoFDO profile vs a mesa one) — ``autofdo/linux-sysforge/`` — and is
    omitted for methods that keep a single profile.
    """
    if method not in PROFILE_METHODS:
        raise ValueError(
            f"unknown profile method {method!r}; expected one of "
            f"{sorted(PROFILE_METHODS)}"
        )
    if method == "instr-pgo":
        base = resolve_pgo_store(tcfg)
    else:
        base = resolve_profile_store_root(tcfg) / method
    if target:
        base = base / target
    return base


# Methods whose store is namespaced per package (``<method>/<target>``); the
# rest keep one profile per method.
_PER_TARGET_METHODS = frozenset({"pgo", "autofdo", "propeller", "bolt"})

# Kernel FDO round sidecar written next to the profile (3.3.0-B14).
ROUND_SIDECAR = "round.toml"


@dataclass(frozen=True)
class StoreEntry:
    """One non-empty profile store, as ``sysforge state profiles`` lists it."""

    method: str
    target: str | None
    path: Path
    size_bytes: int
    mtime: float
    collected_version: str | None


def _store_entry(method: str, target: str | None, path: Path) -> StoreEntry | None:
    files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else []
    if not files:
        return None
    stats = [p.stat() for p in files]
    version = None
    for sidecar in sorted(path.glob("*.profdata.version")):
        version = sidecar.read_text(encoding="utf-8").strip() or None
        break
    if version is None and (path / ROUND_SIDECAR).is_file():
        # Kernel FDO stores record the profiled pkgver in round.toml (3.3.0-B14);
        # kernel_fdo.read_round is its one reader (missing/malformed → None).
        # Imported here, not at module top: kernel_fdo imports this module at load.
        from sysforge.primitives.kernel_fdo import read_round
        info = read_round(path)
        version = info.pkgver if info else None
    return StoreEntry(
        method=method, target=target, path=path,
        size_bytes=sum(s.st_size for s in stats),
        mtime=max(s.st_mtime for s in stats),
        collected_version=version,
    )


def list_profile_stores(tcfg: dict | None) -> list[StoreEntry]:
    """Every non-empty store, per method (and per target where namespaced).

    Read-only; seeded-empty stores are omitted. A per-target method's
    top-level files (none are written today) would list with ``target=None``.
    """
    out: list[StoreEntry] = []
    for method in sorted(PROFILE_METHODS):
        base = resolve_method_store(tcfg, method)
        if method in _PER_TARGET_METHODS and base.is_dir():
            for sub in sorted(p for p in base.iterdir() if p.is_dir()):
                entry = _store_entry(method, sub.name, sub)
                if entry:
                    out.append(entry)
            loose = [p for p in base.iterdir() if p.is_file()]
            if loose:
                out.append(StoreEntry(
                    method, None, base, sum(p.stat().st_size for p in loose),
                    max(p.stat().st_mtime for p in loose), None))
        else:
            entry = _store_entry(method, None, base)
            if entry:
                out.append(entry)
    return out


def resolve_purge_target(tcfg: dict | None, spec: str) -> Path:
    """``method[/target]`` → the store dir to reclaim. Pure path math.

    Refuses an unknown method (``resolve_method_store`` raises) rather than
    guessing, and a target that is not a single plain path component, so a
    purge can never reach outside its method's store.
    """
    method, _, target = spec.partition("/")
    if target and (target in (".", "..") or "/" in target or not target.strip()):
        raise ValueError(f"invalid profile store target {target!r}")
    return resolve_method_store(tcfg, method, target=target or None)


def stores_for_package(tcfg: dict | None, names) -> list[Path]:
    """Non-empty stores that belong to any of ``names`` (pkgbase / pkgnames).

    Instrumentation PGO keys on pkgbase (mesa-family on its back-compat
    ``pgo-mesa`` store); the kernel sample/post-link methods key on pkgname.
    """
    from sysforge.primitives.profile import is_mesa_pkgbase

    found: list[Path] = []
    for name in dict.fromkeys(n for n in names if n):
        candidates = [
            resolve_method_store(tcfg, "pgo-mesa") if is_mesa_pkgbase(name)
            else resolve_method_store(tcfg, "pgo", target=name),
            *(resolve_method_store(tcfg, m, target=name)
              for m in ("autofdo", "propeller", "bolt")),
        ]
        for path in candidates:
            if path not in found and _store_entry("", None, path):
                found.append(path)
    return found


def _try_load_toml(path: Path) -> dict | None:
    """Load a TOML file, returning None on any error."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
        return None


class PGOBuildSkipped(Exception):
    """
    Raised by run() when build_mode is pgo_llvm_toolchain but profdata is
    absent or version-incompatible and the user chose to skip (or input is
    non-interactive).  Callers (e.g. update.py) should treat this as a
    deliberate skip rather than a build failure.
    """


def _resolve_pgo_state(pkgbuild_path: Path) -> tuple[str, str]:
    """
    Check whether a saved clang.profdata is present and compatible with the
    PKGBUILD being built.

    Returns one of:
      ("ready",    str(profdata_path))  — profdata exists, major version matches
      ("mismatch", reason_str)          — profdata exists but major version differs
      ("absent",   reason_str)          — profdata or sidecar missing / toolchain.toml absent
    """
    toolchain_path = TOOLCHAIN_PATH
    if not toolchain_path.exists():
        return ("absent", "toolchain.toml not found — no pgo_store configured")
    try:
        with toolchain_path.open("rb") as f:
            tcfg = tomllib.load(f)
    except Exception as e:
        return ("absent", f"cannot read toolchain.toml: {e}")

    pgo_store = resolve_pgo_store(tcfg)
    profdata_path = pgo_store / "clang.profdata"
    version_path = pgo_store / "clang.profdata.version"

    if not profdata_path.exists():
        return ("absent", f"no profdata at {profdata_path}")
    if not version_path.exists():
        return ("absent", f"profdata version sidecar missing at {version_path}")

    saved_major = version_path.read_text().strip()

    # Extract the target LLVM major version from the PKGBUILD's pkgver line.
    try:
        content = pkgbuild_path.read_text(encoding="utf-8")
        m = re.search(r"^pkgver=([^\s\n]+)", content, re.MULTILINE)
        if not m:
            return ("absent", "cannot determine pkgver from PKGBUILD")
        target_major = m.group(1).split(".")[0]
    except OSError as e:
        return ("absent", f"cannot read PKGBUILD: {e}")

    if saved_major != target_major:
        return (
            "mismatch",
            f"profdata is from LLVM {saved_major}, building LLVM {target_major}",
        )

    return ("ready", str(profdata_path))
