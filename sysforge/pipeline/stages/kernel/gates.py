# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/gates.py — refuse-before-damage checks around the kernel build.

Three gates, and unlike most of sysforge they guard against a failure the user
cannot recover from at the terminal. Gate 1 is a cheap preflight before any
build time is spent; Gate 2 audits the built artifacts and the kconfig actually
produced against the fragment that was requested; Gate 3 verifies the installed
kernel before the run is called a success.

Gate 2's kconfig drift check is the subtle one: kconfig silently drops symbols
whose dependencies are unmet, so a fragment can be "applied" in full and still
not be present in the built config.
"""
from pathlib import Path

from sysforge.primitives import device_probe
from sysforge.primitives import kbuild_map
from sysforge.primitives import kernel_safety
from sysforge.primitives.render import arrow
from sysforge.primitives.render import ellipsis_glyph

from sysforge.pipeline.stages.kernel import config

from sysforge import log

_log = log.get_logger("KERNEL")


# Boot-safety gates
#
# The kernel stage must never leave the machine unbootable. Three gates wrap
# the build/install: a cheap pre-build preflight (Gate 1), a resolved-.config
# audit between build and install (Gate 2 — the only placement that catches a
# Kconfig dependency cascade like CONFIG_SND_PCI=n hiding CONFIG_SND_HDA_INTEL),
# and a post-install boot-readiness check (Gate 3). Brick-class findings
# hard-fail; everything else warns. See DESIGN.md §Kernel stage boot-safety.
# ---------------------------------------------------------------------------


def gate1_preflight(kernel_cfg, options, pkgname, *, dry_run):
    """Cheap pre-build safety checks. Returns the RootTopology for Gate 2.

    Hard-fails (RuntimeError) on brick conditions — no fallback kernel (E1),
    or a missing/too-full /boot (D5) — unless overridden. In dry-run these are
    downgraded to warnings so the run can still preview. The remaining checks
    (localmodconfig strip warning, DKMS rebuild reminder, mkinitcpio HOOKS vs
    root topology) are advisory.
    """
    require_fallback = kernel_cfg.require_fallback_kernel
    boot_audit = kernel_cfg.boot_audit
    min_free = kernel_cfg.min_boot_free_mb
    allow_no_fallback = bool(getattr(options, "allow_no_fallback", False))

    # E1 — fallback-kernel guarantee.
    if require_fallback and not allow_no_fallback:
        fallbacks = kernel_safety.find_fallback_kernels(exclude_pkg=pkgname)
        if fallbacks:
            _log.info(f"Fallback kernel(s) present: {', '.join(fallbacks)}")
        else:
            msg = (
                f"no fallback kernel found — installing {pkgname} as the only "
                "kernel risks an unbootable system with no recovery path. "
                "Install a stock kernel (e.g. linux-lts) first, or pass "
                "--allow-no-fallback / set require_fallback_kernel = false."
            )
            if dry_run:
                _log.ui(f"{msg} (dry-run: would abort)")
            else:
                raise RuntimeError(f"[KERNEL] {msg}")

    # D5 — /boot mounted with headroom.
    if boot_audit:
        space = kernel_safety.check_boot_mount_space(min_mb=min_free)
        if space is not None:
            if dry_run:
                _log.ui(f"[dry-run] {space.message}")
            else:
                raise RuntimeError(f"[KERNEL] {space.message} {space.remediation}")

    topology = kernel_safety.detect_root_topology()
    _log.info(
        f"Root topology: fstype={topology.root_fstype} "
        f"transports={','.join(topology.transports) or '-'} "
        f"crypt={topology.uses_crypt} lvm={topology.uses_lvm} "
        f"raid={topology.uses_raid}"
    )

    # A2 — localmodconfig strips inactive hardware.
    if kernel_cfg.capture_lsmod_snapshot:
        from sysforge.pipeline.state import resolve_state_dir

        state_dir, _ = resolve_state_dir(options.state_dir)
        snapshot_path = Path(state_dir) / "lsmod.snapshot"
        _log.warn(
            "lsmod snapshot captured for `make localmodconfig` — the snapshot "
            "accumulates across builds so intermittently-loaded modules are "
            "kept, but drivers for hardware never active while capturing "
            f"remain excluded. Delete {snapshot_path} to reset the "
            "accumulated set."
        )

    # F1 — DKMS modules will need rebuilding against the new kernel.
    build_headers, _ = config.resolve_subpackages(kernel_cfg, options)
    dkms = kernel_safety.list_dkms_modules()
    if not build_headers:
        # Headers are being dropped from the build — the strongest risk surface.
        msg = (
            f"kernel -headers subpackage disabled — {pkgname}-headers will NOT "
            "be built/installed. Out-of-tree and DKMS modules need the matching "
            "kernel headers to compile; without them they cannot rebuild and "
            "will not load on reboot."
        )
        if dkms:
            msg += (
                f" DKMS modules present ({', '.join(dkms)}) will fail to rebuild "
                "(nvidia → black screen). Re-enable with --headers or "
                "build_headers = true."
            )
        _log.warn(msg)
    elif dkms:
        _log.warn(
            f"DKMS modules present ({', '.join(dkms)}) — they must rebuild "
            f"against {pkgname}; ensure {pkgname}-headers is installed or they "
            "will not load on reboot (nvidia → black screen)."
        )

    # C3 — mkinitcpio HOOKS vs root topology.
    for finding in kernel_safety.check_mkinitcpio_hooks(topology):
        _log.warn(f"{finding.message} {finding.remediation}".strip())

    return topology


def resolve_built_config(pkgbuild_dir):
    """Locate the resolved .config left in the kernel build tree.

    The kernel src lives under ``<build>/src/``; ``<build>`` is the makepkg
    BUILDDIR (``$BUILDDIR/<pkgbase>``, resolved from env or system
    ``makepkg.conf``) or the PKGBUILD dir itself. Returns the newest matching
    ``.config`` or None.
    """
    from sysforge.primitives.pacman import get_builddir

    pkgbuild_dir = Path(pkgbuild_dir)
    roots = []
    builddir = get_builddir()
    if builddir:
        roots.append(builddir / pkgbuild_dir.name)
    roots.append(pkgbuild_dir)

    configs = []
    seen = set()
    for root in roots:
        if str(root) in seen:
            continue
        seen.add(str(root))
        src = root / "src"
        if src.is_dir():
            configs.extend(src.glob("**/.config"))
    if not configs:
        return None
    return max(configs, key=lambda p: p.stat().st_mtime)


def built_kernel_release(config_path):
    """Read the built kernel's release string from kbuild's kernel.release."""
    if config_path is None:
        return None
    rel = config_path.parent / "include" / "config" / "kernel.release"
    text = rel.read_text().strip() if rel.exists() else ""
    return text or None


def gate2_audit(pkgbuild_dir, topology, *, skip_boot_audit, state_dir=None):
    """Audit the resolved .config before install. Raises on brick unless skipped.

    Runs *outside* the install sentinel: a brick abort here leaves the system
    completely untouched (nothing installed, no sentinel set), so the running
    kernel stays bootable.

    The resolved .config's parent is the version-exact kernel source tree, so
    this is also where the kbuild module→CONFIG_* map is harvested: it widens
    this audit's device coverage beyond the curated table and (when
    ``state_dir`` is given) is cached for the hardware stage / the next
    fragment write. The parse is best-effort — any failure degrades to the
    curated-only audit, never blocks the gate.
    """
    config_path = resolve_built_config(pkgbuild_dir)
    if config_path is None:
        _log.warn(
            "Gate 2: resolved kernel .config not found in build tree — "
            "boot-critical config could not be validated before install."
        )
        return

    _log.info(f"Gate 2: auditing resolved kernel config {config_path}")

    kconfig_map = None
    try:
        kconfig_map = kbuild_map.parse_kbuild_tree(config_path.parent)
    except Exception as exc:
        _log.warn(
            f"Gate 2: kbuild module→kconfig parse failed ({exc}) — "
            "auditing with the curated table only"
        )
    if kconfig_map and state_dir is not None:
        cache_path = Path(state_dir) / kbuild_map.KBUILD_MAP_FILENAME
        try:
            kbuild_map.save_map(
                cache_path, kconfig_map, built_kernel_release(config_path),
            )
            _log.info(
                f"Cached kbuild module→kconfig map "
                f"({len(kconfig_map)} modules): {cache_path}"
            )
        except OSError as exc:
            _log.warn(f"could not cache kbuild map at {cache_path}: {exc}")

    devices = device_probe.enumerate_devices(kconfig_map=kconfig_map)
    findings = kernel_safety.audit_resolved_config(config_path, topology, devices)
    bricks = [f for f in findings if f.is_brick]

    for f in findings:
        emit = _log.warn if (f.is_brick or f.severity != "info") else _log.info
        emit(f"Gate 2 [{f.severity.upper()}] {f.check_id}: {f.message}")
        if f.remediation:
            _log.info(f"  → {f.remediation}")

    # E2 — overwriting the running kernel's modules.
    kver = built_kernel_release(config_path)
    if kver and kver == kernel_safety.running_kernel_release():
        _log.warn(
            f"Built kernel release {kver} matches the running kernel — its "
            "/lib/modules entry will be overwritten; reboot before relying on "
            "module loading."
        )

    if bricks and not skip_boot_audit:
        raise RuntimeError(
            f"[KERNEL] {len(bricks)} boot-critical config problem(s) in the "
            "built kernel — aborting before install so the running system "
            "stays bootable. Fix the kconfig and rebuild, or pass "
            f"--skip-boot-audit to override. Resolved .config: {config_path}"
        )
    if bricks:
        _log.warn(
            f"--skip-boot-audit: proceeding to install despite {len(bricks)} "
            "brick-class finding(s)"
        )


def gate2_kconfig_drift(pkgbuild_dir, fragment_path):
    """Advisory: warn when options sysforge merged didn't survive into the
    resolved .config.

    Runs post-build (beside Gate 2, pre-install) but **never raises** — a drop
    can be a deliberate ``nconfig`` toggle *or* legitimate dependency
    resolution by ``make olddefconfig``, and sysforge can't tell the two apart
    without a full dep solve. ``fragment_path is None`` (merge disabled or no
    entries) makes this a no-op, so the check is on exactly when the merge is.

    Returns the drift list so the change summary can relocate the same result
    into its own block (2.6.1-F25) without re-running the comparison; the
    mid-run warnings stay exactly as they were. ``None`` — as distinct from
    ``[]`` — means the check did not run at all, which the summary renders as
    an explicit "did NOT run" rather than as silence.
    """
    if fragment_path is None:
        return None

    config_path = resolve_built_config(pkgbuild_dir)
    if config_path is None:
        # B6: WARN, not INFO — on the AlreadyBuilt path there is no build
        # tree at all, so this advisory audit silently never runs exactly
        # where a stale build makes it most relevant. Say so, visibly.
        _log.warn(
            "kconfig drift check did not run — resolved .config not found in "
            "build tree (no fresh build this run, e.g. package already built): "
            "merged kconfig options were NOT verified against the built kernel"
        )
        return None

    requested = kernel_safety.parse_kconfig_text(
        Path(fragment_path).read_text(encoding="utf-8")
    )
    resolved = kernel_safety.parse_kconfig(config_path) or {}
    drifts = kernel_safety.diff_requested_kconfig(requested, resolved)

    if not drifts:
        _log.info(
            f"kconfig drift check: all {len(requested)} merged option(s) "
            "survived into the resolved .config"
        )
        return drifts

    _log.warn(
        f"kconfig drift: {len(drifts)} option(s) sysforge merged differ in the "
        "resolved .config — possibly toggled in `nconfig`, or dropped by "
        "`make olddefconfig` due to unmet dependencies (advisory, not a failure)"
    )
    for d in drifts:
        _log.warn(f"  {d.option}: {d.requested} → {d.resolved} ({d.kind})")
    return drifts


# ---------------------------------------------------------------------------
# kconfig history + change-summary blocks (2.6.1-F25)
# ---------------------------------------------------------------------------

# Symbols rendered inline before the summary truncates. A major kernel bump
# changes thousands; printing them all would bury the version rows the summary
# exists to show. The full list always goes to the unified log.
KCONFIG_DIFF_CAP = 40


def record_and_diff_kconfig(state_dir, pkgname, pkgbuild_dir):
    """Archive this build's resolved .config and diff it against the previous.

    Returns ``(previous_release, changes)`` or ``None`` when there is nothing
    to compare — no build tree this run (the AlreadyBuilt path) or no earlier
    archive (a first build). Best-effort throughout: this is advisory output
    and must never affect the build.
    """
    from sysforge.primitives import kconfig_history

    try:
        config_path = resolve_built_config(pkgbuild_dir)
        if config_path is None:
            return None
        release = built_kernel_release(config_path) or "unknown"
        new = kernel_safety.parse_kconfig(config_path)
        if new is None:
            return None

        prior = kconfig_history.previous(
            state_dir, pkgname, exclude_release=release
        )
        # Archive after reading the prior entry, so a rebuild at the same
        # release does not shadow the config it should be compared against.
        kconfig_history.archive(state_dir, pkgname, release, config_path)
        if prior is None:
            return None
        prev_release, old = prior
        return prev_release, kernel_safety.diff_kconfig(old, new)
    except Exception as exc:  # noqa: BLE001 — advisory only
        _log.debug(f"kconfig history unavailable: {exc}")
        return None


def kconfig_diff_lines(prev_release: str, changes) -> list[str]:
    """Render the build-to-build kconfig diff, capped, with a full-list pointer."""
    if not changes:
        return [f"no kconfig changes since {prev_release}"]

    lines = [f"{len(changes)} symbol(s) changed since {prev_release}:"]
    for change in changes[:KCONFIG_DIFF_CAP]:
        if change.kind == "added":
            lines.append(f"  +{change.option}={change.new}")
        elif change.kind == "removed":
            lines.append(f"  -{change.option} (was {change.old})")
        else:
            lines.append(f"  {change.option}: {change.old} {arrow()} {change.new}")

    remaining = len(changes) - KCONFIG_DIFF_CAP
    if remaining > 0:
        lines.append(f"  {ellipsis_glyph()} and {remaining} more (full list in the run log)")
    return lines


def kconfig_drift_lines(drifts) -> list[str]:
    """Render the requested-vs-resolved drift block.

    ``drifts is None`` means the check never ran (no build tree). B6 established
    that this must be said out loud rather than rendered as silence: it is
    exactly where a stale build makes the check most relevant.
    """
    if drifts is None:
        return [
            "check did NOT run — no resolved .config in the build tree "
            "(no fresh build this run); merged options were not verified"
        ]
    if not drifts:
        return ["all merged options survived into the resolved .config"]
    return [
        f"  {d.option}: {d.requested} {arrow()} {d.resolved} ({d.kind})"
        for d in drifts
    ]


def gate3_verify(pkgbuild_dir, pkgname, bootloader):
    """Post-install boot-readiness verification. Raises on brick.

    Runs inside the sentinel: a failure here leaves the sentinel set so the
    operator is told to resolve boot wiring before the next run.
    """
    findings = list(kernel_safety.verify_boot_artifacts(pkgname, bootloader))
    kver = built_kernel_release(resolve_built_config(pkgbuild_dir))
    if kver:
        findings += kernel_safety.check_dkms_for_kernel(kver)

    bricks = [f for f in findings if f.is_brick]
    for f in findings:
        _log.warn(f"Gate 3 [{f.severity.upper()}] {f.check_id}: {f.message}")
        if f.remediation:
            _log.warn(f"  → {f.remediation}")

    if bricks:
        raise RuntimeError(
            f"[KERNEL] {len(bricks)} boot-readiness problem(s) after install — "
            "the new kernel may not be bootable. Resolve before rebooting "
            "(see findings above)."
        )


# ---------------------------------------------------------------------------
