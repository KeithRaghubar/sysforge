# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from sysforge import log
from sysforge.primitives import diagnostics as diag
from sysforge.primitives import pacman
from sysforge.primitives import pkgfiles_probe
from sysforge.primitives import restart_probe
from sysforge.primitives.abi_check import (
    check_so_files,
    is_abi_check_skipped_package,
    needed_sonames,
)
from sysforge.primitives.aur_resolve import _strip_version
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.dep_analysis import (
    _default_ldconfig_fn,
    _parse_ldconfig,
    soname_available,
)
from sysforge.primitives.gfxperf_probe import check_gfxperf
from sysforge.primitives.graphics_probe import check_system_graphics
from sysforge.primitives.provides_lookup import (
    files_db_present,
    suggest_for_soname,
)
import contextlib

# [DOCTOR], matching the verb name (the runner derives the same tag from
# verb.name at dispatch) — [DOC] read like "documentation" and broke the
# every-verb-module-logs-under-its-verb-name convention.
_log = log.get_logger("DOCTOR")


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
                with path.open("rb") as f:
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
               file_root: Path,
               benign_sink: list[str] | None = None,
               ) -> tuple[list[str], list[str], list[Path], bool]:
    """
    Return (dep_issues, abi_issues, so_paths, abi_skipped) for one package.

    abi_skipped is True when the package is in abi_check's bundled-binary
    skip list — its ABI pass is suppressed, but the depends check still runs.

    ``benign_sink`` is forwarded to ``check_so_files`` to accumulate demoted
    optional-symbol cases (reduced-target libLLVM target-init absences) for the
    caller's single summary line.
    """
    if pkgname not in installed:
        return ([f"{pkgname}: not installed"], [], [], False)
    depends = pacman.get_package_depends(pkgname)
    dep_issues = _check_depends(depends, ldconfig_set, pkgname=pkgname)
    so_paths = _so_paths_for_pkg(pkgname, file_root)
    if is_abi_check_skipped_package(pkgname):
        return (dep_issues, [], so_paths, True)
    abi_issues = check_so_files(so_paths, benign_sink=benign_sink)
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
            # Reaching here means the soname was absent from both the ld.so
            # cache AND every directory ldconfig scans (the filesystem fallback
            # in dep_analysis already checked those), yet the files db names an
            # installed owner. `sudo ldconfig` cannot help — the file is not on
            # the search path for it to cache. The real causes are a stale files
            # db or a soname-version drift, so point at those instead (2.1.0-B18).
            _log.ui("      → owner installed but soname absent from the library "
                    "search path; refresh the files db (`sudo pacman -Fy`) or "
                    "rebuild the dependent package against current libraries")
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
# System-state axes (toolchain / hardware / graphics / …)
#
# Each axis is a finding-*producer* (returns ``list[diagnostics.Finding]``);
# rendering + exit-code reduction is centralised in
# ``primitives/diagnostics.py`` so doctor and (eventually) the internal callers
# share one shape, one renderer, one exit-code rule. New axes append a producer
# here and an entry in ``_SYSTEM_AXIS_ORDER`` / ``_system_axes``.
# ---------------------------------------------------------------------------

# Findings derived from the *running* kernel or the current boot (not the
# kernel/initramfs just built or installed) won't change until reboot — doctor
# re-probes live every run, but the live truth here is the booted kernel. Tag
# them so a user who applied a fix and re-ran doctor understands why the line
# persists. Annotated at the doctor boundary only: the underlying probes are
# reused by the kernel/toolchain stages, where the audited config is the
# just-built one and this caveat would be wrong.
_REBOOT_HINT = ("reflects the *running* kernel / current boot — "
                "reboot, then re-run doctor, to clear")


def _with_reboot_hint(
    findings: list[diag.Finding],
    *,
    only: Callable[[diag.Finding], bool] | None = None,
) -> list[diag.Finding]:
    """Append the reboot caveat to each finding's remediation (or just those
    matching ``only``), preserving any existing remediation text."""
    out: list[diag.Finding] = []
    for f in findings:
        if only is not None and not only(f):
            out.append(f)
            continue
        tail = f"{f.remediation} — {_REBOOT_HINT}" if f.remediation else _REBOOT_HINT
        out.append(replace(f, remediation=tail))
    return out


def _collect_toolchain_findings(config) -> list[diag.Finding]:
    """Configured-vs-installed toolchain provenance (custom LLVM requested but
    stock repo LLVM installed, or PGO profdata skew). Built on
    ``llvm_state.detect_toolchain_config_mismatch`` — provenance reporting, not
    a third toolchain health probe."""
    from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch
    return diag.adapt_many("toolchain", detect_toolchain_config_mismatch(config))


def _collect_rust_findings(config, args) -> list[diag.Finding]:
    """Rust-toolchain provenance (effective toolchain, non-stable default, and
    rust-toolchain.toml pins for named package targets). Advisory, read-only —
    see ``primitives/rust_probe.py``."""
    from sysforge.primitives import rust_probe
    packages = getattr(args, "packages", None) if args is not None else None
    return rust_probe.collect_rust_findings(config, packages=packages)


def _collect_cache_findings(config) -> list[diag.Finding]:
    """Compile-cache *readiness* before a build relies on it (2.2.0-F1) — the
    point-in-time analog of ``--cache-report``'s per-build effectiveness. Reuses
    ``cache_probe.check_cache_readiness`` (the one home for cache knowledge); no
    cache subprocess logic lives here. Read-only.

    Absence is optional, not a defect: when *neither* tool is installed it's a
    single INFO (matches the boot axis's "missing-but-optional = INFO"); a tool
    that is installed but misconfigured (unwritable dir / unset-or-zero size cap)
    is a WARN carrying its remediation. A ready tool contributes to the axis's
    clean message — no finding."""
    from sysforge.primitives import cache_probe

    rows = cache_probe.check_cache_readiness()
    if all(r["state"] == "absent" for r in rows):
        return [diag.Finding(
            "cache", diag.SEV_INFO, "cache_none",
            "no compile cache configured (ccache/sccache absent); builds won't "
            "benefit from caching",
            remediation="install ccache and/or sccache to speed up rebuilds")]

    out: list[diag.Finding] = []
    for r in rows:
        if r["state"] == "misconfigured":
            out.append(diag.Finding(
                "cache", diag.SEV_WARN, f"cache_misconfigured:{r['tool']}",
                f"{r['tool']} installed but not ready: {r['detail']}",
                remediation=r["remediation"] or ""))
    return out


def _collect_hardware_findings() -> list[diag.Finding]:
    """Device-driver coverage + the *running* kernel's boot-config gaps — the
    on-the-spot analog of the kernel stage's audit ("device X has no driver")."""
    from sysforge.primitives import device_probe, kernel_safety
    from sysforge.primitives.dep_analysis import _parse_kernel_config

    devices = device_probe.enumerate_devices()
    findings = list(device_probe.check_unsupported_devices(devices=devices))
    running_cfg = _parse_kernel_config()
    if running_cfg:
        findings += kernel_safety.audit_resolved_config(running_cfg, devices=devices)
    # The whole axis reads the running kernel's config + bound drivers.
    return _with_reboot_hint(diag.adapt_many("hardware", findings))


def _collect_graphics_findings(config) -> list[diag.Finding]:
    """System-state graphics/windowing health (kernel/module params, driver
    version skew, Wayland protocol advertisement, Steam GPU accel, …)."""
    gpu_vendors = _read_gpu_vendors(config)
    return diag.adapt_many("graphics", check_system_graphics(config, gpu_vendors=gpu_vendors))


def _collect_gfxperf_findings(config) -> list[diag.Finding]:
    """Advisory graphics runtime-degradation checklist (video-decode path, GPU
    power/clock state, CPU governor, frame pacing, thermal/memory snapshots).
    Opt-in via --gfxperf; never contributes an error (WARN/INFO only)."""
    gpu_vendors = _read_gpu_vendors(config)
    return diag.adapt_many("gfxperf", check_gfxperf(config, gpu_vendors=gpu_vendors))


def _collect_pacman_findings() -> list[diag.Finding]:
    """Local pacman-db consistency, stale lock, unmerged .pacnew/.pacsave,
    orphans, plus sysforge libalpm-hook drift. Read-only — never syncs or
    writes. See ``primitives/system_probe.py`` / ``primitives/pacman_hooks.py``."""
    from sysforge.primitives import system_probe
    findings = system_probe.collect_system_findings()
    findings += _collect_hook_findings()
    return findings


def _collect_hook_findings() -> list[diag.Finding]:
    """Report sysforge libalpm hooks that are missing from or stale against the
    shipped source — these silently disable `sysforge update`'s reminder and
    auto-demote logic. Read-only; remediation is `sysforge setup`."""
    from sysforge.primitives import pacman_hooks

    out: list[diag.Finding] = []
    for art, state in pacman_hooks.diff_status():
        if state == pacman_hooks.STATE_OK:
            continue
        verb = "missing" if state == pacman_hooks.STATE_MISSING else "stale"
        out.append(diag.Finding(
            "pacman", diag.SEV_WARN, f"hook_{state}:{art.name}",
            f"sysforge pacman hook {verb}: {art.dest}",
            remediation="run `sysforge setup` to install/refresh sysforge's pacman hooks"))
    return out


def _collect_state_findings(args) -> list[diag.Finding]:
    """sysforge's own state integrity: recorded build failures, an interrupted
    stage sentinel, build_state drift. Read-only — does not recover or save.
    See ``primitives/state_probe.py``."""
    from sysforge.primitives import state_probe
    return state_probe.collect_state_findings(
        state_dir=getattr(args, "state_dir", None))


def _collect_services_findings() -> list[diag.Finding]:
    """Live service/driver runtime health: failed systemd units, firmware a
    driver requested but could not load. See ``primitives/runtime_probe.py``."""
    from sysforge.primitives import runtime_probe
    # Failed units are live; firmware-load failures are from this boot's log.
    return _with_reboot_hint(
        runtime_probe.collect_runtime_findings(),
        only=lambda f: f.check_id == "missing_firmware")


def _collect_audio_findings() -> list[diag.Finding]:
    """Live PipeWire/WirePlumber sound-stack health: failed audio user services
    and a vanished output sink. Read-only — never restarts a unit. User-scoped,
    so it degrades to clean under sudo (no reachable session bus). See
    ``primitives/audio_probe.py``."""
    from sysforge.primitives import audio_probe
    return audio_probe.collect_audio_findings()


def _collect_network_findings() -> list[diag.Finding]:
    """Live network/connectivity configuration health: no default route,
    connection-manager ownership conflicts (>1 enabled manager), and a DNS
    provisioner conflict (resolved active but /etc/resolv.conf static). Read-only
    — never makes a network call or mutates a unit. See
    ``primitives/network_probe.py``."""
    from sysforge.primitives import network_probe
    return network_probe.collect_network_findings()


def _collect_storage_findings(config) -> list[diag.Finding]:
    """Storage / filesystem health: free space on the build dir and /etc/fstab
    integrity (entries whose device/UUID/label no longer resolve). Read-only —
    never mounts or writes. See ``primitives/storage_probe.py``."""
    from sysforge.primitives import config as cfgmod
    from sysforge.primitives import storage_probe
    doctor_cfg = cfgmod.load_sysforge_toml().get("doctor", {}) or {}
    return storage_probe.collect_storage_findings(config, doctor_cfg=doctor_cfg)


def _collect_boot_findings() -> list[diag.Finding]:
    """Running-system boot readiness — the analog of the kernel stage's gates
    1/3, reusing ``kernel_safety``: per-kernel boot artifacts (vmlinuz +
    initramfs + boot entry), a recovery fallback, /boot space, and DKMS modules
    for the running kernel. Boot-artifact gaps are brick-class."""
    from sysforge.primitives import kernel_safety

    out: list[diag.Finding] = []
    kernels = kernel_safety.find_fallback_kernels()
    if not kernels:
        out.append(diag.Finding(
            "boot", diag.SEV_WARN, "boot_no_bootable_kernel",
            "no bootable kernel (vmlinuz + initramfs) found in /boot",
            remediation="reinstall your kernel package and regenerate the initramfs"))
    elif len(kernels) == 1:
        out.append(diag.Finding(
            "boot", diag.SEV_INFO, "boot_no_fallback",
            f"only one bootable kernel ({kernels[0]}); no recovery fallback if it "
            "fails to boot",
            remediation="install a second kernel (e.g. linux-lts) as a fallback"))

    kfindings = []
    for suffix in kernels:
        kfindings += kernel_safety.verify_boot_artifacts(suffix)
    space = kernel_safety.check_boot_mount_space()
    if space is not None:
        kfindings.append(space)
    with contextlib.suppress(Exception):
        kfindings += kernel_safety.check_dkms_for_kernel(
            kernel_safety.running_kernel_release())

    out += diag.adapt_many("boot", kfindings)
    # Boot-artifact / /boot-space findings are filesystem-live (clear on the
    # next run); only the DKMS check is scoped to the running kernel.
    return _with_reboot_hint(out, only=lambda f: f.check_id.startswith("dkms:"))


def _collect_restart_findings() -> list[diag.Finding]:
    """Processes still running superseded code after an upgrade (2.4.0-F2).

    Evidence-based: a ``(deleted)`` mapping in ``/proc/<pid>/maps`` means the
    process is using a file that was replaced on disk. Advisory (never brick-
    class) — unlike the ``boot`` axis nothing here can leave the machine
    unbootable, it just means an upgrade has not taken effect yet. Read-only and
    never escalates.
    """
    report = restart_probe.scan_stale_processes()

    out: list[diag.Finding] = []
    # One finding per package, at that package's worst tier — a package mapped
    # by 290 processes is one problem, not 290. Tiered and untiered entries are
    # reduced separately: an untiered entry has no remediation to compare by
    # tier, but must still surface (spec step 4) rather than vanish, or the
    # axis can report clean while stale mappings genuinely exist.
    worst: dict[str, restart_probe.StaleEntry] = {}
    untiered: dict[str, restart_probe.StaleEntry] = {}
    for e in report.entries:
        key = e.package or e.path
        if e.tier is None:
            untiered.setdefault(key, e)
            continue
        prev = worst.get(key)
        if prev is None or restart_probe.tier_rank(e.tier) > restart_probe.tier_rank(prev.tier):
            worst[key] = e

    for key, e in sorted(worst.items()):
        subject = e.package or e.path
        if e.tier == restart_probe.TIER_REBOOT:
            if restart_probe.is_kernel_entry(e):
                remediation = "reboot to start using the installed kernel"
            else:
                remediation = "reboot to start using the replaced files"
        elif e.tier == restart_probe.TIER_RELOGIN:
            remediation = "log out and back in to restart the affected session processes"
        else:
            flag = " --user" if e.is_user_unit else ""
            remediation = f"systemctl{flag} restart {e.unit}"
        out.append(diag.Finding(
            "restart", diag.SEV_WARN, f"restart:{key}",
            f"{subject} was upgraded but running processes still use the "
            f"replaced files ({e.comm or f'pid {e.pid}'})",
            remediation=remediation))

    for key, e in sorted(untiered.items()):
        subject = e.package or e.path
        out.append(diag.Finding(
            "restart", diag.SEV_INFO, f"restart:{key}",
            f"{subject} was upgraded but running processes still use the "
            f"replaced files ({e.comm or f'pid {e.pid}'})"))

    if report.partial:
        out.append(diag.Finding(
            "restart", diag.SEV_INFO, "restart:partial_coverage",
            "some processes could not be inspected, so this list may be incomplete",
            remediation="re-run as `sudo sysforge doctor --restart` for full coverage"))
    return out


def _collect_integrity_findings(args) -> list[diag.Finding]:
    """`pacman -Qkk` package-file verification (opt-in --integrity axis).

    Read-only. Honors package targets so `doctor --integrity <pkg>` scopes the
    scan; a bare `--integrity` verifies every installed package."""
    packages = list(getattr(args, "packages", None) or [])
    return pkgfiles_probe.collect_integrity_findings(packages or None)


# Canonical order every KNOWN axis renders in (explicit flags select from here).
_SYSTEM_AXIS_ORDER: tuple[str, ...] = (
    "toolchain", "rust", "cache", "hardware", "graphics", "gfxperf", "pacman",
    "state", "boot", "restart", "storage", "services", "audio", "network",
    "integrity",
)

# Axes excluded from the default/`--all` sweep — advisory, opt-in via their flag.
_OPT_IN_AXES: frozenset[str] = frozenset({"gfxperf", "integrity", "rust"})

# CLI flag attribute → axis name. ``--graphics`` is also a package-walk trigger
# (the graphics-stack closure); both effects fire when it is set.
_AXIS_FLAGS: dict[str, str] = {
    "toolchain": "toolchain",
    "rust": "rust",
    "cache": "cache",
    "hardware": "hardware",
    "graphics": "graphics",
    "gfxperf": "gfxperf",
    "pacman": "pacman",
    "state": "state",
    "boot": "boot",
    "restart": "restart",
    "storage": "storage",
    "services": "services",
    "audio": "audio",
    "network": "network",
    "integrity": "integrity",
}


def _system_axes(config, args=None) -> dict[str, diag.Axis]:
    """Build the axis registry. Producer lookups go through module globals so
    tests can monkeypatch a ``_collect_*`` function."""
    return {
        "toolchain": diag.Axis(
            "toolchain", "toolchain checks",
            lambda: _collect_toolchain_findings(config),
            clean_msg=("toolchain config matches the installed LLVM "
                       "(or no custom LLVM toolchain is configured)")),
        "rust": diag.Axis(
            "rust", "rust toolchain provenance",
            lambda: _collect_rust_findings(config, args),
            clean_msg=("Rust toolchain identified; no non-stable default or "
                       "uninstalled pin")),
        "cache": diag.Axis(
            "cache", "compile-cache readiness",
            lambda: _collect_cache_findings(config),
            clean_msg="compile cache(s) ready (writable dir, size cap set)"),
        "hardware": diag.Axis(
            "hardware", "hardware checks",
            lambda: _collect_hardware_findings(),
            clean_msg="no unsupported devices or boot-config gaps detected"),
        "graphics": diag.Axis(
            "graphics", "system graphics checks",
            lambda: _collect_graphics_findings(config),
            clean_msg="no graphics misconfiguration detected"),
        "gfxperf": diag.Axis(
            "gfxperf", "graphics performance checks",
            lambda: _collect_gfxperf_findings(config),
            clean_msg="no graphics-performance issues detected"),
        "pacman": diag.Axis(
            "pacman", "pacman / system integrity",
            lambda: _collect_pacman_findings(),
            clean_msg="local package database consistent; no config drift or orphans"),
        "state": diag.Axis(
            "state", "sysforge state integrity",
            lambda: _collect_state_findings(args),
            clean_msg="no build failures, stale sentinel, or state drift"),
        "boot": diag.Axis(
            "boot", "boot / kernel runtime",
            lambda: _collect_boot_findings(),
            clean_msg="bootable kernel(s) with valid artifacts and a recovery fallback"),
        "restart": diag.Axis(
            "restart", "pending restarts",
            lambda: _collect_restart_findings(),
            clean_msg="no processes running superseded code; no restart pending"),
        "storage": diag.Axis(
            "storage", "storage / filesystem",
            lambda: _collect_storage_findings(config),
            clean_msg="adequate free space; all fstab entries resolve"),
        "services": diag.Axis(
            "services", "services / runtime health",
            lambda: _collect_services_findings(),
            clean_msg="no failed units or missing firmware"),
        "audio": diag.Axis(
            "audio", "audio / sound stack",
            lambda: _collect_audio_findings(),
            clean_msg="audio stack healthy (or not probeable under sudo)"),
        "network": diag.Axis(
            "network", "network / connectivity",
            lambda: _collect_network_findings(),
            clean_msg=("default route present; one connection manager; DNS "
                       "provisioning consistent")),
        "integrity": diag.Axis(
            "integrity", "package-file integrity",
            lambda: _collect_integrity_findings(args),
            clean_msg="all package-owned files match their recorded mtree"),
    }


def _resolve_axis_names(args) -> list[str]:
    """Which system axes to run, in canonical order.

    - Explicit axis flags (``--toolchain``/``--hardware``/``--graphics``/…) →
      exactly those.
    - ``--all`` or a *bare* invocation (no packages, no ``--repo``, no axis
      flags) → every axis (the comprehensive system sweep).
    - Package targets or ``--repo`` without an axis flag → no system axes
      (a focused package walk).
    """
    explicit = {name for attr, name in _AXIS_FLAGS.items()
                if getattr(args, attr, False)}
    if explicit:
        return [n for n in _SYSTEM_AXIS_ORDER if n in explicit]
    if getattr(args, "all", False):
        return [n for n in _SYSTEM_AXIS_ORDER if n not in _OPT_IN_AXES]
    if not args.packages and not getattr(args, "repo", False):
        return [n for n in _SYSTEM_AXIS_ORDER if n not in _OPT_IN_AXES]
    return []


def _run_system_axes(args, config, axis_names: list[str]) -> int:
    """Run + render the selected system axes; return the total error count."""
    if not axis_names:
        return 0
    from sysforge.ui import progress
    registry = _system_axes(config, args)
    axes = [registry[n] for n in axis_names if n in registry]
    # B11: keep the bottom-anchored phase indicator phase-accurate instead of
    # leaving the runner's generic "doctor: starting…" up for the whole sweep.
    results: dict[str, list[diag.Finding]] = {}
    for ax in axes:
        progress.phase(f"doctor: {ax.label}")
        results.update(diag.run_axes([ax]))
    errors = 0
    for ax in axes:
        errors += diag.render_axis(
            _log, ax.label, results[ax.name],
            clean_msg=ax.clean_msg, quiet=args.quiet,
        )
    return errors


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

    # Which system-state axes to run: bare invocation or --all → every axis;
    # explicit axis flags → just those; a focused package walk → none.
    axis_names = _resolve_axis_names(args)
    if not roots and not axis_names:
        _log.error(
            "nothing to check — pass PKG, --graphics, --hardware, --toolchain, "
            "--all, or --repo (bare `doctor` runs the full system sweep)"
        )
        return 2

    # Package walk (depends + ABI linkage). A bare / system-only invocation has
    # no roots — the walk below is a no-op and only the system axes render.
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
    # Accumulates optional-symbol cases demoted by check_so_files (reduced-target
    # libLLVM target-init absences) across the whole walk → one summary line.
    abi_benign: list[str] = []

    from sysforge.ui import progress
    for pkgname in targets:
        # B11: surface the in-progress package instead of a static "starting…".
        progress.phase(f"doctor: auditing {pkgname}")
        dep_issues, abi_issues, so_paths, abi_skipped = _check_one(
            pkgname, ldconfig_set, installed, file_root, benign_sink=abi_benign
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

    if targets:
        _log.newline()
        _log.ui(
            f"Scanned {len(targets)} package(s); "
            f"{len(affected_pkgs)} with issues, {total_issues} total finding(s)."
        )
        if abi_benign:
            _log.ui(
                f"ABI: {len(abi_benign)} optional LLVM target-init symbol(s) "
                "absent from reduced-target libLLVM (benign; -v to list)."
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

    # System-state axes (toolchain / hardware / graphics / …) — selected by
    # _resolve_axis_names and rendered + exit-coded through the unified
    # diagnostics renderer.
    axis_error_count = _run_system_axes(args, config, axis_names)

    # --apply bridge: hand REBUILD candidates to `sysforge update`.
    if apply_requested:
        rc = _apply_rebuilds(args, global_rebuild, global_install,
                             foreign, installed)
        # When apply runs, its return code dominates so a successful rebuild
        # produces exit 0 even if doctor found issues.
        return rc

    if affected_pkgs or axis_error_count:
        return 1
    return 0


def _apply_rebuilds(
    args, rebuild_candidates: list[str], install_candidates: list[str],
    foreign: set[str], installed: dict[str, str],
) -> int:
    """Hand REBUILD-classified candidates to ``sysforge update``.

    Filters to packages eligible for sysforge's rebuild path (foreign packages,
    or repo packages that ``sysforge update`` would walk under
    ``[build] repo_mode = "build_from_source"``). Repo candidates outside that
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
            "[build] repo_mode = \"build_from_source\" in packages.toml):"
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
        answer = prompt_choice(
            "Proceed with rebuild? [y/N] ", ["y", "yes"],
            default="", retry_on_invalid=False,
        )
        if answer not in {"y", "yes"}:
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
    delegated rebuild path's ``BuildOptions`` flow. ``--apply`` (non-dry-run)
    also opts out of this verb's own run log, since ``cmd_update`` opens its
    own on the process-global unified-log handle (see
    ``unified_log_basename``).
    """

    name = "doctor"
    requires_sentinel = False

    def unified_log_basename(self, args) -> str | None:
        # Opt into sysforge-doctor.log for a read-only run, but NOT for
        # `--apply` (non-dry-run): that path delegates to cmd_update, which
        # opens its own sysforge-update.log on the process-global unified-log
        # handle. Opening a doctor log here would be orphaned (leaked FD, no
        # rebuild output) by that reassignment, so the rebuild's record
        # correctly lives in sysforge-update.log instead (2.1.0-F4).
        if getattr(args, "apply", False) and not getattr(args, "dry_run", False):
            return None
        return "sysforge-doctor.log"

    def pre_check(self, args) -> PreCheckResult:
        # cmd_doctor reads args.config; the cli.py wrapper used to set this.
        if not hasattr(args, "config") or args.config is None:
            args.config = _load_config() or {}
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        rc = cmd_doctor(args) or 0
        return ExecResult(exit_code=rc)
