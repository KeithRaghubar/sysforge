"""
aur_resolve.py — recursive AUR dependency resolution

Resolves transitive AUR dependencies for a package by parsing PKGBUILD
depends/makedepends, classifying each as installed/repo/AUR, and
recursively resolving AUR deps until the full graph is known.

Returns a topologically sorted build order so each dep is built and
installed before packages that depend on it.

Public API:
    resolve_aur_deps(pkgbuild_path, config, fetch=True) -> list[ResolvedDep]
    resolve_aur_deps_batch(pkgbuild_paths, config, fetch=True) -> list[ResolvedDep]
    build_resolved_deps(deps, build_options, config) -> list[str]
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur import aur_info, repo_packages
from sysforge.primitives.config import find_pkgbuild
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

# [AUR_RESOLVE], not [RESOLVE]: this resolves the transitive AUR *dependency
# graph* (build order), a different operation from the `resolve` verb's
# profile/rule debugging — they share the word, not the concern.
_log = log.get_logger("AUR_RESOLVE")

# Version constraint operators, longest first so >= matches before >
_VERSION_OPS = (">=", "<=", "!=", "=", ">", "<")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ResolvedDep:
    """A resolved dependency from the AUR dep analysis."""
    name: str
    source: str  # "aur" | "repo" | "installed" | "unknown"
    pkgbuild_path: Path | None = None
    depth: int = 0
    required_by: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_version(dep: str) -> str:
    """Strip version constraints: 'cmake>=3.16' -> 'cmake'."""
    for op in _VERSION_OPS:
        idx = dep.find(op)
        if idx != -1:
            return dep[:idx]
    return dep


def _is_soname(dep: str) -> bool:
    """True if dep looks like a soname (e.g. libfoo.so, libfoo.so=2)."""
    name = _strip_version(dep)
    return ".so" in name


def _looks_unresolved(dep: str) -> bool:
    """True if a dep token still carries un-evaluated shell syntax.

    The static parser resolves brace, array, and scalar expansions, but
    constructs it cannot evaluate (command substitution, conditionals, unknown
    variables) leave a residual token such as ``${_pydeps[@]/#/python-}`` or
    ``$(uname -m)``.  These are not real package names: they must never reach
    ``pacman``/AUR queries, and their presence signals that discovery should
    fall back to authoritative AUR RPC metadata.
    """
    return any(c in dep for c in "${}`(")


def _deps_need_rpc_rescue(depends: list[str], makedepends: list[str]) -> bool:
    """True if any statically-parsed dep token is still unresolved shell syntax."""
    return any(_looks_unresolved(d) for d in (*depends, *makedepends))


def _get_missing_deps(dep_specs: list[str]) -> list[str]:
    """Return dep specs not satisfied on the local system (pacman -T)."""
    if not dep_specs:
        return []
    result = subprocess.run(
        ["pacman", "-T"] + dep_specs,
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return []
    return [s.strip() for s in result.stdout.splitlines() if s.strip()]


def _get_pkgname(pkgmeta: dict) -> str:
    """Extract a display name from parsed PKGBUILD metadata."""
    globals_ = pkgmeta.get("globals", {})
    name = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(name, list):
        return name[0] if name else "unknown"
    return name


def _get_deps_from_pkgbuild(pkgbuild_path: Path) -> tuple[list[str], list[str]]:
    """Parse a PKGBUILD and return (depends, makedepends)."""
    pkgmeta = parse_pkgbuild(pkgbuild_path)
    globals_ = pkgmeta.get("globals", {})
    return globals_.get("depends", []), globals_.get("makedepends", [])


def _get_deps_from_aur_rpc(name: str, aur_data: dict) -> tuple[list[str], list[str]]:
    """Extract depends/makedepends from AUR RPC response data."""
    info = aur_data.get(name, {})
    depends = info.get("Depends") or []
    makedepends = info.get("MakeDepends") or []
    return depends, makedepends


# ---------------------------------------------------------------------------
# Core recursive resolver
# ---------------------------------------------------------------------------

def _resolve_deps(
    dep_specs: list[str],
    config: dict | None,
    fetch: bool,
    visited: dict[str, ResolvedDep],
    in_progress: set[str],
    order: list[str],
    depth: int,
    required_by: str,
) -> None:
    """Classify deps as installed/repo/AUR; recurse into AUR deps.

    Mutates *visited*, *in_progress*, and *order* in place.  After the
    call, *order* contains AUR dep names in topological build order
    (leaves first).
    """
    # Filter soname deps — handled by dep_analysis.py, not package resolution
    dep_specs = [d for d in dep_specs if not _is_soname(d)]
    if not dep_specs:
        return

    # Build name -> original spec mapping, skip already-visited and any token
    # that still carries un-evaluated shell syntax (not a real package name).
    name_map: dict[str, str] = {}
    for spec in dep_specs:
        if _looks_unresolved(spec):
            _log.info(
                f"skipping unresolved dep token {spec!r} (required by {required_by})"
            )
            continue
        name = _strip_version(spec)
        if name and name not in visited and name not in name_map:
            name_map[name] = spec

    if not name_map:
        return

    names = list(name_map.keys())

    # Check which deps are not installed
    specs_to_check = [name_map[n] for n in names]
    missing_specs = _get_missing_deps(specs_to_check)
    missing_names = {_strip_version(s) for s in missing_specs}

    # Mark installed deps
    for name in names:
        if name not in missing_names:
            visited[name] = ResolvedDep(
                name=name, source="installed", depth=depth,
                required_by=[required_by],
            )

    missing = [n for n in names if n in missing_names]
    if not missing:
        return

    # Classify missing deps: repo vs AUR
    repo_set = repo_packages(missing)
    for name in missing:
        if name in repo_set:
            visited[name] = ResolvedDep(
                name=name, source="repo", depth=depth,
                required_by=[required_by],
            )

    aur_candidates = [n for n in missing if n not in repo_set]
    if not aur_candidates:
        return

    aur_data = aur_info(aur_candidates)

    for name in aur_candidates:
        if name not in aur_data:
            _log.warn(
                f"{name}: not found in repos or AUR "
                f"(required by {required_by})"
            )
            visited[name] = ResolvedDep(
                name=name, source="unknown", depth=depth,
                required_by=[required_by],
            )
            continue

        # Cycle detection
        if name in in_progress:
            raise RuntimeError(
                f"Dependency cycle detected: {name} is already being "
                f"resolved (required by {required_by})"
            )

        in_progress.add(name)

        # Resolve this dep's own dependencies.  When fetching, clone the PKGBUILD
        # so the build step has a local tree, and prefer the static parse for
        # discovery — but fall back to authoritative AUR RPC metadata when the
        # parse fails or still carries un-evaluated shell syntax (command
        # substitution, conditionals) the static parser cannot expand.  RPC
        # ``.SRCINFO`` is fully shell-evaluated, so its dep list is complete.
        pkgbuild_path = None
        child_deps: list[str] = []
        child_makedeps: list[str] = []
        if fetch:
            try:
                pkgbuild_path = find_pkgbuild(name, config)
                child_deps, child_makedeps = _get_deps_from_pkgbuild(pkgbuild_path)
            except (FileNotFoundError, RuntimeError) as e:
                _log.info(f"{name}: PKGBUILD not available ({e}), using AUR RPC metadata")
                child_deps, child_makedeps = _get_deps_from_aur_rpc(name, aur_data)
            else:
                if _deps_need_rpc_rescue(child_deps, child_makedeps):
                    rpc_deps, rpc_makedeps = _get_deps_from_aur_rpc(name, aur_data)
                    if rpc_deps or rpc_makedeps:
                        _log.info(
                            f"{name}: static parse left unresolved dep tokens, "
                            "using AUR RPC metadata for discovery"
                        )
                        child_deps, child_makedeps = rpc_deps, rpc_makedeps
        else:
            child_deps, child_makedeps = _get_deps_from_aur_rpc(name, aur_data)

        # Recurse into this dep's deps
        _resolve_deps(
            child_deps + child_makedeps,
            config, fetch,
            visited, in_progress, order,
            depth=depth + 1, required_by=name,
        )

        in_progress.discard(name)

        visited[name] = ResolvedDep(
            name=name, source="aur", depth=depth,
            required_by=[required_by],
            pkgbuild_path=pkgbuild_path,
        )
        order.append(name)


# ---------------------------------------------------------------------------
# Public API — resolution
# ---------------------------------------------------------------------------

def resolve_aur_deps(
    pkgbuild_path: Path,
    config: dict | None = None,
    *,
    fetch: bool = True,
) -> list[ResolvedDep]:
    """Resolve all transitive AUR deps for a single package.

    Returns a topo-sorted list of ResolvedDep (AUR deps that need
    building, in build order — leaves first).  Repo and installed deps
    are tracked internally but not included in the returned list.

    Args:
        pkgbuild_path: Path to the root package's PKGBUILD.
        config: Loaded flag_profiles config (for find_pkgbuild).
        fetch: If True, clone missing PKGBUILDs.  If False, use AUR RPC
               metadata only (for dry-run / resolve --deps).
    """
    pkgmeta = parse_pkgbuild(pkgbuild_path)
    pkg_name = _get_pkgname(pkgmeta)
    globals_ = pkgmeta.get("globals", {})
    deps = globals_.get("depends", [])
    makedeps = globals_.get("makedepends", [])

    visited: dict[str, ResolvedDep] = {}
    in_progress: set[str] = set()
    order: list[str] = []

    _resolve_deps(
        deps + makedeps, config, fetch,
        visited, in_progress, order,
        depth=0, required_by=pkg_name,
    )

    return [visited[name] for name in order]


def resolve_aur_deps_batch(
    pkgbuild_paths: list[Path],
    config: dict | None = None,
    *,
    fetch: bool = True,
) -> list[ResolvedDep]:
    """Resolve AUR deps for multiple packages, de-duplicated and topo-sorted.

    Shared deps appear once in the returned list at the earliest required
    depth.
    """
    visited: dict[str, ResolvedDep] = {}
    in_progress: set[str] = set()
    order: list[str] = []

    for path in pkgbuild_paths:
        pkgmeta = parse_pkgbuild(path)
        pkg_name = _get_pkgname(pkgmeta)
        globals_ = pkgmeta.get("globals", {})
        deps = globals_.get("depends", [])
        makedeps = globals_.get("makedepends", [])

        _resolve_deps(
            deps + makedeps, config, fetch,
            visited, in_progress, order,
            depth=0, required_by=pkg_name,
        )

    return [visited[name] for name in order]


def resolve_all_deps(
    pkgbuild_path: Path,
    config: dict | None = None,
    *,
    fetch: bool = True,
) -> list[ResolvedDep]:
    """Like resolve_aur_deps but returns ALL deps (installed, repo, AUR).

    Used by ``resolve --deps`` to display the full dependency picture.
    AUR deps are in topological build order; repo/installed deps are
    appended in discovery order.
    """
    pkgmeta = parse_pkgbuild(pkgbuild_path)
    pkg_name = _get_pkgname(pkgmeta)
    globals_ = pkgmeta.get("globals", {})
    deps = globals_.get("depends", [])
    makedeps = globals_.get("makedepends", [])

    visited: dict[str, ResolvedDep] = {}
    in_progress: set[str] = set()
    order: list[str] = []

    _resolve_deps(
        deps + makedeps, config, fetch,
        visited, in_progress, order,
        depth=0, required_by=pkg_name,
    )

    # Build the result: AUR deps in topo order first, then repo/installed
    aur_deps = [visited[name] for name in order]
    other_deps = [d for d in visited.values() if d.source != "aur"]
    return aur_deps + other_deps


# ---------------------------------------------------------------------------
# Public API — building
# ---------------------------------------------------------------------------

def build_resolved_deps(
    deps: list[ResolvedDep],
    *,
    profile_conf: str | None = None,
    cc_override: str | None = None,
    cxx_override: str | None = None,
    ld_override: str | None = None,
    state_dir: Path | None = None,
) -> list[str]:
    """Build and install AUR deps in topological order.

    Each dep is built via makepkg_wrapper.run() with ``-i`` (install)
    appended so it is available for subsequent deps.

    Returns list of successfully built dep names.
    """
    from sysforge.primitives.makepkg_wrapper import (
        BuildOptions,
        run as makepkg_run,
    )

    aur_deps = [d for d in deps if d.source == "aur" and d.pkgbuild_path]
    if not aur_deps:
        return []

    _log.ui(f"Building {len(aur_deps)} AUR dependency(ies) before main package")

    from sysforge.ui import progress as _ui_progress
    built: list[str] = []
    with _ui_progress.tracker(len(aur_deps), "AUR dep") as _tick:
        for i, dep in enumerate(aur_deps):
            req = ", ".join(dep.required_by)
            _tick(dep.name)
            _log.ui(f"  [{i + 1}/{len(aur_deps)}] {dep.name} (required by {req})")

            opts = BuildOptions(
                extra_flags=["-i"],
                profile_conf=profile_conf,
                cc_override=cc_override,
                cxx_override=cxx_override,
                ld_override=ld_override,
                state_dir=state_dir,
                init_session=(i == 0),
                pkg_log=False,
            )
            makepkg_run(dep.pkgbuild_path, options=opts)
            built.append(dep.name)

    _log.ui(f"All {len(built)} dependency(ies) built and installed")
    return built
