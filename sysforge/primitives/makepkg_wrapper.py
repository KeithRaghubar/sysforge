# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_wrapper.py — build orchestrator + public facade

The thin [BUILD] orchestrator that ties the build together: ``_run_build`` /
``run`` (the top-level entry), ``BuildOptions``, ``install_built_packages``, and
``_maybe_patch_llvm_targets``. The genuinely-own concerns have been split into
focused single-tag modules and are re-imported/re-exported here so the public
surface is unchanged:
    conf emission        → makepkg_conf      ([CONF])
    flag-string utils    → makepkg_flags     ([FLAG], + INSTALL_FLAGS/SYNC_FLAGS)
    env resolution       → makepkg_env       ([ENV])
    makepkg invoke+retry → makepkg_invoke    ([MAKEPKG])
    PGO profdata state   → makepkg_pgo
    built-artifact parse → makepkg_artifacts
Config loading, profile resolution, and PKGBUILD parsing are delegated to their
respective modules.

Public API (re-exported where the symbol now lives elsewhere):
    BuildOptions                       — dataclass of run() options (all fields defaulted)
    PGOBuildSkipped                    — raised when a pgo_llvm_toolchain build is skipped
    AlreadyBuilt                       — raised when PKGDEST already holds a matching .pkg.tar
    expand_makepkg_flags(flags_str)   → list
    run(pkgbuild_path, options=None)
"""
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from sysforge import log
from sysforge.primitives.aur import import_pgp_keys
from sysforge.primitives.build_throttle import resolve_throttle
from sysforge.primitives.cache_probe import (
    diff_ccache,
    diff_sccache,
    emit_build_stats,
    emit_session_report,
    emit_system_probes,
    probe_ccache,
    probe_sccache,
    record_build_result,
    report_thinlto_cache,
    reset_session,
)
from sysforge.primitives.config import (
    _active_profiles_path,
    find_pkgbuild,
    load_config,
    load_conflict_groups,
    load_consumes_inference,
    parse_system_makepkg_conf,
)
from sysforge.primitives.dep_analysis import run_dep_analysis
from sysforge.primitives.failure import handle_failure

# Flag-string manipulation lives in makepkg_flags (owns the [FLAG] tag).
# Re-exported here so emit_makepkg_conf and the CLI/update call sites
# that import `expand_makepkg_flags` from makepkg_wrapper keep working.
from sysforge.primitives.makepkg_artifacts import (
    _find_built_packages,
    _parse_built_pkg_filename,
)
from sysforge.primitives.makepkg_conf import emit_makepkg_conf
from sysforge.primitives.makepkg_env import resolve_env_vars
from sysforge.primitives.makepkg_flags import (
    INSTALL_FLAGS,
    expand_makepkg_flags,  # noqa: F401  (re-export)
    resolve_effective_linker,
)
from sysforge.primitives.makepkg_invoke import (
    AlreadyBuilt,
    RecoveryOutcome,  # noqa: F401  (re-export; tests build outcomes via mw.RecoveryOutcome)
    ToolchainMismatchError,
    _build_failed_error,
    _invoke_with_retry,
    take_last_recovery,
)
from sysforge.primitives.makepkg_pgo import (
    PGOBuildSkipped,
    _resolve_pgo_state,
    _try_load_toml,
    resolve_pgo_store,
)
from sysforge.primitives.paths import SYSFORGE_TOML_PATH, TOOLCHAIN_PATH
from sysforge.primitives.pkgbuild_meta import (
    hardcoded_build_linker,
    has_hardcoded_gcc,
    is_musl_static_build,
    parse_pkgbuild,
)
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives.pkgbuild_patcher import (
    apply_patch_pkgbuild,
    cleanup_patch_artifacts,
    extract_pkgbuild_profile,
    is_llvm_pkgbase,
    patch_build_linker,
    patch_hotplug_fragment_merge,
    patch_kconfig_targets,
    patch_kernel_btf_guard,
    patch_kernel_config_install,
    patch_kernel_kconfig_apply,
    patch_kernel_subpackages,
    patch_llvm_dir,
    patch_llvm_targets,
    patch_mesa_drivers,
    patch_noninteractive_kconfig,
    patch_package_suffix,
    patch_pkgbase_rename,
    patch_pkgbuild_groups,
    patch_subshell_env_reset,
    validate_patched_meson_pkgbuild,
    validate_patched_pkgbuild,
    warn_artifacts_left,
    write_extracted_profile,
)
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.source_sync import (
    STATUS_DIVERGED,
    STATUS_FAILED,
    STATUS_PURGE_REFUSED,
    STATUS_RATE_LIMITED,
    SyncRequest,
    get_scheduler,
)
from sysforge.profile_writer import write_package_compiler_override

# ABI / CACHE / PATCH tags are emitted by their owning modules (abi_check.py,
# cache_probe.py, pkgbuild_patcher.py); CONF lives in makepkg_conf.py — this
# orchestrator delegates to them.
_build_log = log.get_logger("BUILD")
from sysforge.primitives.profile import (
    CONF_KEY_MAP,
    build_mode_uses_extracted_profile,
    get_build_mode,
    is_mesa_pkgbase,
    is_optimized_build_mode,
    match_rules,
    normalize_build_mode,
    rename_mode_for_build_mode,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
    serialize_flags,
    variant_env_overlay,
)

# ---------------------------------------------------------------------------
# Built-package install
#
# Conf emission / flag utils / env resolution / makepkg invocation now live in
# makepkg_conf / makepkg_flags / makepkg_env / makepkg_invoke respectively.
# ---------------------------------------------------------------------------


class _ConfKwargs(TypedDict):
    """Keyword shape re-used across the initial ``emit_makepkg_conf`` call and
    the ``_reemit_conf`` retry closure in ``_run_build`` — mirrors that
    function's keyword parameters field-for-field so the ``**_conf_kwargs``
    expansion keeps each value's real type instead of widening to
    ``bool | str | int | None``."""

    kernel_build: bool
    compiler_flags_extra: str | None
    linker_flags_extra: str | None
    strip_full_lto: bool
    pkgbuild_has_hardcoded_gcc: bool
    reactive_gcc_fallback: bool
    is_lib32: bool
    is_musl_static: bool
    pkgbuild_options: list | None
    toolchain_variant: str | None
    jobs: int | None


def _persist_recovery_overrides(pkgbase) -> None:
    """If the interactive recovery menu recorded a successful compiler swap,
    persist it to profiles.toml [package_compiler_overrides]. Best-effort:
    a write failure is logged and swallowed — never fail a good build."""
    outcome = take_last_recovery()
    if outcome is None or not outcome.overrides or not pkgbase:
        return
    ov = outcome.overrides
    cc, cxx, ld = ov.get("cc"), ov.get("cxx"), ov.get("ld")
    if cc is None or cxx is None or ld is None:
        _build_log.warn(
            "Recovery override incomplete (missing cc/cxx/ld) — "
            "skipping profiles.toml persist"
        )
        return
    # Best-effort end to end: path resolution and the write must never raise
    # out of here (CLAUDE.md: persistence never fails a good build).
    try:
        path = _active_profiles_path()
        write_package_compiler_override(path, pkgbase, cc, cxx, ld)
    except Exception as e:
        _build_log.warn(f"Could not persist recovery override to profiles.toml: {e}")


def _find_artifacts(pkgbuild_dir) -> list:
    """Locate built ``.pkg.tar*`` artifacts honouring makepkg's PKGDEST.

    makepkg writes artifacts to its configured ``PKGDEST`` (env or layered
    makepkg.conf), falling back to the PKGBUILD directory only when PKGDEST is
    unset. Any code that *locates* a built artifact must go through
    ``pacman.get_pkgdest()`` (CLAUDE.md: makepkg path resolution has one home) —
    globbing only the PKGBUILD dir silently finds nothing when PKGDEST is set.
    Searches the union of {PKGDEST, pkgbuild_dir} (deduped) so artifacts are
    found wherever they landed.
    """
    from sysforge.primitives.pacman import get_pkgdest

    roots, seen = [], set()
    for root in (get_pkgdest(), Path(pkgbuild_dir).resolve()):
        if root is None:
            continue
        rp = Path(root).resolve()
        if str(rp) in seen:
            continue
        seen.add(str(rp))
        roots.append(rp)
    found, names = [], set()
    for root in roots:
        for p in _find_built_packages(root):
            if p.name in names:
                continue
            names.add(p.name)
            found.append(p)
    return found


# B9: sidecar recording the exact package basenames a build emitted, written
# at build time from ``makepkg --packagelist`` while the patched
# ``PKGBUILD.sysforge`` is still present. The install step (which runs after
# that patched file is cleaned up, leaving only the un-renamed upstream
# PKGBUILD) reads it to match this build's artifacts exactly, rather than
# prefix-globbing a shared PKGDEST — pkgname ``linux`` otherwise sweeps in
# ``linux-custom``, stale ``linux-sysforge-<oldver>``, etc.
_BUILT_MANIFEST_NAME = ".sysforge-built.list"


def _read_built_manifest(pkgbuild_dir) -> set[str]:
    """Return the recorded emitted basenames for this build, or empty set."""
    manifest = Path(pkgbuild_dir) / _BUILT_MANIFEST_NAME
    if not manifest.is_file():
        return set()
    try:
        return {ln.strip() for ln in manifest.read_text().splitlines() if ln.strip()}
    except OSError:
        return set()


def _artifacts_for_pkgbuild(pkgbuild_dir) -> list:
    """Return only the artifacts in PKGDEST that belong to ``pkgbuild_dir``.

    ``_find_artifacts`` globs the *whole* PKGDEST (shared across every build),
    so on a populated PKGDEST it returns far more than the current build's
    output. Two-tier scoping:

    1. When a build-time manifest (``_BUILT_MANIFEST_NAME``, from
       ``makepkg --packagelist``) exists, filter to exactly those basenames —
       the authoritative set of what *this* build emitted (B9). This is the
       only reliable answer for a renamed kernel, whose patched PKGBUILD is
       gone by install time.
    2. Otherwise fall back to scoping by the PKGBUILD's own pkgnames via
       ``_parse_built_pkg_filename``; and to the unfiltered union only when the
       PKGBUILD can't be parsed, so a parse limitation degrades to the old
       behaviour rather than installing nothing.
    """
    found = _find_artifacts(pkgbuild_dir)
    wanted = _read_built_manifest(pkgbuild_dir)
    if wanted:
        exact = [p for p in found if p.name in wanted]
        # Only trust the manifest when at least one listed artifact is actually
        # present; an empty intersection (stale/removed manifest) degrades to
        # pkgname scoping below rather than installing nothing.
        if exact:
            return exact
    pkgbuild_file = Path(pkgbuild_dir) / "PKGBUILD"
    if not pkgbuild_file.is_file():
        return found
    try:
        parsed = parse_pkgbuild(pkgbuild_file)
    except Exception:
        return found
    globals_ = parsed.get("globals", {})
    pkgnames = globals_.get("pkgname", [])
    if isinstance(pkgnames, str):
        pkgnames = [pkgnames]
    if not pkgnames:
        return found
    scoped = [
        p for p in found
        if any(_parse_built_pkg_filename(name, p.name) is not None
               for name in pkgnames)
    ]
    # Return the scoped set even when empty — do NOT degrade to the full PKGDEST
    # union. When the PKGBUILD parses cleanly and advertises pkgnames but none of
    # them match any artifact, this build's output simply isn't here under those
    # names — the common cause being a rename (kernel ``linux`` → ``linux-sysforge``
    # via patch_pkgbase_rename), whose renamed artifacts the un-patched on-disk
    # PKGBUILD can't name. That case is handled by the build-time manifest (tier 1
    # above); if the manifest is somehow absent, the caller must fail loudly
    # ("nothing to install") rather than hand a shared PKGDEST of hundreds of
    # unrelated packages — old kernels, downgrades, conflicting -git builds — to
    # ``pacman -U`` and risk bricking the system.
    return scoped


def _capture_built_manifest(patched_pkgbuild_path) -> None:
    """Record the exact package basenames this build emits into the sidecar.

    Runs ``makepkg --packagelist`` against the *patched* PKGBUILD (rename +
    dropped subpackages applied), so the recorded set matches what actually
    lands in PKGDEST. Called on build success, before the patched PKGBUILD is
    cleaned up. Best-effort: any failure leaves no sidecar and the install step
    falls back to pkgname scoping. ``--packagelist`` only sources the PKGBUILD
    header (no prepare()/build()), so it is cheap and safe for a kernel.
    """
    patched = Path(patched_pkgbuild_path)
    try:
        r = subprocess.run(
            ["makepkg", "-p", patched.name, "--packagelist"],
            cwd=str(patched.parent), capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return
    if r.returncode != 0:
        return
    names = [Path(ln.strip()).name for ln in r.stdout.splitlines() if ln.strip()]
    if not names:
        return
    with suppress(OSError):
        (patched.parent / _BUILT_MANIFEST_NAME).write_text(
            "\n".join(names) + "\n", encoding="utf-8")


def install_built_packages(pkgbuild_dir, *, noconfirm: bool = True) -> list:
    """Install the .pkg.tar* artifacts for ``pkgbuild_dir`` via ``pacman -U``.

    For callers that split build from install — the kernel stage builds with
    ``BuildOptions.no_install`` so its safety audit can run against the
    resolved .config, then calls this to install only once the audit passes.
    Locates artifacts via ``_artifacts_for_pkgbuild`` (PKGDEST-aware **and**
    scoped to this PKGBUILD's pkgnames, so a shared/populated PKGDEST doesn't
    drag every previously-built package into the ``pacman -U``). Inherits stdio
    so a pacman conflict/sudo prompt is visible. Raises RuntimeError when no
    artifact is found or the install fails.
    """
    pkgs = _artifacts_for_pkgbuild(pkgbuild_dir)
    if not pkgs:
        raise RuntimeError(
            f"no built package found in {pkgbuild_dir} — nothing to install")
    cmd = privileged_argv(["pacman", "-U"])
    if noconfirm:
        cmd.append("--noconfirm")
    cmd += [str(p) for p in pkgs]
    _build_log.ui(
        f"Installing built package(s): {', '.join(p.name for p in pkgs)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"pacman -U failed (exit {result.returncode})")
    # B9: the build-time manifest has served its purpose — drop it so a later
    # unrelated build in the same dir doesn't read a stale artifact list.
    manifest = Path(pkgbuild_dir) / _BUILT_MANIFEST_NAME
    if manifest.is_file():
        with suppress(OSError):
            manifest.unlink()
    return pkgs


def _pkgname_from_meta(pkgmeta: dict | None) -> str:
    """Extract a display name from parsed PKGBUILD metadata."""
    if pkgmeta is None:
        return "unknown"
    globals_ = pkgmeta.get("globals", {})
    name = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(name, list):
        name = name[0] if name else "unknown"
    return name or "unknown"


def _maybe_patch_llvm_targets(
    pkgbuild_path, pkgmeta, state_dir_override: Path | None = None
) -> bool:
    """Inject ``-DLLVM_TARGETS_TO_BUILD=...`` for LLVM-toolchain PKGBUILDs.

    Returns ``True`` when the PKGBUILD was modified (an injection occurred),
    ``False`` otherwise — the caller uses this to gate the post-patch validation.

    Applies regardless of build_mode — covers ``update --patch-pkgbuild``,
    kernel mode, and ``run toolchain`` PGO/plain builds uniformly. Resolution
    order is:
      1. toolchain.toml ``[llvm] targets`` override (incl. force-all via ``[]``)
      2. ``hardware_profile.toml`` at the resolved state dir
      3. Live hardware detection (uname -m + lspci)

    Falling back to live detection means ``sysforge run toolchain`` works
    even when the hardware stage hasn't written a profile at the current
    state dir — which is the common case (toolchain stage runs standalone)
    and the same scenario that defeated rounds 1 & 2.

    ``state_dir_override`` is still honoured for callers that want to pin
    the patcher to a specific state dir (CLI ``--state-dir``); it just
    isn't load-bearing anymore.
    """
    pkgname = _pkgname_from_meta(pkgmeta)
    if not is_llvm_pkgbase(pkgname):
        return False
    # lib32-* LLVM packages must NOT have their target set reduced. They ship no
    # headers of their own and compile against the all-target 64-bit
    # /usr/include/llvm headers, so a reduced LLVM_TARGETS_TO_BUILD leaves
    # lib32-llvm without the target-init symbols lib32-clang's offload tools
    # (clang-nvlink-wrapper / clang-sycl-linker) reference from those headers — a
    # hard link failure. Always build all targets for lib32; let the upstream
    # PKGBUILD decide (stock lib32-llvm builds the full set).
    if pkgname.startswith("lib32-"):
        return False
    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives.llvm_targets import resolve_or_detect_llvm_targets
    state_dir, _ = resolve_state_dir(state_dir_override)
    hw_profile = state_dir / "hardware_profile.toml"
    targets = resolve_or_detect_llvm_targets(TOOLCHAIN_PATH, hw_profile)
    if not targets:
        # Either user explicitly disabled filtering ([llvm] targets = []),
        # or live detection couldn't classify the host (unsupported arch).
        # Either way, no patch — let the upstream PKGBUILD decide.
        return False
    used_live = not hw_profile.is_file()
    if used_live:
        _build_log.info(
            f"LLVM target filter: no hardware_profile.toml at {hw_profile} — "
            f"using live detection result {targets}",
        )
    return patch_llvm_targets(pkgbuild_path, targets)


def _maybe_patch_mesa_drivers(
    pkgbuild_path, pkgmeta, state_dir_override: Path | None = None
):
    """Trim mesa's ``gallium-drivers`` / ``vulkan-drivers`` meson options to the
    drivers this host runs, the mesa analogue of :func:`_maybe_patch_llvm_targets`.

    Returns the resolved ``{"gallium": [...], "vulkan": [...]}`` dict when a
    rewrite occurred (so the caller can validate against it), else ``None``.

    Opt-in: ``resolve_or_detect_mesa_drivers`` returns ``None`` unless
    ``[mesa] filter_drivers = true`` in sysforge.toml, so with the switch off
    this is a no-op for every build. Unlike the LLVM path, lib32-mesa IS
    filtered — mesa drivers are vendor- not arch-determined, and lib32 has no
    header-symbol coupling to a reduced set.
    """
    pkgname = _pkgname_from_meta(pkgmeta)
    if not is_mesa_pkgbase(pkgname):
        return None
    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives.mesa_drivers import resolve_or_detect_mesa_drivers
    state_dir, _ = resolve_state_dir(state_dir_override)
    hw_profile = state_dir / "hardware_profile.toml"
    drivers = resolve_or_detect_mesa_drivers(SYSFORGE_TOML_PATH, hw_profile)
    if not drivers:
        # Switch off, or nothing resolved — let the upstream PKGBUILD decide.
        return None
    if patch_mesa_drivers(pkgbuild_path, drivers["gallium"], drivers["vulkan"]):
        return drivers
    return None


def _maybe_patch_build_linker(pkgbuild_path, pkgmeta, resolved_profile, ld_override):
    """Reconcile a PKGBUILD ``build()`` that hardcodes ``-fuse-ld=X`` against
    sysforge's effective linker.

    The conf layer injects ``--ld`` / profile LDFLAGS into the makepkg.conf, but
    a ``RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"`` inside ``build()`` re-appends
    its own linker at runtime, *after* the conf is sourced — and "last
    ``-fuse-ld=`` wins" at link time. This is the only layer that can reach it.

    Gate: skip unless the PKGBUILD hardcodes a linker; then rewrite only when
    the hardcoded linker differs from the effective linker (one shared
    definition, :func:`resolve_effective_linker`). Equal -> silent no-op.

    Returns the :func:`patch_build_linker` result dict on a rewrite (so the
    caller validates against the original), else ``None``.
    """
    hardcoded = hardcoded_build_linker(pkgmeta)
    if not hardcoded:
        return None

    system_ldflags = ""
    try:
        _sys = parse_system_makepkg_conf()
        _raw = (_sys.get("LDFLAGS") or "").strip()
        system_ldflags = _raw[1:-1] if (len(_raw) >= 2 and _raw[0] == _raw[-1] == '"') else _raw
    except Exception:
        system_ldflags = ""

    effective = resolve_effective_linker(
        ld_override=ld_override,
        profile_ldflags=resolved_profile.get("LDFLAGS"),
        system_ldflags=system_ldflags,
    )
    if hardcoded == effective:
        return None

    _build_log.info(
        f"PKGBUILD build() hardcodes -fuse-ld={hardcoded}, but effective linker "
        f"is {effective} — rewriting to honor the effective linker"
    )
    return patch_build_linker(pkgbuild_path, effective)


def _run_build(pkgbuild_path, resolved_profile, config, groups,
               active_consumes=None, extracted_profile=None, pkgmeta=None,
               extra_flags=None, interactive=False,
               cc_override=None, cxx_override=None, ld_override=None,
               kernel_build: bool = False,
               kernel_build_headers: bool = True,
               kernel_build_docs: bool = True,
               kconfig_targets: list[str] | None = None,
               compiler_flags_extra: str | None = None,
               linker_flags_extra: str | None = None,
               strip_full_lto: bool = False,
               injected_env: dict | None = None,
               strip_flags=None,
               pkgbuild_has_hardcoded_gcc: bool = False,
               state_dir: Path | None = None,
               toolchain_variant: str | None = None,
               cmake_llvm_dir: str | None = None,
               optimization_build_mode: str | None = None,
               rename_pkgbase_to: str | None = None):
    """
    Emit makepkg.conf and invoke makepkg, handling build failures.

    If extracted_profile is provided (patch_pkgbuild or kernel mode), applies
    the patched PKGBUILD instead of the original. Cleans up patch artifacts on
    success; leaves them in place on failure for diagnosis.

    kernel_build: enables kernel-specific behaviour —
      - Interactive kconfig targets are replaced with olddefconfig unless
        interactive=True, in which case the PKGBUILD runs as-is.
      - Compiler flag keys (CFLAGS/CXXFLAGS/LDFLAGS/etc.) are stripped from
        the emitted makepkg.conf; system conf values pass through verbatim.
      - If the effective CC resolves to clang, LLVM=1 and LLVM_IAS=1 are
        injected into the build environment.
    """
    # Keep the original (upstream) PKGBUILD path so the post-patch validation
    # can prove the dependency arrays survived patching unchanged.
    original_pkgbuild_path = pkgbuild_path
    if extracted_profile is not None:
        # patch_pkgbuild / kernel mode: use patched copy with flags stripped
        pkgbuild_path = apply_patch_pkgbuild(pkgbuild_path, pkgmeta or {"globals": {}})
    else:
        pkgbuild_path = patch_pkgbuild_groups(pkgbuild_path, groups)

    cmake_injected = _maybe_patch_llvm_targets(
        pkgbuild_path, pkgmeta, state_dir_override=state_dir
    )

    # Force find_package(LLVM CONFIG) at the staged libLLVM prefix for the
    # toolchain stage's staged PGO passes (1b/3b/3c). CMAKE_PREFIX_PATH alone
    # (set in the build env) is silently losing to /usr, so clang/lld link the
    # live libLLVM instead of the one that ships → Gate-3 _ZNSt*@LLVM_* brick.
    # -DLLVM_DIR is the highest-precedence config-mode override.
    if cmake_llvm_dir:
        cmake_injected = patch_llvm_dir(pkgbuild_path, cmake_llvm_dir) or cmake_injected

    # Mesa (meson, not cmake): trim gallium/vulkan drivers to this host. Opt-in —
    # a no-op unless [mesa] filter_drivers = true. Returns the resolved driver
    # dict when a rewrite happened, so the meson validation can check against it.
    mesa_filtered = _maybe_patch_mesa_drivers(
        pkgbuild_path, pkgmeta, state_dir_override=state_dir
    )

    if kernel_build:
        # Always inject the sysforge.config fragment merge so a stock PKGBUILD
        # actually applies the hardware/device kconfig (it adds `make nconfig`
        # itself only when interactive). The non-interactive patch then rewrites
        # any *existing* interactive kconfig target to olddefconfig. When a
        # configured kconfig_targets sequence is set it is the sole authority
        # for kconfig generation *and* UI review (resolve_kconfig_targets put
        # any UI target last), so the interactive nconfig-review injection is
        # suppressed — the seed/merge blocks are still injected, sentinel-
        # tagged so patch_kconfig_targets leaves their resolve lines intact.
        patch_kernel_kconfig_apply(
            pkgbuild_path, interactive=interactive and not kconfig_targets)
        # Ship the resolved .config to /boot (pacman-tracked) when the PKGBUILD
        # doesn't already — the main image subpackage is named for the pkgbase.
        patch_kernel_config_install(pkgbuild_path, pkgname=_pkgname_from_meta(pkgmeta))
        # Gate the bpftool vmlinux.h build+install on CONFIG_DEBUG_INFO_BTF so a
        # BTF-off resolved .config (e.g. base_config="running" on a lean kernel)
        # doesn't hard-fail at the bpftool step. Guard is config-conditional at
        # build time, so it's safe to apply unconditionally.
        patch_kernel_btf_guard(pkgbuild_path)
        # Drop the -headers/-docs subpackages from pkgname when disabled, so the
        # build never packages them (helper bodies stay defined but unreferenced).
        patch_kernel_subpackages(
            pkgbuild_path, headers=kernel_build_headers, docs=kernel_build_docs
        )
        # Configured kconfig_targets sequence (kernel.toml, already resolved/
        # validated by the kernel stage) replaces the PKGBUILD's own kconfig
        # generation step. Applied after the other kernel patchers so it sees
        # their output — it skips the sentinel-tagged resolve lines the
        # fragment-merge injection added and lands the configured block after
        # the seed/merge guard blocks — and before patch_noninteractive_kconfig
        # so a UI tail left in the configured sequence still gets stripped when
        # the run is non-interactive (kernel.toml can't opt a UI target out of
        # that).
        if kconfig_targets:
            patch_kconfig_targets(pkgbuild_path, kconfig_targets)
        # F2: re-enable hotplug driver classes as modules AFTER the minimizer.
        # Injected after patch_kconfig_targets (so it can anchor before the UI
        # tail) and before patch_noninteractive_kconfig (which then strips any UI
        # tail on a non-interactive run — the hotplug merge stays put). Always
        # called: the block is file-guarded, and the stage decides whether
        # sysforge.hotplug.config exists.
        patch_hotplug_fragment_merge(pkgbuild_path)
        if not interactive:
            patch_noninteractive_kconfig(pkgbuild_path)

    # Reset toolchain env vars in subshell functions so sub-builds (musl
    # bootstrap, embedded grub, etc.) use the system default compiler/linker
    # instead of inheriting the sysforge profile CC/CXX or shell LD.
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    toolchain_env = {k: v for k, v in resolved_profile.items() if k in toolchain_keys}
    inherited_env = {k: v for k, v in os.environ.items() if k in ("CC", "CXX", "LD")}
    patch_subshell_env_reset(pkgbuild_path, toolchain_env, inherited_env=inherited_env)

    # Fail fast on a corrupt cmake-arg injection (a `-D…` spliced into a dep
    # array, or orphaned as its own command) — both have only ever surfaced hours
    # into a build. Scoped to the injection path: the dependency-array invariant
    # strictly holds there. Raises PkgbuildPatchError, which aborts the build.
    if cmake_injected:
        validate_patched_pkgbuild(original_pkgbuild_path, pkgbuild_path)

    # Same fail-fast for a mesa meson-driver rewrite: G1 globals unchanged + the
    # rewritten gallium/vulkan options kept their mandatory software baseline.
    if mesa_filtered:
        validate_patched_meson_pkgbuild(
            original_pkgbuild_path,
            pkgbuild_path,
            mesa_filtered["gallium"],
            mesa_filtered["vulkan"],
        )

    # Reconcile a build()-hardcoded -fuse-ld= linker against the effective
    # linker. The conf layer cannot reach a RUSTFLAGS+= inside build(); this
    # patch layer rewrites the token so --ld / the profile linker actually wins.
    linker_rewritten = _maybe_patch_build_linker(
        pkgbuild_path, pkgmeta, resolved_profile, ld_override
    )
    if linker_rewritten:
        validate_patched_pkgbuild(original_pkgbuild_path, pkgbuild_path)

    # -sysforge optimization-provenance rename. Gated on the Phase-1 predicate
    # is_optimized_build_mode (one home for "does this build earn the suffix?"):
    # only an optimization variant the *build verb* drove (e.g. mesa `--pgo=use`,
    # build_mode pgo_mesa) reaches here with optimization_build_mode set — the
    # toolchain stage's own PGO/BOLT naming is stage-managed and never threads it.
    # patch_package_suffix injects provides/conflicts/replaces (conflict mode) so
    # the renamed build is a validated drop-in; the rename dict rides back to run()
    # for build_state (renamed names + origin_pkgbase). Applied last so it sees the
    # fully-patched PKGBUILD and validate_patched_pkgbuild can prove every non-rename
    # global survived.
    # Kernel local-rename (F40): patch the cloned upstream's pkgbase to the
    # configured local pkgname (linux-zen → linux-mine) so the build coexists
    # with the official package. Applied BEFORE the optional -sysforge suffix so
    # the layers stack orthogonally (linux-zen → linux-mine → linux-mine-sysforge).
    local_rename = None
    if rename_pkgbase_to:
        local_rename = patch_pkgbase_rename(
            pkgbuild_path, rename_pkgbase_to, mode="coexist"
        )
        if local_rename:
            validate_patched_pkgbuild(
                original_pkgbuild_path, pkgbuild_path, rename=local_rename
            )

    rename = None
    if optimization_build_mode and is_optimized_build_mode(optimization_build_mode):
        # Conflict vs coexist is policy-per-build_mode (one home:
        # rename_mode_for_build_mode) — kernel FDO coexists with the stock kernel
        # for bootloader fallback; llvm/mesa replace their stock package.
        rename_mode = rename_mode_for_build_mode(optimization_build_mode)
        rename = patch_package_suffix(pkgbuild_path, "sysforge", mode=rename_mode)
        if rename:
            validate_patched_pkgbuild(
                original_pkgbuild_path, pkgbuild_path, rename=rename
            )

    # Probe ThinLTO cache dir (informational, once per build) — owned by cache_probe.py
    report_thinlto_cache(resolved_profile.get("LDFLAGS", ""))

    # Snapshot cache state before build for per-build delta
    before_cc = probe_ccache()
    before_sc = probe_sccache()

    success = False
    try:
        extra_env = resolve_env_vars(resolved_profile, active_consumes)
        # CLI toolchain overrides must also land in the subprocess env directly.
        # emit_makepkg_conf writes them to the conf file, but that only affects
        # shells that export CC/CXX. For bare profile (no CC in inherited env),
        # the conf assignment creates an unexported variable children cannot see.
        if cc_override is not None:
            extra_env["CC"] = cc_override
        if cxx_override is not None:
            extra_env["CXX"] = cxx_override
        if injected_env:
            extra_env.update(injected_env)

        # Variant-driven per-package env overlay (e.g. MESA_WHICH_LLVM=4 for
        # mesa-family pkgbases under stock_llvm/pgo_llvm). The overlay is
        # applied AFTER injected_env so a stage explicitly setting these
        # keys still wins — sysforge defaults, not overrides.
        _overlay_pkgbase = (pkgmeta or {}).get("globals", {}).get("pkgbase")
        if not _overlay_pkgbase:
            _pkgnames_seq = (pkgmeta or {}).get("pkgnames") or []
            _overlay_pkgbase = _pkgnames_seq[0] if _pkgnames_seq else None
        if _overlay_pkgbase:
            _overlay = variant_env_overlay(_overlay_pkgbase, toolchain_variant)
            for k, v in _overlay.items():
                if k not in extra_env:
                    extra_env[k] = v
                    _build_log.info(
                        f"[VARIANT_ENV] {k}={v} "
                        f"({_overlay_pkgbase}, variant={toolchain_variant})"
                    )

        if kernel_build:
            effective_cc = (
                cc_override
                or resolved_profile.get("CC")
                or os.environ.get("CC", "")
            )
            compiler_name = Path(effective_cc).name if effective_cc else ""
            if compiler_name.startswith("clang"):
                extra_env.update({"LLVM": "1", "LLVM_IAS": "1"})
                _build_log.info(f"Detected clang ({effective_cc!r}): injecting LLVM=1 LLVM_IAS=1")
            else:
                _build_log.info(f"Non-clang toolchain ({effective_cc!r} → 'gcc'): GCC kernel build")

        # ── Pre-flight: .SRCINFO drift regeneration ─────────────────────
        from sysforge.primitives import auto_repair as _ar
        _failure_cfg = (config or {}).get("failure_handling", {})
        _srcinfo_behaviour = _failure_cfg.get(
            "srcinfo_drift", "auto_repair_with_warning"
        )
        try:
            _ar.preflight_srcinfo(pkgbuild_path.parent, _srcinfo_behaviour)
        except RuntimeError as _e:
            raise _build_failed_error(_e) from _e

        # Outer loop: on ToolchainMismatchError, regenerate the makepkg.conf
        # once with reactive_gcc_fallback=True (forcing the GCC+LTO guard on)
        # and retry the build. A second mismatch falls through to normal
        # failure handling. On other failures, walk the auto-repair registry;
        # each scenario fires at most once so a misdetection cannot loop.
        _reactive_retry_used = False
        _repaired_scenarios: set[str] = set()
        _batch_mode = bool(resolved_profile.get("batch", False))
        # lib32-* packages: the directory name is the reliable signal even when
        # ``pkgname`` is interpolated. Matches the lib32 detection used for the
        # GCC flag guard in _run_build.
        is_lib32 = Path(pkgbuild_path).parent.name.startswith("lib32-")
        # Static-musl bootstraps (pacman-static): the profile's lld linker +
        # -static + musl crashes at runtime, so force bfd / scrub PGO at emit.
        is_musl_static = is_musl_static_build(pkgmeta)
        while True:
            try:
                # TypedDict keeps each keyword's real type (bool/str/int/None
                # per-field) through the **_conf_kwargs expansion below —
                # a plain dict() call would widen every value to the union
                # of all of them (bool | str | int | None), which pyright
                # then rejects against emit_makepkg_conf's narrow params.
                _conf_kwargs: _ConfKwargs = {
                    "kernel_build": kernel_build,
                    "compiler_flags_extra": compiler_flags_extra,
                    "linker_flags_extra": linker_flags_extra,
                    "strip_full_lto": strip_full_lto,
                    "pkgbuild_has_hardcoded_gcc": pkgbuild_has_hardcoded_gcc,
                    "reactive_gcc_fallback": _reactive_retry_used,
                    "is_lib32": is_lib32,
                    "is_musl_static": is_musl_static,
                    "pkgbuild_options": (pkgmeta or {}).get("globals", {}).get("options"),
                    "toolchain_variant": toolchain_variant,
                    "jobs": resolve_throttle(resolved_profile, config).jobs,
                }

                @contextmanager
                def _reemit_conf(cc, cxx, ld, _kw: _ConfKwargs = _conf_kwargs):
                    # ld is folded into LDFLAGS by emit via ld_override.
                    with emit_makepkg_conf(
                            resolved_profile, active_consumes,
                            cc_override=cc or None,
                            cxx_override=cxx or None,
                            ld_override=ld or None,
                            **_kw) as cpath:
                        yield cpath

                with emit_makepkg_conf(
                        resolved_profile, active_consumes,
                        cc_override=cc_override,
                        cxx_override=cxx_override,
                        ld_override=ld_override,
                        **_conf_kwargs) as conf_path:
                    _invoke_with_retry(
                        pkgbuild_path, conf_path, resolved_profile,
                        extra_env, extra_flags, interactive, strip_flags,
                        reemit_conf=_reemit_conf,
                        pkgbase=_pkgname_from_meta(pkgmeta))
                # Key the persist by the same pkgbase-or-pkgname value the
                # recovery menu labels with (line above) and that
                # resolve_profile reads the override back with — a raw
                # globals["pkgbase"] is absent for non-split PKGBUILDs, so the
                # write would silently no-op on the `not pkgbase` guard and the
                # swap would never self-heal the next build (2.1.0-B2).
                _persist_recovery_overrides(_pkgname_from_meta(pkgmeta))
                break
            except ToolchainMismatchError as e:
                if _reactive_retry_used:
                    _build_log.error(
                        "Toolchain mismatch persists after auto-retry — "
                        "aborting (check the PKGBUILD and profile flags)"
                    )
                    raise _build_failed_error(e) from e
                _build_log.warn(
                    "Auto-retrying build with GCC-compatible flags "
                    "(rewriting clang-only flags like -flto=thin)"
                )
                _reactive_retry_used = True
            except subprocess.CalledProcessError as e:
                # Auto-repair: walk REGISTRY against captured stdout. Each
                # scenario is at-most-once-per-build (tracked in
                # _repaired_scenarios) so a misdetection can't loop.
                _captured = getattr(e, "captured_output", None) or []
                _accum = _ar.BuildOutputAccumulator(
                    lines=list(_captured),
                    srcdir=pkgbuild_path.parent / "src",
                )
                _result = _ar.apply_first_match(
                    _ar.REGISTRY,
                    _accum,
                    pkgbuild_dir=pkgbuild_path.parent,
                    behaviour_for=lambda key: _failure_cfg.get(key, "auto_repair"),
                    batch=_batch_mode,
                    already_repaired=_repaired_scenarios,
                )
                if _result is None or _result.aborted or not _result.repaired:
                    raise
                # Retry semantics: incremental keeps srcdir, from_scratch lets
                # makepkg redo prepare()/extract by NOT passing -e (handled by
                # the existing strip_flags / extra_flags). For now, retry the
                # build with the same flags — the repair has fixed the on-disk
                # state, and the next iteration of the outer loop will rerun
                # invoke_makepkg.
                continue

        # Post-build cache delta
        pkgname = _pkgname_from_meta(pkgmeta)
        after_cc = probe_ccache()
        after_sc = probe_sccache()
        cc_delta = (
            diff_ccache(before_cc, after_cc)
            if before_cc is not None and after_cc is not None
            else None
        )
        sc_delta = (
            diff_sccache(before_sc, after_sc)
            if before_sc is not None and after_sc is not None
            else None
        )
        emit_build_stats(pkgname, cc_delta, sc_delta)
        record_build_result(pkgname, cc_delta, sc_delta)

        success = True
        # B9/B3: record the exact emitted basenames while the patched PKGBUILD
        # is still present, so the (later, decoupled) install step matches this
        # build's artifacts precisely instead of prefix-globbing PKGDEST. Fire
        # whenever the on-disk names may differ from the un-patched PKGBUILD —
        # i.e. an extracted profile OR a rename (kernel local-rename / -sysforge
        # suffix). A rename is exactly the case pkgname scoping can't recover at
        # install time (``linux`` PKGBUILD, ``linux-sysforge-*`` artifacts), so
        # without the manifest the install would find nothing to attribute.
        # ``pkgbuild_path`` here is the patched sidecar, so --packagelist emits
        # the renamed names.
        if extracted_profile is not None or rename or local_rename:
            _capture_built_manifest(pkgbuild_path)
    except AlreadyBuilt:
        # PKGDEST already holds this build's renamed artifacts from a prior run,
        # so makepkg refused to rebuild — no fresh build ran and the success-path
        # capture above was skipped. But the decoupled install step still needs
        # the manifest to locate renamed artifacts the un-patched on-disk PKGBUILD
        # cannot name (``linux`` PKGBUILD vs ``linux-sysforge-*`` artifacts);
        # without it, pkgname scoping matches nothing and the kernel stage fails
        # with "nothing to install". The patched sidecar is still on disk here
        # (a failed/refused build leaves it in place), so --packagelist against it
        # emits the renamed names. Same rename/extracted-profile guard as above.
        if extracted_profile is not None or rename or local_rename:
            _capture_built_manifest(pkgbuild_path)
        raise
    except RuntimeError:
        raise
    except Exception as e:
        handle_failure("tempfile_write_failed", str(e), config)
    finally:
        if extracted_profile is not None:
            if success:
                cleanup_patch_artifacts(pkgbuild_path.parent / "PKGBUILD")
            else:
                warn_artifacts_left(bool(extracted_profile))
        else:
            if success:
                if pkgbuild_path.exists():
                    pkgbuild_path.unlink()
                    _build_log.info(f"Removed patched PKGBUILD: {pkgbuild_path}")
            else:
                _build_log.warn(
                    f"Build failed — leaving patched PKGBUILD in place: {pkgbuild_path}"
                )

    # The rename dict (or None) rides back to run() so build_state records the
    # renamed names + origin_pkgbase. When both the kernel local-rename and the
    # -sysforge suffix applied, the on-disk names come from the suffix pass but
    # the origin stays the *upstream* pkgbase, so `sysforge update` keeps
    # source-syncing the upstream tree.
    if rename and local_rename:
        return {
            **rename,
            "origin_pkgbase": local_rename["origin_pkgbase"],
            "origin_pkgnames": local_rename["origin_pkgnames"],
        }
    return rename or local_rename


# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------

@dataclass
class BuildOptions:
    """
    Options for a single makepkg_wrapper.run() invocation.

    All fields default to their safe/do-nothing values so call sites
    only need to specify what they actually care about. Adding a new
    option only requires: add a field here with its default, handle it
    in run() — no changes needed at call sites that don't use it.
    """
    extra_flags: list | None = None
    interactive: bool = False
    kernel_build_headers: bool = True
    kernel_build_docs: bool = True
    kconfig_targets: list[str] | None = None
    pkg_log: bool = True
    persist_log: bool = False
    log_dir: Path | None = None
    profile_conf: str | None = None
    cc_override: str | None = None
    cxx_override: str | None = None
    ld_override: str | None = None
    cache_report: bool = False
    init_session: bool = True
    update: bool = True
    profile_override: str | None = None
    compiler_flags_extra: str | None = None
    linker_flags_extra: str | None = None
    strip_full_lto: bool = False
    extra_env: dict | None = None
    state_dir: Path | None = None
    abi_check: bool = False
    strip_flags: frozenset | set | None = None
    force_batch: bool = False
    no_install: bool = False  # strip -i/--install: build the package but do not install it
    pgo_managed: bool = False
    source: str | None = None  # "aur" | "repo" | "git" | "local" — persisted in build_state
    # e.g. "kernel" — persisted so `sysforge update` skips by default
    owner_stage: str | None = None
    # "gcc" | "stock_llvm" | "pgo_llvm" — persisted so `sysforge update` can flag drift
    toolchain_variant: str | None = None
    # Q9: opaque active-toolchain identity — persisted so `sysforge update` flags
    # same-variant rebuilds
    toolchain_fingerprint: str | None = None
    # force -DLLVM_DIR at a staged libLLVM prefix (toolchain PGO passes 1b/3b/3c)
    cmake_llvm_dir: str | None = None
    # "record" | "use" — mesa instrumentation PGO (`build --pgo`); no-op for non-mesa pkgbases
    pgo_mode: str | None = None
    # e.g. "autofdo_kernel" — stage-supplied optimization mode; seeds record_build_mode →
    # -sysforge rename + build_state. mesa --pgo=use sets its own ("pgo_mesa") internally.
    optimization_build_mode: str | None = None
    # F40: patch the cloned upstream's pkgbase to this local name (coexist) — set by the
    # kernel stage when pkgname != upstream_pkgname
    rename_pkgbase_to: str | None = None


def _record_build_state(pkgbuild_path, pkgmeta, resolved_profile, options,
                        rename, record_build_mode, build_elapsed):
    """Record build metadata for `sysforge update` (non-fatal).

    The single per-build record site: called once after a successful build.
    build_elapsed is the measured wall-clock build duration in whole seconds,
    threaded into BuildState.record(build_seconds=...) (1.2.0-F21).
    """
    try:
        from sysforge.pipeline.state import resolve_state_dir
        from sysforge.primitives.build_state import BUILD_MODE_SOURCE, BuildState
        from sysforge.primitives.vcs_pkgver import read_built_upstream_commit
        _state_dir, _ = resolve_state_dir(options.state_dir)
        bs = BuildState(_state_dir)
        globals_ = pkgmeta.get("globals", {})
        pkgnames = globals_.get("pkgname", [])
        if isinstance(pkgnames, str):
            pkgnames = [pkgnames]
        pkgbase = globals_.get("pkgbase") or (pkgnames[0] if pkgnames else "unknown")
        fs = serialize_flags(resolved_profile) if resolved_profile is not None else None

        # An optimization rename (-sysforge) changes what landed on disk: the
        # built artifacts and installed packages carry the suffix, so that is
        # what build_state must track (and what the filename_versions match
        # below keys on). origin_pkgbase preserves the upstream correlation so
        # `sysforge update`'s source-sync still finds the original tree.
        origin_pkgbase = None
        if rename:
            pkgnames = list(rename["renamed_pkgnames"])
            pkgbase = rename["renamed_pkgbase"] or pkgbase
            origin_pkgbase = rename["origin_pkgbase"]

        # Single-git-source VCS packages: capture the just-built upstream
        # SHA from the cloned srcdir so the next `sysforge update --devel`
        # can short-circuit pkgver() resolution via `git ls-remote`.
        # Returns None for non-VCS, multi-git-source, or unparseable
        # source URLs — recorded entries simply omit the field and fall
        # through to the full path on the next check.
        upstream_commit = read_built_upstream_commit(pkgbuild_path.parent)

        # Prefer pkgver/pkgrel/epoch from the built .pkg.tar.* filenames
        # over the static PKGBUILD parse. The parser intentionally leaves
        # shell parameter-expansion forms (e.g. ``${_ver/[a-z]/.${_ver//[0-9.]/}}``)
        # untouched, so packages using them would otherwise record a
        # literal ``$...`` string as pkgver and always mismatch vercmp.
        filename_versions: dict[str, tuple[str, str, str]] = {}
        for p in _find_artifacts(pkgbuild_path.resolve().parent):
            for name in pkgnames:
                if name in filename_versions:
                    continue
                parsed = _parse_built_pkg_filename(name, p.name)
                if parsed is not None:
                    filename_versions[name] = parsed

        # The clone HEAD of the build that just succeeded becomes the
        # review baseline: the PKGBUILD review gate (pkgbuild_review.py)
        # diffs future HEADs against it. Stamped here — the single
        # record site — so dep builds and pipeline stages are covered
        # without every caller threading the value. None (non-git dir)
        # leaves any prior value sticky.
        from sysforge.primitives.pkgbuild_review import head_commit as _review_head
        _reviewed = _review_head(pkgbuild_path.parent)
        for name in pkgnames:
            ep, ver, rel = filename_versions.get(name, (
                globals_.get("epoch", "0"),
                globals_.get("pkgver", ""),
                globals_.get("pkgrel", "1"),
            ))
            bs.record(
                pkgname=name,
                pkgver=ver,
                pkgrel=rel,
                epoch=ep,
                pkgbase=pkgbase,
                pkgbuild_dir=pkgbuild_path.parent,
                build_mode=record_build_mode or BUILD_MODE_SOURCE,
                flags_string=fs,
                built_upstream_commit=upstream_commit,
                source=options.source,
                owner_stage=options.owner_stage,
                toolchain_variant=options.toolchain_variant,
                toolchain_fingerprint=options.toolchain_fingerprint,
                reviewed_commit=_reviewed,
                origin_pkgbase=origin_pkgbase,
                build_seconds=build_elapsed,
            )
        bs.save()
        _build_log.info(f"Recorded build state for {pkgbase!r}")
    except Exception as e:
        _build_log.warn(f"Failed to record build state: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(pkgbuild_path, options: BuildOptions | None = None):
    if options is None:
        options = BuildOptions()

    config_paths = [Path(options.profile_conf)] if options.profile_conf is not None else None
    config = load_config(config_paths=config_paths)
    # Note: load_config() debug dumps (full flag_profiles etc.) fire here, before the
    # pkg log is open — they go to the unified log (if open) and terminal only.

    try:
        pkgbuild_path = find_pkgbuild(str(pkgbuild_path), config)
    except FileNotFoundError as e:
        _build_log.fatal(str(e))

    # Open per-package log as early as possible so subsequent debug output is captured.
    # Use the PKGBUILD directory name; this matches pkgbase in all normal cases.
    if options.pkg_log:
        log_base = Path(options.log_dir) if options.log_dir is not None else pkgbuild_path.parent
        log_path = log_base / f"sysforge_{pkgbuild_path.parent.name}.log"
        log.open_pkg_log(log_path, argv=sys.argv)
        _build_log.info(f"Per-package log: {log_path}")

    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    if options.init_session:
        reset_session()
        emit_system_probes()

    if options.update:
        pkgbuild_dir = pkgbuild_path.parent
        # B8: honor the PKGBUILD's origin classification. Omitting this let the
        # SyncRequest default to source="aur", so a hand-maintained ("local") or
        # git-hosted PKGBUILD was mis-synced as AUR: a "local" kernel PKGBUILD
        # triggered a spurious AUR RPC (that could fatal), and a "git"-hosted
        # PKGBUILD repo was never fetched — building from a stale PKGBUILD.
        result = get_scheduler().request(SyncRequest(
            pkgbase=pkgbuild_dir.name,
            pkgbuild_dir=pkgbuild_dir,
            source=options.source or "aur",
            force_fetch=True,
        ))
        if result.status in (STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED):
            _build_log.fatal(f"source sync failed for {pkgbuild_dir.name}: "
                           f"{result.error or result.status}")
        if result.status == STATUS_DIVERGED:
            _build_log.warn(
                f"{pkgbuild_dir.name}: upstream diverged — building with local PKGBUILD"
            )
    else:
        _build_log.info("--no-update: skipping source sync")

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        handle_failure("pkgbuild_unparseable", str(e), config)
        pkgmeta = {"globals": {}}

    # lib32-* packages always use 32-bit GCC (Arch multilib has no lib32-clang),
    # so they trigger the guard unconditionally. The directory name is the
    # canonical package identifier and is reliable even when ``pkgname`` itself
    # is interpolated (e.g. ``pkgname=lib32-$_basename``), which the static
    # PKGBUILD parser does not expand.
    pkgbuild_has_hardcoded_gcc = (
        has_hardcoded_gcc(pkgmeta)
        or pkgbuild_path.parent.name.startswith("lib32-")
    )
    if pkgbuild_has_hardcoded_gcc:
        _build_log.info(
            "PKGBUILD treated as hardcoded-gcc (build-time function invokes "
            "gcc/g++ or pkgname is lib32-*) — GCC flag guard will be applied "
            "even if the active profile sets CC=clang"
        )

    build_success = False
    try:
        matched_rules = match_rules(pkgmeta, config.get("rules", []))

        if options.profile_override is not None:
            # Bypass rule matching: resolve the named profile directly.
            from sysforge.primitives.profile import merge_extends
            profiles = config.get("profiles", {})
            if options.profile_override not in profiles:
                raise RuntimeError(
                    f"[BUILD] profile_override {options.profile_override!r} "
                    "not found in loaded config"
                )
            resolved_profile = merge_extends(
                options.profile_override, profiles, conflict_groups=conflict_groups
            )
            build_mode = normalize_build_mode(resolved_profile.get("build_mode"))
            _build_log.info(
                f"Profile override: {options.profile_override!r} (build_mode={build_mode!r})"
            )
        else:
            build_mode = get_build_mode(matched_rules, config)
            resolved_profile = None  # resolved below after extracted_profile is known

        kernel_build = (build_mode == "kernel")

        # pgo_llvm_toolchain: inject -fprofile-use if saved profdata is compatible,
        # otherwise prompt to plain-build or skip (default: skip).
        # Skip this block when pgo_managed=True — the toolchain stage controls PGO
        # flags directly via compiler_flags_extra; we must not override or prompt.
        effective_flags_extra = options.compiler_flags_extra
        if build_mode == "pgo_llvm_toolchain" and not options.pgo_managed:
            pkgname = pkgbuild_path.parent.name
            pgo_state, pgo_info = _resolve_pgo_state(pkgbuild_path)
            if pgo_state == "ready":
                pgo_flag = f"-fprofile-use={pgo_info}"
                effective_flags_extra = (
                    f"{options.compiler_flags_extra} {pgo_flag}".strip()
                    if options.compiler_flags_extra
                    else pgo_flag
                )
                _build_log.info(f"Reusing profdata for PGO build: {pgo_info}")
            else:
                reason = pgo_info
                if sys.stdin.isatty():
                    choice = prompt_choice(
                        f"{pkgname}: PGO profdata unavailable ({reason})."
                        " [p]lain build or [s]kip? [S]: ",
                        choices=("p", "plain", "s"),
                        default="s",
                        eof_default="s",
                        tag="BUILD",
                        level="WARN",
                    )
                else:
                    choice = "s"
                    _build_log.warn(
                        f"Non-interactive: skipping pgo_llvm_toolchain build for "
                        f"{pkgname} ({reason})",
                    )
                if choice not in ("p", "plain"):
                    raise PGOBuildSkipped(
                        f"[PGO] Skipped {pkgname!r}: {reason}. "
                        "Run 'sysforge run toolchain' to regenerate profdata."
                    )
                _build_log.warn(f"{pkgname}: Building without PGO: {reason}")

        # mesa instrumentation PGO (`build --pgo=record|use`) — reuses the same
        # compiler_flags_extra seam as the toolchain PGO above (makepkg_conf
        # injects it into CFLAGS/CXXFLAGS/LDFLAGS, which mesa's arch-meson
        # inherits). `record` bakes the store path into an instrumented mesa so
        # *any* GPU app that loads it appends .profraw to the store; `use` merges
        # those and consumes the profile, earning the -sysforge rename below
        # (record_build_mode = "pgo_mesa"). A logged no-op for non-mesa pkgbases —
        # mesa is the only wired target this phase.
        # Seeded from the stage-supplied optimization mode (kernel FDO sets
        # "autofdo_kernel"/"propeller_kernel" here); the mesa --pgo=use block
        # below sets its own. Either way it drives the -sysforge rename in
        # _run_build and the recorded build_state build_mode.
        record_build_mode: str | None = options.optimization_build_mode
        _pgo_globals = pkgmeta.get("globals", {})
        _pgo_names = _pgo_globals.get("pkgname")
        if isinstance(_pgo_names, list):
            _pgo_names = _pgo_names[0] if _pgo_names else None
        _pgo_pkgbase = _pgo_globals.get("pkgbase") or _pgo_names
        if options.pgo_mode:
            # Instrumentation PGO is now a generic per-package build flag (F5):
            # mesa is the seeded target (with bespoke graphics handling) but any
            # package can be profiled. The "not recommended for most packages"
            # warning is emitted once, up front, in BuildVerb.pre_check (it has
            # the config + the allow-list); here we just do the injection.
            from sysforge.primitives import fs_provision, mesa_pgo
            _store = mesa_pgo.resolve_store(pkgbase=_pgo_pkgbase)
            if options.pgo_mode == "record":
                try:
                    fs_provision.ensure_writable_dir(_store)
                except fs_provision.FsProvisionError as e:
                    _build_log.warn(
                        f"PGO store {_store} could not be group-provisioned "
                        f"({e}) — instrumented {_pgo_pkgbase} may be unable to "
                        "write .profraw there"
                    )
                _pgo_flag = mesa_pgo.generate_flag(_store)
            elif options.pgo_mode == "use":
                # Raises MesaPgoError (clean pre-build abort) if nothing was
                # collected or llvm-profdata is unavailable.
                _profdata = mesa_pgo.merge_profraw(_store, pkgbase=_pgo_pkgbase)
                _pgo_flag = mesa_pgo.use_flags(_profdata)
                record_build_mode = mesa_pgo.build_mode_for(_pgo_pkgbase)
            else:
                raise RuntimeError(f"unknown --pgo mode {options.pgo_mode!r}")
            effective_flags_extra = (
                f"{effective_flags_extra} {_pgo_flag}".strip()
                if effective_flags_extra
                else _pgo_flag
            )
            _build_log.ui(
                f"PGO ({options.pgo_mode}) {_pgo_pkgbase}: injecting {_pgo_flag!r}"
            )
        elif _pgo_pkgbase:
            # Durability: no explicit --pgo, but a prior `build <pkg> --pgo=use`
            # left a merged profile in the package's store. A source-tracked
            # package is rebuilt every update cycle; reuse the existing profile
            # (same compiler_flags_extra seam as --pgo=use) rather than silently
            # regressing to a stock build. No re-merge — the optimized build isn't
            # instrumented, so no new .profraw accrues. None ⇒ never PGO-built
            # here ⇒ a normal build. use_flags already demotes the skew warnings
            # so a slightly-stale profile never -Werror-fails the rebuild.
            from sysforge.primitives import mesa_pgo
            from sysforge.primitives.profile import is_llvm_toolchain
            # A clang .profdata only feeds a clang build (the BuildVerb.pre_check
            # LLVM gate doesn't run on the update/plain-build path, so guard
            # here). Resolve the compiler the same way the kernel branch does:
            # explicit override > resolved profile (if already resolved) > env CC.
            _reuse_cc = (
                options.cc_override
                or (resolved_profile.get("CC") if resolved_profile else None)
                or os.environ.get("CC", "")
            )
            _reuse = (
                mesa_pgo.reuse_profdata(pkgbase=_pgo_pkgbase)
                if is_llvm_toolchain(_reuse_cc)
                else None
            )
            if _reuse is not None:
                _pgo_flag = mesa_pgo.use_flags(_reuse)
                record_build_mode = mesa_pgo.build_mode_for(_pgo_pkgbase)
                effective_flags_extra = (
                    f"{effective_flags_extra} {_pgo_flag}".strip()
                    if effective_flags_extra
                    else _pgo_flag
                )
                _build_log.ui(
                    f"PGO (reuse) {_pgo_pkgbase}: re-applying {_reuse} from a "
                    "prior --pgo=use (source rebuild stays profiled)"
                )

        extracted_profile = None
        if build_mode_uses_extracted_profile(build_mode):
            extracted_profile = extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
            if extracted_profile:
                write_extracted_profile(extracted_profile, pkgbuild_path)

        if options.profile_override is None:
            resolved_profile = resolve_profile(
                pkgmeta, matched_rules, config, conflict_groups,
                extracted_profile=extracted_profile,
            )
        # resolved_profile is set on both branches above (profile_override
        # path at line ~1158, the None path just above) — never actually None
        # here, but pyright can't correlate the two separate `if` tests.
        assert resolved_profile is not None  # noqa: S101 — internal invariant, not input validation
        if options.force_batch and not resolved_profile.get("batch", False):
            resolved_profile = dict(resolved_profile)
            resolved_profile["batch"] = True

        # Build-without-install: strip -i/--install so the package is produced
        # but pacman never runs. Callers that split build from install (e.g.
        # the kernel stage's pre-install safety audit) install the artifact
        # themselves via install_built_packages() after their checks pass.
        effective_strip_flags = options.strip_flags
        if options.no_install:
            effective_strip_flags = set(options.strip_flags or ()) | INSTALL_FLAGS
            _build_log.info("no_install: stripping install flags from makepkg invocation")
        active_consumes = resolve_consumes(resolved_profile, pkgmeta, inference_map)
        groups = resolve_groups(pkgmeta, matched_rules, config.get("defaults", {}))

        if resolved_profile.get("clean_builddir", False):
            build_dir = pkgbuild_path.parent
            for entry in build_dir.iterdir():
                if entry.name != "PKGBUILD" and not entry.name.endswith(".PKGBUILD"):
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            _build_log.info(f"Cleaned build dir: {build_dir}")

        import_pgp_keys(pkgmeta, pkgbuild_path)
        run_dep_analysis(pkgmeta, config)

        import time as _time
        _build_start = _time.time()
        rename = _run_build(
                pkgbuild_path, resolved_profile, config, groups,
                active_consumes=active_consumes,
                extracted_profile=(
                    extracted_profile
                    if build_mode_uses_extracted_profile(build_mode)
                    else None
                ),
                pkgmeta=pkgmeta,
                extra_flags=options.extra_flags,
                interactive=options.interactive,
                cc_override=options.cc_override,
                cxx_override=options.cxx_override,
                ld_override=options.ld_override,
                kernel_build=kernel_build,
                kernel_build_headers=options.kernel_build_headers,
                kernel_build_docs=options.kernel_build_docs,
                kconfig_targets=options.kconfig_targets,
                compiler_flags_extra=effective_flags_extra,
                linker_flags_extra=options.linker_flags_extra,
                strip_full_lto=options.strip_full_lto,
                injected_env=options.extra_env,
                strip_flags=effective_strip_flags,
                pkgbuild_has_hardcoded_gcc=pkgbuild_has_hardcoded_gcc,
                state_dir=options.state_dir,
                toolchain_variant=options.toolchain_variant,
                cmake_llvm_dir=options.cmake_llvm_dir,
                optimization_build_mode=record_build_mode,
                rename_pkgbase_to=options.rename_pkgbase_to,
            )
        build_success = True
        build_elapsed = int(_time.time() - _build_start)

        # Post-build: abort if profraw files are accumulating outside PGO pass 2.
        # This catches instrumented LLVM binaries leaking onto the system after a
        # failed or partial toolchain run — the builds silently emit profraw that
        # can fill the disk.  Fatal because continued builds would keep generating
        # profraw until storage is exhausted.
        #
        # Files predating _build_start are orphans from a prior run the user has
        # since cleaned up (e.g. reinstalled llvm/llvm-libs); purge them rather
        # than aborting forever.  Only profraw files touched by THIS build signal
        # a still-instrumented system.
        if not options.pgo_managed:
            _tcfg = _try_load_toml(TOOLCHAIN_PATH) if TOOLCHAIN_PATH.exists() else None
            _pgo_store = resolve_pgo_store(_tcfg)
            if _pgo_store.is_dir():
                _all_profraw = list(_pgo_store.glob("**/*.profraw"))
                # 1s slack absorbs filesystem mtime rounding on second-granularity fs.
                _freshness_cutoff = _build_start - 1
                _fresh_profraw = [
                    f for f in _all_profraw if f.stat().st_mtime >= _freshness_cutoff
                ]
                _orphan_profraw = [
                    f for f in _all_profraw if f.stat().st_mtime < _freshness_cutoff
                ]
                if _fresh_profraw:
                    _total_bytes = sum(p.stat().st_size for p in _fresh_profraw)
                    _build_log.fatal(
                        f"{len(_fresh_profraw)} stale .profraw files "
                        f"({_total_bytes / 1024 / 1024:.1f} MiB) in {_pgo_store} — "
                        "instrumented LLVM binaries may be installed on this system. "
                        "Reinstall clean llvm/llvm-libs or run 'sysforge run toolchain' "
                        "to complete the PGO build."
                    )
                if _orphan_profraw:
                    _orphan_bytes = sum(p.stat().st_size for p in _orphan_profraw)
                    for _f in _orphan_profraw:
                        with suppress(OSError):
                            _f.unlink()
                    _build_log.info(
                        f"Purged {len(_orphan_profraw)} orphaned .profraw file(s) "
                        f"({_orphan_bytes / 1024 / 1024:.1f} MiB) from {_pgo_store} "
                        "(prior run residue; current build produced none)"
                    )

        # Post-build ABI check (non-fatal) — owned by abi_check.py
        if options.abi_check:
            from sysforge.primitives.abi_check import report_post_build_abi
            report_post_build_abi(_find_artifacts(pkgbuild_path.resolve().parent))

        # Record build metadata for `sysforge update` (non-fatal)
        _record_build_state(
            pkgbuild_path, pkgmeta, resolved_profile, options,
            rename, record_build_mode, build_elapsed,
        )

    finally:
        if options.pkg_log:
            log.close_pkg_log(success=build_success, persist=options.persist_log)

    if options.cache_report:
        emit_session_report()


if __name__ == "__main__":
    run(sys.argv[1])
