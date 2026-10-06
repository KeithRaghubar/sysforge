# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/fdo.py — sample-based feedback-directed optimization.

AutoFDO and Propeller, orchestrated through ``primitives/kernel_fdo.py``. Both
need the LLVM toolchain, so the gate here is a hard precondition rather than a
warning: a sample-based profile collected under one compiler is not applicable
to the other, and applying it silently would produce a kernel optimized against
the wrong codegen.
"""
from dataclasses import dataclass
from pathlib import Path
import os

from sysforge.primitives import kernel_fdo

from sysforge import log

_log = log.get_logger("KERNEL")


# Sample-based FDO (AutoFDO / Propeller) — kernel_fdo orchestration
#
# Three steps spanning reboots, run twice for Propeller: `record` builds a
# profiling kernel (CONFIG_AUTOFDO_CLANG=y, `-profiling` role name); `capture` is
# a read-only step that refuses unless tools are present and the profiling kernel
# is booted, then prints perf + llvm-profgen (round 1) or
# generate_propeller_profiles (round 2) commands; `use` rebuilds consuming the
# collected profile (injected via the build's extra_env make-variables) under the
# `-fdo` / `-propeller` role name. LLVM-only — there is no GCC path. See
# DESIGN.md §Kernel stage.
# ---------------------------------------------------------------------------


def resolve_fdo(options, kernel_cfg=None):
    """Resolve the kernel FDO request: ``(mode, propeller, explicit)``.

    ``mode`` is one of ``kernel_fdo.VALID_MODES`` (``record``/``capture``/``use``)
    or ``None``. Precedence: an explicit ``--autofdo=…`` wins (``explicit`` is
    True); otherwise ``kernel.toml [fdo] mode`` makes a plain run a ``use``
    build (``explicit`` False). Config never implies record/capture: they need
    a reboot and a human workload. ``--propeller`` is a modifier that layers
    Propeller on the AutoFDO cycle, so it requires a mode. Raises on an invalid
    combination.
    """
    mode = getattr(options, "kernel_fdo", None)
    propeller = bool(getattr(options, "kernel_propeller", False))
    if mode is not None and mode not in kernel_fdo.VALID_MODES:
        raise RuntimeError(
            f"[KERNEL] invalid --autofdo value {mode!r}: must be one of "
            f"{kernel_fdo.VALID_MODES}"
        )
    if propeller and mode is None:
        raise RuntimeError(
            "[KERNEL] --propeller layers Propeller on the AutoFDO cycle and "
            "requires --autofdo=record|capture|use"
        )
    if mode is not None:
        return mode, propeller, True
    cfg_mode = kernel_cfg.fdo_mode if kernel_cfg is not None else "off"
    if cfg_mode == "off":
        return None, False, False
    return "use", cfg_mode == "propeller", False


def fdo_is_llvm(compiler, cc):
    """Whether the resolved kernel compiler is Clang/LLVM (kernel FDO is LLVM-only).

    ``compiler`` is the explicit "gcc"/"llvm" choice (CLI/kernel.toml/pipeline);
    when it is ``None`` (inherited toolchain) we sniff the resolved ``cc`` and,
    failing that, the environment ``CC`` — the same basename the kernel build
    keys its LLVM=1 detection on, so a workstation whose ``CC=clang`` is honored.
    """
    if compiler == "llvm":
        return True
    if compiler == "gcc":
        return False
    probe = cc or os.environ.get("CC", "")
    return bool(probe) and Path(probe).name.startswith("clang")


def gate_fdo_llvm(mode, propeller, compiler, cc, *, explicit=True):
    """Hard-abort when kernel FDO is requested under a non-LLVM toolchain.

    AutoFDO/Propeller have no GCC path in sysforge, and the kernel's
    CONFIG_AUTOFDO_CLANG/CONFIG_PROPELLER_CLANG have no GCC equivalent at all — so
    this is a clean refusal before any build work. The single home for the
    kernel-FDO LLVM gate. A config-driven request (``explicit=False``) names the
    kernel.toml setting and its way out rather than a flag the user never passed.
    """
    if fdo_is_llvm(compiler, cc):
        return
    from sysforge.primitives.profile import LLVM_REQUIRED_HINT
    if not explicit:
        cfg_mode = "propeller" if propeller else "autofdo"
        raise RuntimeError(
            f'[KERNEL] kernel.toml [fdo] mode = "{cfg_mode}" requires the LLVM '
            'toolchain for the kernel. Set mode = "off", or build the kernel with '
            'LLVM (kernel.toml compiler = "llvm" / --compiler=llvm). '
            f"{LLVM_REQUIRED_HINT}"
        )
    feature = "kernel Propeller" if propeller else "kernel AutoFDO"
    raise RuntimeError(
        f"[KERNEL] {feature} (--autofdo={mode}) requires the LLVM toolchain. "
        f"{LLVM_REQUIRED_HINT}"
    )


@dataclass(frozen=True)
class FdoPlan:
    """What this kernel build applies and installs as (one decision, threaded
    through the stage)."""

    mode: str | None            # "record" | "use" | None
    propeller: bool             # effective (after any fallback)
    eff_pkgname: str
    env: dict | None
    build_mode: str | None      # optimization build_mode (use only)
    round_store: Path | None    # record: store to write round.toml into after install
    afdo_sha256: str | None     # record --propeller: pinned profile's hash
    profile_store: Path | None = None  # use: the store the profile is consumed from
    # Pass makepkg -f on the first build: every record (R20: a reused package
    # has the wrong round's inputs or no build tree), and a use whose applied
    # profile changed since the last install of that role (R21).
    force_rebuild: bool = False
    applied_fingerprint: str | None = None  # use: written to applied.toml after install


def built_pkgver(pkgbuild, *, pkgname=None) -> str | None:
    """``pkgver-pkgrel`` the build just produced, read after makepkg ran.

    makepkg builds from the patched copy (``makepkg -p PKGBUILD.sysforge``) and
    rewrites the pkgver literal of *that* file from ``pkgver()``; the
    operator's PKGBUILD keeps the previous value. So, in order: the patched
    copy while it still exists (a failed or AlreadyBuilt build leaves it), the
    build manifest's entry for ``pkgname`` (recorded from the rewritten copy
    before a successful build deletes it), then the PKGBUILD's static literal.
    ``None`` when none of them can be read statically.
    """
    from sysforge.primitives.makepkg_wrapper import built_manifest_version
    from sysforge.primitives.pkgbuild_patcher import PATCHED_PKGBUILD_NAME
    pkgbuild = Path(pkgbuild)
    patched = pkgbuild.parent / PATCHED_PKGBUILD_NAME
    if patched.is_file():
        ver = _parse_static_pkgver(patched)
        if ver:
            return ver
    if pkgname:
        ver = built_manifest_version(pkgbuild.parent, pkgname)
        if ver:
            return ver
    return _parse_static_pkgver(pkgbuild)


def _parse_static_pkgver(path) -> str | None:
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    try:
        parsed = parse_pkgbuild(path)
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        return None
    return _static_pkgver(parsed)


def _static_pkgver(parsed) -> str | None:
    g = parsed["globals"]
    ver, rel = g.get("pkgver"), g.get("pkgrel")
    if not isinstance(ver, str) or not ver or "$" in ver:
        return None
    if isinstance(rel, str) and "$" in rel:
        return None
    return f"{ver}-{rel}" if isinstance(rel, str) and rel else ver


def building_pkgver(pkgbuild) -> str | None:
    """``pkgver-pkgrel`` of the PKGBUILD about to build, or ``None`` if unknown.

    A VCS PKGBUILD (one defining ``pkgver()``) reads as ``None``: before the
    build its static ``pkgver`` is the *previous* build's value, and the real
    one is only known once makepkg runs ``pkgver()``. Otherwise as
    :func:`built_pkgver`. Callers skip the compare on ``None`` rather than
    refuse on a spurious mismatch.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    try:
        parsed = parse_pkgbuild(pkgbuild)
    except (OSError, ValueError):
        return None
    if "pkgver" in parsed["functions"]:
        return None
    return _static_pkgver(parsed)


def plan_fdo_build(mode, propeller, pkgname, *, explicit, building_pkgver,
                   dry_run=False, tcfg=None) -> FdoPlan:
    """Decide the FDO side of this build. Raises ``RuntimeError`` (clean
    pre-build refusal) when it cannot proceed.

    ``record --propeller`` is round 2: it pins round 1's AutoFDO profile into
    the Propeller store and applies it, so the profiling kernel and the final
    ``use`` build share one AutoFDO profile. ``--dry-run`` copies nothing and
    provisions nothing.
    """
    try:
        if mode == "record":
            return _plan_record(propeller, pkgname, dry_run=dry_run, tcfg=tcfg)
        if mode == "use":
            return _plan_use(propeller, pkgname, explicit=explicit,
                             building_pkgver=building_pkgver, tcfg=tcfg)
    except kernel_fdo.KernelFdoError as e:
        raise RuntimeError(f"[KERNEL] {e}") from e
    return FdoPlan(None, False, pkgname, None, None, None, None)


def _plan_record(propeller, pkgname, *, dry_run, tcfg) -> FdoPlan:
    store = kernel_fdo.resolve_store(pkgname, propeller=propeller, tcfg=tcfg)
    env, sha = None, None
    if not dry_run:
        # round.toml (and in round 2 the pinned profile) lands here; provision
        # it first, as capture does, so the pin copy below meets a group-owned
        # dir rather than one created with the caller's umask.
        from sysforge.primitives import fs_provision
        try:
            fs_provision.ensure_writable_dir(store)
        except fs_provision.FsProvisionError as e:
            _log.warn(f"FDO store {store} could not be group-provisioned ({e}); "
                      "the round record may be unwritable there")
    if propeller:
        try:
            pinned, sha = kernel_fdo.pin_autofdo_profile(
                pkgname, tcfg=tcfg, dry_run=dry_run)
        except OSError as e:
            raise RuntimeError(
                f"[KERNEL] cannot pin the AutoFDO profile into {store}: {e}") from e
        env = kernel_fdo.use_env(store, propeller=False, afdo=pinned)
    return FdoPlan("record", propeller, kernel_fdo.record_pkgname(pkgname),
                   env, None, store, sha, force_rebuild=True)


def _plan_use(propeller, pkgname, *, explicit, building_pkgver, tcfg) -> FdoPlan:
    """``use`` plan. ``explicit`` (``--autofdo=use``) refuses on a stale
    Propeller round; config-driven (``[fdo] mode``) falls back to AutoFDO only,
    so a routine kernel update keeps building rather than failing."""
    afdo_store = kernel_fdo.resolve_store(pkgname, propeller=False, tcfg=tcfg)
    if not propeller:
        return _plan_autofdo(afdo_store, pkgname, explicit=explicit,
                             building_pkgver=building_pkgver)
    prop_store = kernel_fdo.resolve_store(pkgname, propeller=True, tcfg=tcfg)
    try:
        info = kernel_fdo.require_profile(prop_store, propeller=True)
    except kernel_fdo.KernelFdoError as e:
        if explicit:
            raise
        raise kernel_fdo.KernelFdoError(
            f'{e} Or set kernel.toml [fdo] mode = "autofdo".') from e
    prop_name = kernel_fdo.optimized_pkgname(pkgname, propeller=True)
    if building_pkgver is None:
        if not explicit:
            _log.warn(
                "the building kernel version is unknown until makepkg runs pkgver(), "
                "so the round-2 Propeller profile cannot be verified against it: "
                f"building AutoFDO only. If {prop_name} is installed, it stays "
                "installed with the old profile; remove it when you no longer want "
                "it, or redo round 2.")
            return _plan_autofdo(afdo_store, pkgname, explicit=False,
                                 building_pkgver=building_pkgver)
        _log.warn("cannot verify the round-2 Propeller profile matches this build "
                  "(the building version is unknown until makepkg runs pkgver()); "
                  "applying it anyway")
    elif info.pkgver and info.pkgver != building_pkgver:
        if explicit:
            raise kernel_fdo.KernelFdoError(
                f"round 2 profiled {info.pkgver} but this build is {building_pkgver}; "
                "Propeller profiles only match the exact build they sampled. Redo "
                "round 2, or build with `--autofdo=use` (AutoFDO only).")
        _log.warn(
            f"Propeller round 2 profiled {info.pkgver}, this build is "
            f"{building_pkgver}: building AutoFDO only. If {prop_name} is "
            "installed, it stays installed with the old profile; remove it when you "
            "no longer want it, or redo round 2.")
        return _plan_autofdo(afdo_store, pkgname, explicit=False,
                             building_pkgver=building_pkgver)
    return FdoPlan("use", True, prop_name, kernel_fdo.use_env(prop_store, propeller=True),
                   kernel_fdo.BUILD_MODE_PROPELLER, None, None,
                   profile_store=prop_store,
                   **_applied_state(prop_store, propeller=True))


def _plan_autofdo(store, pkgname, *, explicit, building_pkgver) -> FdoPlan:
    """AutoFDO ``use`` plan from the live round-1 store. A profile collected on a
    different major.minor still applies (AutoFDO degrades gracefully), so it
    only warns: upstream version semantics, not a tuned threshold."""
    try:
        info = kernel_fdo.require_profile(store, propeller=False)
    except kernel_fdo.KernelFdoError as e:
        if explicit:
            raise
        # require_profile's text ends "re-run `--autofdo=use`": the wrong next
        # step when kernel.toml [fdo] mode drives the build.
        raise kernel_fdo.KernelFdoError(
            f"kernel.toml [fdo] mode applies the AutoFDO profile at "
            f"{kernel_fdo.autofdo_profile_path(store)}, but there is none. Collect "
            "one: `sysforge run kernel --autofdo=record`, reboot into it, run "
            "`--autofdo=capture` and the commands it prints, then a plain "
            '`sysforge run kernel`. Or set kernel.toml [fdo] mode = "off".') from e
    if info is None or not info.pkgver:
        _log.warn("AutoFDO profile's collected version unknown (collected before "
                  "version tracking); consider a new round.")
    else:
        have = kernel_fdo.kernel_major_minor(info.pkgver)
        want = kernel_fdo.kernel_major_minor(building_pkgver)
        if have and want and have != want:
            _log.warn(
                f"AutoFDO profile was collected on {have[0]}.{have[1]}, building "
                f"{want[0]}.{want[1]}: it still applies but drifts; consider a new round.")
    return FdoPlan("use", False, kernel_fdo.optimized_pkgname(pkgname, propeller=False),
                   kernel_fdo.use_env(store, propeller=False),
                   kernel_fdo.BUILD_MODE_AUTOFDO, None, None, profile_store=store,
                   **_applied_state(store, propeller=False))


def _applied_state(store, *, propeller) -> dict:
    """``force_rebuild`` / ``applied_fingerprint`` for a ``use`` plan (R21).

    Rebuild unless the profile files this build consumes hash to what the last
    successful install of this role applied (``applied.toml``), so a new
    capture at an unchanged pkgver never silently reinstalls the package built
    with the old profile. An unreadable input cannot be verified, so it
    rebuilds. Read-only: the sidecar is written only after install.
    """
    try:
        fp = kernel_fdo.applied_fingerprint(store, propeller=propeller)
    except OSError:
        return {"force_rebuild": True, "applied_fingerprint": None}
    return {"force_rebuild": kernel_fdo.read_applied(store) != fp,
            "applied_fingerprint": fp}


def finish_fdo_build(plan, *, built_dir, pkgver) -> None:
    """After a successful install: write the FDO sidecar for this plan's mode.

    The stage's one post-install call. ``record`` writes ``round.toml``
    (:func:`finish_fdo_record`); ``use`` records the applied-profile
    fingerprint in ``applied.toml`` so the next run reuses the package only
    while the profile is unchanged. Best-effort: a write failure only warns,
    since the kernel is already installed.
    """
    if plan.mode == "record":
        finish_fdo_record(plan, built_dir=built_dir, pkgver=pkgver)
    elif plan.mode == "use" and plan.profile_store and plan.applied_fingerprint:
        try:
            kernel_fdo.write_applied(plan.profile_store, plan.applied_fingerprint)
        except OSError as e:
            _log.warn(
                f"could not record the applied profile in {plan.profile_store} ({e}); "
                "the next run rebuilds the kernel even if the profile is unchanged.")


def finish_fdo_record(plan, *, built_dir, pkgver) -> None:
    """After a successful record install: write the round sidecar capture reads.

    An AlreadyBuilt record (``built_dir is None``) installed a kernel built in an
    earlier run, so when the version is unchanged it keeps that run's tree (so
    capture still finds vmlinux) *and* its pin hash (that kernel applied the
    earlier pin, which this run's planning just overwrote). With no matching
    prior round it records no hash, so ``use --propeller`` refuses rather than
    trusting an unknown pin. Best-effort: a write failure only warns, since the
    kernel is already installed.
    """
    if plan.mode != "record" or plan.round_store is None:
        return
    sha = plan.afdo_sha256
    if built_dir is None:
        prev = kernel_fdo.read_round(plan.round_store)
        if prev and prev.pkgver == pkgver:
            built_dir, sha = prev.build_dir, prev.afdo_sha256
        else:
            sha = None
    try:
        kernel_fdo.write_round(plan.round_store, pkgver=pkgver, build_dir=built_dir,
                               afdo_sha256=sha)
    except OSError as e:
        _log.warn(
            f"could not write the round record in {plan.round_store} ({e}); "
            "capture will fall back to the system BUILDDIR and may not find "
            "vmlinux. Re-running `--autofdo=record` restores it.")


def run_fdo_capture(pkgname, propeller, dry_run):
    """``--autofdo=capture``: print commands that work as printed, or refuse.

    Read-only — no build, install, or sentinel. Refuses (non-zero) when a needed
    tool is missing or the running kernel is not the profiling kernel, printing
    nothing runnable.
    """
    store = kernel_fdo.resolve_store(pkgname, propeller=propeller)
    record_name = kernel_fdo.record_pkgname(pkgname)
    if dry_run:
        _log.ui(f"[dry-run] would print AutoFDO capture commands for {pkgname} "
                f"(store {store})")
        return
    missing = kernel_fdo.missing_tools(propeller=propeller)
    if missing:
        raise RuntimeError("[KERNEL] capture needs tools that are not on PATH:\n  "
                           + "\n  ".join(missing))
    info = kernel_fdo.read_round(store)
    try:
        vmlinux = kernel_fdo.resolve_vmlinux(
            record_name, recorded_build_dir=info.build_dir if info else None)
    except kernel_fdo.KernelFdoError as e:
        raise RuntimeError(f"[KERNEL] {e}") from e
    sampling = kernel_fdo.detect_branch_sampling()
    from sysforge.primitives import fs_provision
    try:
        fs_provision.ensure_writable_dir(store)
    except fs_provision.FsProvisionError as e:
        _log.warn(f"FDO store {store} could not be group-provisioned ({e}); "
                  "the converter may be unable to write the profile there")
    if sampling.supported:
        _log.info(f"branch sampling: {sampling.note}")
    else:
        _log.warn(f"branch sampling unsupported on this CPU: {sampling.note}")
    _log.ui(f"{'Propeller (round 2)' if propeller else 'AutoFDO (round 1)'} capture "
            f"on {record_name}:")
    for line in kernel_fdo.capture_commands(
            store, sampling=sampling, vmlinux=vmlinux, propeller=propeller):
        _log.ui(f"  {line}")
    _log.ui("Then build the optimized kernel: "
            f"sysforge run kernel --autofdo=use{' --propeller' if propeller else ''}")


# ---------------------------------------------------------------------------
# kernel.toml loading
# ---------------------------------------------------------------------------
