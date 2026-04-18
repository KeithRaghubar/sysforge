"""
doctor.py — health-check an installed package's depends + linkage

Read-only. Never builds or installs. Answers "is this installed package's
declared world still satisfied by the currently installed system?" — the
class of breakage where a partial rebuild leaves an installed package
referencing ABIs that no longer exist (graphics-stack drift, etc.).

For each target package:
  - Read /var/lib/pacman/local/<pkg>-<ver>/ for owned files and depends.
  - Depends check: pacman -T for versioned package deps; soname_satisfied
    for libfoo.so[=N] entries.
  - ABI check: abi_check.check_so_files on the package's owned shared libs.

By default the walk recurses into the target's dep closure (BFS, dedup on
pkgname). --shallow restricts to direct depends only. --graphics expands
to a curated graphics-stack list driven by the hardware overlay's
gpu_vendors. --all verifies every installed package (foreign and
non-foreign); --repo narrows to non-foreign packages only.

Public API:
    cmd_doctor(args)
"""
from __future__ import annotations

import re
import subprocess
import tomllib
from collections import deque
from pathlib import Path

from sysforge import log
from sysforge.primitives import pacman
from sysforge.primitives.abi_check import check_so_files, needed_sonames
from sysforge.primitives.aur_resolve import _strip_version
from sysforge.primitives.dep_analysis import (
    _default_ldconfig_fn,
    _parse_ldconfig,
    soname_satisfied,
)
from sysforge.primitives.provides_lookup import (
    files_db_present,
    suggest_for_soname,
)

_log = log.get_logger("DOC")


# Issue-string soname extractors. Kept close to the formatters in
# _check_depends and abi_check.check_so_files — if either message text
# changes, these patterns need to follow.
_DEP_SONAME_ISSUE_RE = re.compile(r"^soname not found in ldconfig: (\S+)$")
_ABI_NEEDED_ISSUE_RE = re.compile(
    r": NEEDED lib '([^']+)' not found in ldconfig cache"
)
# An "undefined versioned symbol" issue carries the .so basename at the
# start of the line; the symbol itself isn't directly lookup-able, but the
# .so's NEEDED libs are the likely update/rebuild candidates.
_ABI_UNDEF_ISSUE_RE = re.compile(
    r"^(?P<soname>\S+\.so[^\s:]*): undefined versioned symbol "
)


# ---------------------------------------------------------------------------
# Graphics stack expansion
# ---------------------------------------------------------------------------

# Reference data, not config. Variants (-git, lib32) are all listed; the
# expansion filters against `pacman -Q` so only installed ones are checked.
GRAPHICS_BASE = [
    "mesa", "mesa-git",
    "lib32-mesa", "lib32-mesa-git",
    "vulkan-icd-loader", "lib32-vulkan-icd-loader",
    "vulkan-headers", "vulkan-headers-git",
    "libglvnd", "lib32-libglvnd",
    "egl-wayland", "lib32-egl-wayland",
    "xwayland", "xwayland-git",
]

GRAPHICS_BY_VENDOR = {
    "nvidia": [
        "nvidia", "nvidia-dkms",
        "nvidia-open", "nvidia-open-dkms",
        "nvidia-utils", "lib32-nvidia-utils",
    ],
    "amd": [
        "vulkan-radeon", "lib32-vulkan-radeon",
        "libva-mesa-driver", "lib32-libva-mesa-driver",
        "mesa-vdpau", "lib32-mesa-vdpau",
    ],
    "intel": [
        "vulkan-intel", "lib32-vulkan-intel",
        "intel-media-driver",
    ],
}


def _read_gpu_vendors(config) -> list[str]:
    """Read gpu_vendors from hardware_profile.toml. Empty list if absent."""
    hw_path = config.get("hardware_profile") if config else None
    if not hw_path:
        return []
    path = Path(hw_path).expanduser()
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as f:
            hw = tomllib.load(f)
    except Exception as e:
        _log.warn(f"failed to read hardware_profile at {path}: {e}")
        return []
    vendors = hw.get("hardware", {}).get("gpu_vendors", [])
    return [v for v in vendors if isinstance(v, str)]


def _expand_graphics_targets(config, installed: dict[str, str]) -> list[str]:
    """
    Return the list of graphics-stack packages to verify: base stack plus
    per-vendor additions from gpu_vendors, filtered to those actually
    installed (so we don't false-positive on absent lib32 variants).
    """
    candidates = list(GRAPHICS_BASE)
    for vendor in _read_gpu_vendors(config):
        candidates.extend(GRAPHICS_BY_VENDOR.get(vendor, []))
    seen: set[str] = set()
    expanded: list[str] = []
    for name in candidates:
        if name in seen or name not in installed:
            continue
        seen.add(name)
        expanded.append(name)
    return expanded


# ---------------------------------------------------------------------------
# Per-package checks
# ---------------------------------------------------------------------------

def _so_paths_for_pkg(pkgname: str, file_root: Path) -> list[Path]:
    """Return absolute paths to .so / .so.* files owned by pkgname that exist on disk."""
    owned = pacman.get_package_files(pkgname)
    paths: list[Path] = []
    for rel in owned:
        if ".so" not in rel:
            continue
        if rel.endswith(".a"):
            continue
        # Filter to actual ELF files, not plain .so symlinks that point at an
        # unversioned name. .so.* and bare .so both welcome; check_so_files
        # handles non-ELF robustly (nm -D just returns nothing).
        abs_path = file_root / rel
        if abs_path.is_file():
            paths.append(abs_path)
    return paths


def _check_depends(depends: list[str], ldconfig_set: set[str]) -> list[str]:
    """
    Return list of human-readable issue strings for unsatisfied depends.

    Soname entries (libfoo.so[=N]) are checked against ldconfig_set.
    Everything else is batched into one `pacman -T` call.
    """
    issues: list[str] = []
    pkg_specs: list[str] = []
    for entry in depends:
        if ".so" in _strip_version(entry):
            if not soname_satisfied(entry, ldconfig_set):
                issues.append(f"soname not found in ldconfig: {entry}")
        else:
            pkg_specs.append(entry)

    if pkg_specs:
        result = subprocess.run(
            ["pacman", "-T", *pkg_specs],
            capture_output=True, text=True,
        )
        # pacman -T: exit 0 all satisfied; exit 127 some missing; stdout lists them.
        if result.returncode != 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    issues.append(f"unsatisfied dep: {line}")
    return issues


# ---------------------------------------------------------------------------
# Closure walk
# ---------------------------------------------------------------------------

def _walk_closure(roots: list[str], shallow: bool) -> list[str]:
    """
    BFS over %DEPENDS% starting from `roots`, deduped on installed pkgname.
    Returns the ordered list of packages to inspect (roots first).
    Entries that don't resolve to an installed package are dropped from the
    walk but still present in the report as "not installed" at the root level.
    """
    order: list[str] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for r in roots:
        queue.append((r, 0))
    installed = pacman.get_all_installed_packages()

    while queue:
        name, depth = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        order.append(name)
        if name not in installed:
            continue
        if shallow and depth >= 1:
            continue
        for dep in pacman.get_package_depends(name):
            base = _strip_version(dep)
            if ".so" in base:
                continue
            if base in seen or base not in installed:
                # Unknown provides (virtual pkg) → skip; not-installed gets
                # flagged as a depends issue at the parent anyway.
                continue
            queue.append((base, depth + 1))
    return order


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _check_one(pkgname: str, ldconfig_set: set[str],
               installed: dict[str, str],
               file_root: Path) -> tuple[list[str], list[str], list[Path]]:
    """Return (dep_issues, abi_issues, so_paths) for one package."""
    if pkgname not in installed:
        return ([f"{pkgname}: not installed"], [], [])
    depends = pacman.get_package_depends(pkgname)
    dep_issues = _check_depends(depends, ldconfig_set)
    so_paths = _so_paths_for_pkg(pkgname, file_root)
    abi_issues = check_so_files(so_paths)
    return (dep_issues, abi_issues, so_paths)


def _collect_suggestions(pkgname: str,
                         dep_issues: list[str],
                         abi_issues: list[str],
                         so_paths: list[Path] | None = None,
                         cache: dict[tuple[str, bool], list[str]] | None = None,
                         ) -> dict[str, list[str]]:
    """
    For each lookup-able issue, reverse-lookup candidate packages via
    `pacman -Fq`. Returns {issue_text: [candidate, ...]}. Issues with no
    extractable lookup are absent from the mapping.

    Handled shapes:
      - dep: "soname not found in ldconfig: <soname>"
      - abi: "<file>: NEEDED lib '<soname>' not found in ldconfig cache …"
      - abi: "<file>: undefined versioned symbol …" — enumerate the NEEDED
        sonames of <file> and suggest whichever packages own them; those
        are the likely ABI-drift culprits.

    If `cache` is provided it is consulted/populated per (soname, lib32)
    so repeated lookups across issues and across packages in a single
    doctor run collapse to one `pacman -Fq` subprocess per soname.
    """
    lib32 = pkgname.startswith("lib32-")
    by_name: dict[str, Path] = {}
    for p in so_paths or ():
        by_name.setdefault(p.name, p)

    def _lookup(soname: str) -> list[str]:
        key = (soname, lib32)
        if cache is not None and key in cache:
            return cache[key]
        result = suggest_for_soname(soname, lib32=lib32)
        if cache is not None:
            cache[key] = result
        return result

    out: dict[str, list[str]] = {}
    for issue in dep_issues:
        m = _DEP_SONAME_ISSUE_RE.match(issue)
        if not m:
            continue
        out[issue] = _lookup(m.group(1))

    for issue in abi_issues:
        m = _ABI_NEEDED_ISSUE_RE.search(issue)
        if m:
            out[issue] = _lookup(m.group(1))
            continue
        m = _ABI_UNDEF_ISSUE_RE.match(issue)
        if not m:
            continue
        path = by_name.get(m.group("soname"))
        if path is None:
            continue
        seen: set[str] = set()
        merged: list[str] = []
        for soname in needed_sonames(path):
            for cand in _lookup(soname):
                if cand in seen:
                    continue
                seen.add(cand)
                merged.append(cand)
        out[issue] = merged
    return out


def _origin_tag(pkgname: str, foreign: set[str], installed: dict[str, str]) -> str:
    """
    Return "[aur]" for foreign packages, "[repo]" for non-foreign installed
    packages, or "" if the package isn't installed (header already reads
    "(not installed)" in that case).
    """
    if pkgname not in installed:
        return ""
    return "[aur]" if pkgname in foreign else "[repo]"


def _print_report(pkgname: str, version: str | None,
                  dep_issues: list[str], abi_issues: list[str],
                  quiet: bool,
                  suggestions: dict[str, list[str]] | None = None,
                  origin: str = "") -> None:
    clean = not dep_issues and not abi_issues
    if clean and quiet:
        return
    header = f"== {pkgname} {version or '(not installed)'}"
    if origin:
        header += f" {origin}"
    header += " =="
    _log.ui(header)
    if clean:
        _log.ui("  clean")
        return

    def _emit_issue(issue: str) -> None:
        _log.ui(f"    - {issue}")
        if suggestions is None or issue not in suggestions:
            return
        cands = suggestions[issue]
        if cands:
            _log.ui(f"      → provided by: {', '.join(cands)}")
        else:
            _log.ui("      → provided by: no candidate in files db")

    if dep_issues:
        _log.ui(f"  [DEPENDS] {len(dep_issues)} issue(s):")
        for i in dep_issues:
            _emit_issue(i)
    if abi_issues:
        _log.ui(f"  [ABI] {len(abi_issues)} issue(s):")
        for i in abi_issues:
            _emit_issue(i)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    """
    sysforge doctor entry point.

    args attributes:
        packages: list[str]       — positional package names
        graphics: bool            — expand to curated graphics stack
        all: bool                 — verify every installed package (foreign
                                    and non-foreign)
        repo: bool                — verify every non-foreign package
        shallow: bool             — skip transitive dep closure
        quiet: bool               — suppress clean lines in output
        suggest: bool             — look up candidate packages for each
                                    unsatisfied soname via `pacman -Fq`
        config: dict (optional)   — passed through for --graphics overlay read
    """
    config = getattr(args, "config", None) or {}
    file_root = Path("/")

    installed = pacman.get_all_installed_packages()
    foreign = set(pacman.get_foreign_packages().keys())

    # Assemble root targets
    roots: list[str] = list(args.packages or [])
    if args.graphics:
        roots.extend(_expand_graphics_targets(config, installed))
    if args.all:
        roots.extend(installed.keys())
    if getattr(args, "repo", False):
        roots.extend(name for name in installed if name not in foreign)
    # Dedupe preserving order
    seen_roots: set[str] = set()
    deduped: list[str] = []
    for p in roots:
        if p not in seen_roots:
            seen_roots.add(p)
            deduped.append(p)
    roots = deduped

    if not roots:
        _log.error(
            "no packages to check — pass PKG, --graphics, --all, or --repo"
        )
        return 2

    # Warn on any root that isn't installed — the closure walk will still
    # include it so the report shows it explicitly.
    for r in roots:
        if r not in installed:
            _log.warn(f"{r}: not installed — will report as missing")

    targets = _walk_closure(roots, shallow=args.shallow)

    ldconfig_set = _parse_ldconfig(_default_ldconfig_fn())

    suggest = bool(getattr(args, "suggest", False))
    if suggest and not files_db_present():
        _log.warn("--suggest: pacman files db not synced; "
                  "run `sudo pacman -Fy` (or `-Fyy`) first — "
                  "no candidate lookups will be performed")
        suggest = False

    total_issues = 0
    affected_pkgs: list[tuple[str, int, str]] = []
    per_pkg_suggestions: list[tuple[str, list[str]]] = []
    global_candidates: list[str] = []
    global_seen: set[str] = set()
    suggest_cache: dict[tuple[str, bool], list[str]] = {}

    for pkgname in targets:
        dep_issues, abi_issues, so_paths = _check_one(
            pkgname, ldconfig_set, installed, file_root
        )
        origin = _origin_tag(pkgname, foreign, installed)
        n = len(dep_issues) + len(abi_issues)
        if n:
            affected_pkgs.append((pkgname, n, origin))
            total_issues += n
        suggestions = (
            _collect_suggestions(pkgname, dep_issues, abi_issues, so_paths,
                                 cache=suggest_cache)
            if suggest else None
        )
        _print_report(
            pkgname, installed.get(pkgname),
            dep_issues, abi_issues, quiet=args.quiet,
            suggestions=suggestions,
            origin=origin,
        )
        if suggest and suggestions:
            pkg_seen: set[str] = set()
            pkg_flat: list[str] = []
            for cands in suggestions.values():
                for c in cands:
                    if c not in pkg_seen:
                        pkg_seen.add(c)
                        pkg_flat.append(c)
                    if c not in global_seen:
                        global_seen.add(c)
                        global_candidates.append(c)
            if pkg_flat:
                per_pkg_suggestions.append((pkgname, pkg_flat))

    _log.newline()
    _log.ui(
        f"Scanned {len(targets)} package(s); "
        f"{len(affected_pkgs)} with issues, {total_issues} total finding(s)."
    )
    if affected_pkgs:
        names = ", ".join(
            f"{name} {tag} ({n})" if tag else f"{name} ({n})"
            for name, n, tag in affected_pkgs
        )
        _log.ui(f"Affected: {names}")
    if suggest and global_candidates:
        _log.ui("Suggestions:")
        for name, cands in per_pkg_suggestions:
            _log.ui(f"  {name}: {', '.join(cands)}")
        _log.ui(f"Suggested packages: {', '.join(global_candidates)}")
    return 1 if affected_pkgs else 0
