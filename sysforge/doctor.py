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
from sysforge.primitives.abi_check import (
    check_so_files,
    is_abi_check_skipped_package,
    needed_sonames,
)
from sysforge.primitives.aur_resolve import _strip_version
from sysforge.primitives.dep_analysis import (
    _default_ldconfig_fn,
    _parse_ldconfig,
    soname_available,
)
from sysforge.primitives.graphics_probe import (
    SEV_ERROR as _GFX_SEV_ERROR,
    check_system_graphics,
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
    "egl-wayland", "lib32-egl-wayland", "egl-wayland-git", "lib32-egl-wayland-git",
    "xwayland", "xwayland-git",
    "xorg-xwayland", "xorg-xwayland-git",
    "wayland", "lib32-wayland",
    "libdrm", "lib32-libdrm",
    "libva", "lib32-libva",
    "libvdpau", "lib32-libvdpau",
    "gamescope",
]

GRAPHICS_BY_VENDOR = {
    "nvidia": [
        "nvidia", "nvidia-dkms",
        "nvidia-open", "nvidia-open-dkms",
        "nvidia-utils", "lib32-nvidia-utils",
        "nvidia-settings",
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
    """
    Return GPU vendors for the current box.

    Preferred source is `hardware_profile.toml` (written by the hardware
    pipeline stage). When absent or unreadable, fall back to parsing
    `lspci -nn`. Keeps `--graphics` and graphics_probe working on systems
    that haven't run the hardware stage.
    """
    hw_path = config.get("hardware_profile") if config else None
    if hw_path:
        path = Path(hw_path).expanduser()
        if path.is_file():
            try:
                with open(path, "rb") as f:
                    hw = tomllib.load(f)
                vendors = hw.get("hardware", {}).get("gpu_vendors", [])
                result = [v for v in vendors if isinstance(v, str)]
                if result:
                    return result
            except Exception as e:
                _log.warn(f"failed to read hardware_profile at {path}: {e}")
    return _detect_gpu_vendors_via_lspci()


def _detect_gpu_vendors_via_lspci() -> list[str]:
    """
    Run `lspci -nn` and map VGA/3D-controller lines to vendor names.
    Returns an empty list if lspci is missing or fails.
    """
    try:
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    vendors: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        # Match VGA compatible controller / 3D controller / Display controller
        if not re.search(r"VGA compatible controller|3D controller|Display controller",
                         line, re.IGNORECASE):
            continue
        lower = line.lower()
        if "nvidia" in lower:
            vendor = "nvidia"
        elif "advanced micro devices" in lower or " amd " in lower or "[amd/ati]" in lower:
            vendor = "amd"
        elif "intel" in lower:
            vendor = "intel"
        else:
            continue
        if vendor not in seen:
            seen.add(vendor)
            vendors.append(vendor)
    return vendors


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


def _check_depends(depends: list[str], ldconfig_set: set[str],
                   pkgname: str = "") -> list[str]:
    """
    Return list of human-readable issue strings for unsatisfied depends.

    Soname entries (libfoo.so[=N]) are checked against ldconfig_set with
    a filesystem fallback (so a stale /etc/ld.so.cache doesn't generate
    false positives). Everything else is batched into one `pacman -T` call.
    """
    lib32 = pkgname.startswith("lib32-")
    issues: list[str] = []
    pkg_specs: list[str] = []
    for entry in depends:
        if ".so" in _strip_version(entry):
            if not soname_available(entry, ldconfig_set, lib32=lib32):
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
               file_root: Path) -> tuple[list[str], list[str], list[Path], bool]:
    """
    Return (dep_issues, abi_issues, so_paths, abi_skipped) for one package.

    abi_skipped is True when the package is in abi_check's bundled-binary
    skip list — its ABI pass is suppressed, but the depends check still runs.
    """
    if pkgname not in installed:
        return ([f"{pkgname}: not installed"], [], [], False)
    depends = pacman.get_package_depends(pkgname)
    dep_issues = _check_depends(depends, ldconfig_set, pkgname=pkgname)
    so_paths = _so_paths_for_pkg(pkgname, file_root)
    if is_abi_check_skipped_package(pkgname):
        return (dep_issues, [], so_paths, True)
    abi_issues = check_so_files(so_paths)
    return (dep_issues, abi_issues, so_paths, False)


# Suggestion kinds. Each finding implies a different fix action:
#   "install"      — soname missing on disk; the candidate package should be
#                    installed. Already-installed candidates are filtered out
#                    at lookup time so this list never re-recommends what's
#                    already on the system.
#   "abi_drift"    — soname IS installed but its exported versioned symbols
#                    don't match what the dependent .so was built against.
#                    Subdivided into:
#   "rebuild"      — abi_drift candidate that IS installed AND is foreign
#                    (locally built / AUR) → rebuild it against the current
#                    system. The actionable drift bucket.
#   "repo_rebuild" — abi_drift candidate that IS installed but comes from an
#                    official repo. Not actionable through the foreign-package
#                    rebuild flow; surfaced as informational so the user knows
#                    drift exists without polluting the actionable list.
#   abi_drift findings whose candidates are NOT installed remain "install"
#   (the upgrade source is missing entirely).
SUGGEST_KIND_INSTALL = "install"
SUGGEST_KIND_ABI_DRIFT = "abi_drift"
SUGGEST_KIND_REBUILD = "rebuild"
SUGGEST_KIND_REPO_REBUILD = "repo_rebuild"


def _collect_suggestions(pkgname: str,
                         dep_issues: list[str],
                         abi_issues: list[str],
                         so_paths: list[Path] | None = None,
                         cache: dict[tuple[str, bool, bool], list[str]] | None = None,
                         installed_names: set[str] | None = None,
                         foreign: set[str] | None = None,
                         ) -> dict[str, tuple[str, list[str]]]:
    """
    For each lookup-able issue, reverse-lookup candidate packages via
    `pacman -Fq`. Returns {issue_text: (kind, [candidate, ...])}. Issues
    with no extractable lookup are absent from the mapping.

    Handled shapes:
      - dep: "soname not found in ldconfig: <soname>"       (kind=install)
      - abi: "<file>: NEEDED lib '<soname>' not found …"    (kind=install)
      - abi: "<file>: undefined versioned symbol …"         (kind=abi_drift)
        — enumerate the NEEDED sonames of <file> and suggest whichever
        packages own them; those are the likely ABI-drift culprits and
        the fix is upgrade or rebuild, not reinstall.

    For ``install`` kind, the ``installed_names`` filter (if provided) is
    applied at the `suggest_for_soname` layer so the returned candidate
    list only contains packages the user does not yet have installed.

    For ``abi_drift`` kind, the candidate set is partitioned three ways
    when ``installed_names`` is supplied:
      * installed AND in ``foreign`` → ``rebuild`` (actionable: rebuild
        the locally-built package against current libs)
      * installed but NOT in ``foreign`` → ``repo_rebuild`` (informational:
        repo package whose drift the user can't fix locally — they must
        await a repo update or `pacman -S` reinstall)
      * not installed → ``install`` (upgrade source missing entirely)
    When ``foreign`` is None (legacy callers), the foreign-vs-repo split is
    skipped and any installed candidate is classified as ``rebuild``.
    A single `abi_drift` finding can split into up to three entries in the
    result dict, distinguished by issue suffix.

    If `cache` is provided it is consulted/populated per
    ``(soname, lib32, filter_installed)`` so repeated lookups across
    issues and across packages in a single doctor run collapse to one
    `pacman -Fq` subprocess per soname per filter mode.
    """
    lib32 = pkgname.startswith("lib32-")
    by_name: dict[str, Path] = {}
    for p in so_paths or ():
        by_name.setdefault(p.name, p)

    def _lookup(soname: str, *, filter_installed: bool) -> list[str]:
        key = (soname, lib32, filter_installed)
        if cache is not None and key in cache:
            return cache[key]
        names = installed_names if filter_installed else None
        result = suggest_for_soname(soname, lib32=lib32, installed_names=names)
        if cache is not None:
            cache[key] = result
        return result

    out: dict[str, tuple[str, list[str]]] = {}
    for issue in dep_issues:
        m = _DEP_SONAME_ISSUE_RE.match(issue)
        if not m:
            continue
        out[issue] = (SUGGEST_KIND_INSTALL,
                      _lookup(m.group(1), filter_installed=True))

    for issue in abi_issues:
        m = _ABI_NEEDED_ISSUE_RE.search(issue)
        if m:
            out[issue] = (SUGGEST_KIND_INSTALL,
                          _lookup(m.group(1), filter_installed=True))
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
            for cand in _lookup(soname, filter_installed=False):
                if cand in seen:
                    continue
                seen.add(cand)
                merged.append(cand)
        if installed_names is not None:
            rebuild_foreign: list[str] = []
            rebuild_repo: list[str] = []
            install: list[str] = []
            for c in merged:
                bare = _bare_pkgname(c)
                if bare not in installed_names:
                    install.append(c)
                elif foreign is not None and bare not in foreign:
                    rebuild_repo.append(c)
                else:
                    rebuild_foreign.append(c)
            if rebuild_foreign:
                out[issue] = (SUGGEST_KIND_REBUILD, rebuild_foreign)
            if rebuild_repo:
                key = issue if not rebuild_foreign else f"{issue} [repo]"
                out[key] = (SUGGEST_KIND_REPO_REBUILD, rebuild_repo)
            if install:
                key = (issue if not (rebuild_foreign or rebuild_repo)
                       else f"{issue} [missing source]")
                out[key] = (SUGGEST_KIND_INSTALL, install)
        else:
            out[issue] = (SUGGEST_KIND_ABI_DRIFT, merged)
    return out


def _bare_pkgname(candidate: str) -> str:
    """Strip the optional ``repo/`` prefix that `pacman -Fq` emits."""
    return candidate.split("/", 1)[1] if "/" in candidate else candidate


def _origin_tag(pkgname: str, foreign: set[str], installed: dict[str, str],
                tracked: set[str] | None = None) -> str:
    """
    Return ``[aur]`` for foreign packages, ``[repo]`` for non-foreign installed
    packages, or ``""`` if the package isn't installed (header already reads
    ``(not installed)`` in that case).

    When ``tracked`` is supplied (the set of pkgnames known to
    ``build_state.toml``), foreign packages absent from it are tagged
    ``[aur][untracked]`` — the cheap signal that doctor sees the package
    but ``sysforge update`` won't rebuild it from a known PKGBUILD without
    a fresh fetch.
    """
    if pkgname not in installed:
        return ""
    if pkgname in foreign:
        if tracked is not None and pkgname not in tracked:
            return "[aur][untracked]"
        return "[aur]"
    return "[repo]"


_SUGGEST_LABEL = {
    SUGGEST_KIND_INSTALL: "install candidate",
    SUGGEST_KIND_ABI_DRIFT: "ABI-drift candidate (rebuild/upgrade)",
    SUGGEST_KIND_REBUILD: "rebuild candidate",
    SUGGEST_KIND_REPO_REBUILD: "repo rebuild candidate (await repo update)",
}


def _print_report(pkgname: str, version: str | None,
                  dep_issues: list[str], abi_issues: list[str],
                  quiet: bool,
                  suggestions: dict[str, tuple[str, list[str]]] | None = None,
                  origin: str = "",
                  abi_skipped: bool = False) -> None:
    clean = not dep_issues and not abi_issues
    if clean and quiet and not abi_skipped:
        return
    header = f"== {pkgname} {version or '(not installed)'}"
    if origin:
        header += f" {origin}"
    header += " =="
    _log.ui(header)
    if clean and not abi_skipped:
        _log.ui("  clean")
        return

    def _emit_issue(issue: str) -> None:
        _log.ui(f"    - {issue}")
        if suggestions is None or issue not in suggestions:
            return
        kind, cands = suggestions[issue]
        label = _SUGGEST_LABEL.get(kind, "candidate")
        if cands:
            _log.ui(f"      → {label}: {', '.join(cands)}")
        elif kind == SUGGEST_KIND_INSTALL:
            _log.ui("      → all owning packages already installed; "
                    "try `sudo ldconfig`, then re-run doctor")
        else:
            _log.ui(f"      → {label}: no candidate in files db")

    if dep_issues:
        _log.ui(f"  [DEPENDS] {len(dep_issues)} issue(s):")
        for i in dep_issues:
            _emit_issue(i)
    if abi_skipped:
        _log.ui("  [ABI] skipped: vendored prebuilt binaries "
                "(reinstall cannot change on-disk symbols)")
    elif abi_issues:
        _log.ui(f"  [ABI] {len(abi_issues)} issue(s):")
        for i in abi_issues:
            _emit_issue(i)


# ---------------------------------------------------------------------------
# Hardware / boot-readiness checks (--hardware)
# ---------------------------------------------------------------------------

def _emit_hardware_checks() -> int:
    """Render device-driver coverage + running-kernel boot-config gaps.

    Mirrors the ``--graphics`` system-probe block: prints each finding as
    ``[SEV] check_id: message → remediation`` and returns the count of
    ``error``-severity findings for the exit code. Surfaces the same
    device/boot-config audit the kernel stage runs, but against the *running*
    kernel — the on-the-spot diagnostic for "device X has no driver".
    """
    from sysforge.primitives import device_probe, kernel_safety
    from sysforge.primitives.dep_analysis import _parse_kernel_config

    devices = device_probe.enumerate_devices()
    findings = list(device_probe.check_unsupported_devices(devices=devices))

    running_cfg = _parse_kernel_config()
    if running_cfg:
        findings += kernel_safety.audit_resolved_config(running_cfg, devices=devices)

    _log.newline()
    _log.ui("== hardware checks ==")
    if not findings:
        _log.ui("  no unsupported devices or boot-config gaps detected")
        return 0

    error_count = 0
    for f in findings:
        _log.ui(f"  [{f.severity.upper()}] {f.check_id}: {f.message}")
        remediation = getattr(f, "remediation", "")
        if remediation:
            _log.ui(f"      → {remediation}")
        if f.severity == _GFX_SEV_ERROR:
            error_count += 1
    _log.ui(
        f"Hardware probe: {len(findings)} finding(s), {error_count} error(s)."
    )
    return error_count


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
        apply: bool               — hand REBUILD candidates to sysforge
                                    update for actual rebuild (implies suggest)
        no_confirm: bool          — skip the y/N prompt before --apply
        dry_run: bool             — report --apply work without rebuilding
        config: dict (optional)   — passed through for --graphics overlay read
    """
    config = getattr(args, "config", None) or {}
    file_root = Path("/")
    apply_requested = bool(getattr(args, "apply", False))
    if apply_requested:
        # --apply has nothing to act on without --suggest's classified candidates.
        args.suggest = True

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
        # --hardware is a system probe with no package targets — run it
        # standalone rather than erroring on the empty package set.
        if getattr(args, "hardware", False):
            return 1 if _emit_hardware_checks() else 0
        _log.error(
            "no packages to check — pass PKG, --graphics, --hardware, --all, "
            "or --repo"
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

    installed_name_set = set(installed.keys())

    # Used to tag foreign packages with `[untracked]` when they have no
    # build_state.toml entry. Failure to read the state file is non-fatal —
    # without `tracked` the legacy behaviour (no [untracked] suffix) holds.
    tracked_names: set[str] | None = None
    try:
        from sysforge.primitives.build_state import BuildState
        from sysforge.pipeline.state import resolve_state_dir
        state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
        tracked_names = set(BuildState(state_dir).all_packages())
    except Exception:
        tracked_names = None

    total_issues = 0
    affected_pkgs: list[tuple[str, int, str]] = []
    per_pkg_suggestions: list[
        tuple[str, list[str], list[str], list[str], list[str]]
    ] = []
    global_install: list[str] = []
    global_rebuild: list[str] = []
    global_repo_rebuild: list[str] = []
    global_drift: list[str] = []
    global_install_seen: set[str] = set()
    global_rebuild_seen: set[str] = set()
    global_repo_rebuild_seen: set[str] = set()
    global_drift_seen: set[str] = set()
    suggest_cache: dict[tuple[str, bool, bool], list[str]] = {}

    for pkgname in targets:
        dep_issues, abi_issues, so_paths, abi_skipped = _check_one(
            pkgname, ldconfig_set, installed, file_root
        )
        origin = _origin_tag(pkgname, foreign, installed, tracked=tracked_names)
        n = len(dep_issues) + len(abi_issues)
        if n:
            affected_pkgs.append((pkgname, n, origin))
            total_issues += n
        suggestions = (
            _collect_suggestions(pkgname, dep_issues, abi_issues, so_paths,
                                 cache=suggest_cache,
                                 installed_names=installed_name_set,
                                 foreign=foreign)
            if suggest else None
        )
        _print_report(
            pkgname, installed.get(pkgname),
            dep_issues, abi_issues, quiet=args.quiet,
            suggestions=suggestions,
            origin=origin,
            abi_skipped=abi_skipped,
        )
        if suggest and suggestions:
            pkg_install: list[str] = []
            pkg_rebuild: list[str] = []
            pkg_repo_rebuild: list[str] = []
            pkg_drift: list[str] = []
            pkg_install_seen: set[str] = set()
            pkg_rebuild_seen: set[str] = set()
            pkg_repo_rebuild_seen: set[str] = set()
            pkg_drift_seen: set[str] = set()
            buckets = {
                SUGGEST_KIND_INSTALL: (pkg_install, pkg_install_seen,
                                       global_install, global_install_seen),
                SUGGEST_KIND_REBUILD: (pkg_rebuild, pkg_rebuild_seen,
                                       global_rebuild, global_rebuild_seen),
                SUGGEST_KIND_REPO_REBUILD: (pkg_repo_rebuild,
                                            pkg_repo_rebuild_seen,
                                            global_repo_rebuild,
                                            global_repo_rebuild_seen),
                SUGGEST_KIND_ABI_DRIFT: (pkg_drift, pkg_drift_seen,
                                         global_drift, global_drift_seen),
            }
            for kind, cands in suggestions.values():
                if kind not in buckets:
                    continue
                local, local_seen, gbl, gbl_seen = buckets[kind]
                for c in cands:
                    if c not in local_seen:
                        local_seen.add(c)
                        local.append(c)
                    if c not in gbl_seen:
                        gbl_seen.add(c)
                        gbl.append(c)
            if pkg_install or pkg_rebuild or pkg_repo_rebuild or pkg_drift:
                per_pkg_suggestions.append(
                    (pkgname, pkg_install, pkg_rebuild,
                     pkg_repo_rebuild, pkg_drift)
                )

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
    if suggest and (global_install or global_rebuild
                    or global_repo_rebuild or global_drift):
        _log.ui("Suggestions:")
        for name, inst, rebuild, repo_rebuild, drift in per_pkg_suggestions:
            parts: list[str] = []
            if inst:
                parts.append(f"install: {', '.join(inst)}")
            if rebuild:
                parts.append(f"rebuild: {', '.join(rebuild)}")
            if repo_rebuild:
                parts.append(f"repo-rebuild: {', '.join(repo_rebuild)}")
            if drift:
                parts.append(f"ABI-drift: {', '.join(drift)}")
            _log.ui(f"  {name}: {' | '.join(parts)}")
        if global_install:
            _log.ui(f"Install candidates: {', '.join(global_install)}")
        if global_rebuild:
            _log.ui(
                "Rebuild candidates (foreign; ABI drift): "
                f"{', '.join(global_rebuild)}"
            )
        if global_repo_rebuild:
            _log.ui(
                "Repo packages with ABI drift "
                "(await repo update or `sudo pacman -S` to reinstall): "
                f"{', '.join(global_repo_rebuild)}"
            )
        if global_drift:
            _log.ui(
                "ABI-drift candidates (rebuild or upgrade, not reinstall): "
                f"{', '.join(global_drift)}"
            )

    # System-state graphics probes — only under --graphics.
    gfx_error_count = 0
    if args.graphics:
        gpu_vendors = _read_gpu_vendors(config)
        findings = check_system_graphics(config, gpu_vendors=gpu_vendors)
        if findings:
            _log.newline()
            _log.ui("== system graphics checks ==")
            for f in findings:
                _log.ui(f"  [{f.severity.upper()}] {f.check_id}: {f.message}")
                if f.remediation:
                    _log.ui(f"      → {f.remediation}")
                if f.severity == _GFX_SEV_ERROR:
                    gfx_error_count += 1
            _log.ui(
                f"Graphics probe: {len(findings)} finding(s), "
                f"{gfx_error_count} error(s)."
            )

    # System-state hardware probes — under --hardware (alongside a package walk).
    hw_error_count = 0
    if getattr(args, "hardware", False):
        hw_error_count = _emit_hardware_checks()

    # --apply bridge: hand REBUILD candidates to `sysforge update`.
    if apply_requested:
        rc = _apply_rebuilds(args, global_rebuild, global_install,
                             foreign, installed)
        # When apply runs, its return code dominates so a successful rebuild
        # produces exit 0 even if doctor found issues.
        return rc

    if affected_pkgs or gfx_error_count or hw_error_count:
        return 1
    return 0


def _apply_rebuilds(
    args, rebuild_candidates: list[str], install_candidates: list[str],
    foreign: set[str], installed: dict[str, str],
) -> int:
    """Hand REBUILD-classified candidates to ``sysforge update``.

    Filters to packages eligible for sysforge's rebuild path (foreign packages,
    or repo packages that ``sysforge update`` would walk under
    ``[build] repo_mode = "profiled"``). Repo candidates outside that
    scope are surfaced as informational (``run: sudo pacman -S ...``) rather
    than invoked. Install candidates (not yet installed) are out of v1.x
    scope — printed as a hint, never run.
    """
    eligible: list[str] = []
    pacman_only: list[str] = []
    for cand in rebuild_candidates:
        bare = cand.split("/", 1)[1] if "/" in cand else cand
        if bare not in installed:
            # Defensive: REBUILD by definition implies installed; skip stragglers.
            continue
        if bare in foreign:
            eligible.append(bare)
        else:
            pacman_only.append(bare)

    _log.newline()
    if pacman_only:
        _log.ui(
            "Repo packages with ABI drift (rebuild via pacman or set "
            "[build] repo_mode = \"profiled\" in packages.toml):"
        )
        for name in pacman_only:
            _log.ui(f"  → run: sudo pacman -S {name}")
    if install_candidates:
        _log.ui(
            "Install candidates skipped — `sysforge doctor --apply` only "
            "rebuilds (not installs) in v1.x:"
        )
        for cand in install_candidates:
            bare = cand.split("/", 1)[1] if "/" in cand else cand
            _log.ui(f"  → run: sysforge build {bare}")

    if not eligible:
        _log.ui("No eligible rebuild candidates — nothing to apply.")
        return 0

    dry_run = bool(getattr(args, "dry_run", False))
    no_confirm = bool(getattr(args, "no_confirm", False))

    _log.ui(f"\n--apply: would rebuild {len(eligible)} package(s):")
    for name in eligible:
        _log.ui(f"  • {name}")

    if dry_run:
        _log.ui("\n(--dry-run: nothing rebuilt)")
        return 0

    if not no_confirm:
        try:
            answer = input("Proceed with rebuild? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            _log.ui("Aborted.")
            return 0

    # Synthesize an args namespace for cmd_update with the eligible packages
    # as the positional pkgname filter. Carry over any state_dir / log_dir
    # the caller passed; everything else takes update's defaults.
    from types import SimpleNamespace
    from sysforge.update import cmd_update
    update_args = SimpleNamespace(
        pkgnames=eligible,
        packages=None,           # packages.toml override file path (default)
        profile_conf=None,
        state_dir=getattr(args, "state_dir", None),
        log_dir=getattr(args, "log_dir", None),
        offline=False,
        install_only=False,
        dry_run=False,
        cleansrc=False,
        devel=False,
        no_cleanbuild=False,
        no_pkg_log=False,
        persist_log=False,
        interactive=False,
        makepkg=None,
        cache_report=False,
        skip_sync_check=False,
        cc=None, cxx=None, ld=None,
    )
    try:
        cmd_update(update_args)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    return 0


# ---------------------------------------------------------------------------
# Verb wrapper
# ---------------------------------------------------------------------------

from sysforge.primitives.config import load_config as _load_config  # noqa: E402
from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class DoctorVerb(Verb):
    """Health-check installed package depends and ABI linkage.

    Read-only by default (``cmd_doctor`` only prints findings). ``--apply``
    delegates to ``cmd_update`` for actual rebuild, but that path
    synthesizes an update args namespace and invokes the existing function
    directly — no sentinel needed here, since ``UpdateVerb`` is not in
    play. The sentinel for the rebuild itself is installed by the
    delegated rebuild path's ``BuildOptions`` flow.
    """

    name = "doctor"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        # cmd_doctor reads args.config; the cli.py wrapper used to set this.
        if not hasattr(args, "config") or args.config is None:
            args.config = _load_config() or {}
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        rc = cmd_doctor(args) or 0
        return ExecResult(exit_code=rc)
