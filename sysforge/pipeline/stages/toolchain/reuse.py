# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/reuse.py — opt-in input-fingerprint reuse for Pass 4.

Pass 4 rebuilds the whole suite against the merged profile, which is the
expensive half of a PGO run. When nothing that feeds a package changed —
sources, flags, the profile, the staged deps it links against — that rebuild
produces a bit-identical result, so it can be skipped.

``ReuseCtx`` carries the per-pass inputs and ``pkg_fingerprint`` folds them into
one hash via ``primitives/build_fingerprint.py``. Correctness here is
one-directional: a fingerprint that is too *coarse* silently ships a stale
binary, so every input that can change the output must be in the hash, and the
feature stays opt-in.
"""
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from sysforge.primitives import build_fingerprint

from sysforge.pipeline.stages.toolchain import verify

from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Single-package build helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pass-4 input-fingerprint reuse (opt-in). See primitives/build_fingerprint.py
# and DESIGN.md §Toolchain stage → Pass-4 input-fingerprint reuse.
# ---------------------------------------------------------------------------


def reuse_cache_path(pgo_store: Path) -> Path:
    """Persistent location for the Pass-4 input-fingerprint reuse cache.

    Deliberately a *sibling* of ``pgo_store`` (``<pgo_store>.parent/build-cache/
    build_cache.json``), never inside it: a fresh 4-pass run empties
    ``pgo_store`` (``empty_dir_contents``) at startup, which would otherwise wipe
    the cache a prior run recorded before it is ever consulted. Each cache entry
    already binds its own ``profdata_sha``, so a surviving-but-stale entry from a
    different profdata simply misses (fail-safe) rather than mis-hitting (B14).
    """
    return pgo_store.parent / "build-cache" / "build_cache.json"


@dataclass
class ReuseCtx:
    """Per-sub-pass context for input-fingerprint build reuse (Pass 4 only).

    ``consult`` gates whether a fingerprint match *skips* the build (opt-in via
    ``--reuse-built`` / ``reuse_unchanged``); the cache is *written* regardless
    so a first, non-opted-in run still populates it for a later resume.
    ``staged_dep_fps`` carries the prior sub-pass's fingerprints (Merkle chain:
    4b/4c fold in 4a's so a rebuilt libLLVM forces its consumers to rebuild).
    ``exclude_deps`` is the toolchain build-set (llvm, llvm-libs, clang, …) whose
    *installed* version must be kept out of ``makedep_versions``: Pass 4 satisfies
    those from the staging prefix (``--nodeps``), so their live-``/usr`` version is
    not a build input, and the staged libLLVM they actually link is already folded
    in via ``staged_dep_fps``. Including it would spuriously invalidate every
    Pass-4 fingerprint the moment the suite is self-installed (2.5.1-B2).
    """
    pass_id: str
    cache: dict
    cache_path: Path
    config_digest: str
    profdata_sha: str | None
    pkgdest: Path | None
    consult: bool
    staged_dep_fps: list[str] = field(default_factory=list)
    exclude_deps: frozenset[str] = frozenset()


def _dep_versions_from_globals(
    globals_: dict, exclude: frozenset[str] | set[str] = frozenset(),
) -> dict[str, str | None]:
    """Installed versions of a PKGBUILD's build deps, for fingerprinting.

    Takes already-parsed PKGBUILD globals, collects depends+makedepends+
    checkdepends (arch arrays already merged by ``parse_pkgbuild``), strips
    version constraints, drops unresolved ``${...}``/``$(...)`` tokens, and
    queries pacman for each installed version.

    ``exclude`` names the toolchain build-set members (llvm, llvm-libs, clang, …)
    to omit: during Pass 4 those are satisfied from a stage prefix (``--nodeps``),
    so their *installed* version is not a build input and the staged libLLVM they
    link is already captured via ``staged_dep_fps``. Folding their live-``/usr``
    version in here would flip every consumer's fingerprint the instant the suite
    is self-installed, defeating cache reuse on a post-install re-run (2.5.1-B2).
    External deps (cmake, ninja, glibc, …) do come from ``/usr`` and stay.
    """
    from sysforge.primitives.aur_resolve import _looks_unresolved, _strip_version

    names: set[str] = set()
    for key in ("depends", "makedepends", "checkdepends"):
        for tok in globals_.get(key, []) or []:
            if not tok or _looks_unresolved(tok):
                continue
            bare = _strip_version(tok)
            if bare and bare not in exclude:
                names.add(bare)
    if not names:
        return {}
    return dict(sorted(verify.query_pacman_versions(tuple(sorted(names))).items()))


def pkg_fingerprint(
    ctx: ReuseCtx,
    name: str,
    pkgbuild_path: Path,
    cc: str | None,
    compiler_flags_extra: str | None,
    linker_flags_extra: str | None,
    cmake_llvm_dir: str | None,
    extra_flags,
) -> tuple[str, str]:
    """Return ``(pkgbase, fingerprint)`` for one PKGBUILD in pass ``ctx.pass_id``.

    Parses the PKGBUILD once (for pkgbase + dep versions) and folds every input
    that determines the build output into the fingerprint. Parse failure is
    non-fatal — the recipe is still captured by ``pkgbuild_sha`` and the missing
    metadata only ever over-invalidates (forces a rebuild), never under.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    try:
        globals_ = parse_pkgbuild(pkgbuild_path).get("globals", {})
    except Exception:  # noqa: BLE001 — fingerprint helper must never abort a build
        globals_ = {}
    pkgbase = globals_.get("pkgbase") or name
    components = {
        "pass_id": ctx.pass_id,
        "pkgbase": pkgbase,
        "pkgbuild_sha": build_fingerprint.hash_file(pkgbuild_path),
        "source_commit": build_fingerprint.source_commit(pkgbuild_path.parent),
        # Pass-4 reuse must survive the staged→installed compiler swap: the
        # profgen run builds with the staged stage-2 clang while a profdata-reuse
        # resume builds with /usr/bin/clang — different binaries at different
        # paths, so clang_identity's path+size+mtime would guarantee a cache
        # miss. Key only on the compiler --version line (a genuine version bump
        # still invalidates); the actual codegen carrier is pinned by
        # profdata_sha below, which already folds in the trained toolchain (B14).
        "cc_identity": build_fingerprint.compiler_version_line(cc),
        "compiler_flags_extra": compiler_flags_extra,
        "linker_flags_extra": linker_flags_extra,
        "cmake_llvm_dir": cmake_llvm_dir,
        "extra_flags": list(extra_flags or []),
        "config_digest": ctx.config_digest,
        "profdata_sha": ctx.profdata_sha,
        "makedep_versions": _dep_versions_from_globals(globals_, ctx.exclude_deps),
        "staged_dep_fps": sorted(ctx.staged_dep_fps),
    }
    return pkgbase, build_fingerprint.compute_fingerprint(components)
