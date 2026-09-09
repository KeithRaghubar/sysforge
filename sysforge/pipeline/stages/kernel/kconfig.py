# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/kconfig.py — authoring the kconfig fragment the PKGBUILD merges.

The stage never edits a kernel .config directly. It composes a *fragment* from
three sources — hardware detection, the running system's loaded modules, and
the user's manual ``[[kconfig]]`` entries — and lets the PKGBUILD's prepare()
overlay it. That division is deliberate: the PKGBUILD stays buildable by hand.

The lsmod snapshot is union-merged across runs rather than replaced, because a
device that was not plugged in during this boot still needs its driver.
"""
from pathlib import Path
import subprocess
import tomllib

from sysforge.pipeline.stages.kernel import config

from sysforge import log

_log = log.get_logger("KERNEL")


def merge_lsmod(prior_text: str, current_text: str) -> str:
    """Union-merge two lsmod outputs by module name (first column).

    Current rows win for modules present in both (fresher Size/Used data);
    prior-only rows are retained — the snapshot grows monotonically so
    ``make localmodconfig`` keeps modules that are only loaded intermittently
    (USB devices, VPN, container netfilter, …). Output stays valid lsmod
    format: header line then one row per module.
    """

    def rows(text):
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("Module"):
            raise ValueError("not lsmod output")
        return {line.split()[0]: line for line in lines[1:] if line.split()}

    current = rows(current_text)
    merged = rows(prior_text)
    merged.update(current)
    header = current_text.splitlines()[0]
    return header + "\n" + "\n".join(merged[name] for name in sorted(merged)) + "\n"


def capture_lsmod_snapshot(state_dir, dry_run):
    """
    Capture current lsmod output to <state_dir>/lsmod.snapshot.
    Used by the PKGBUILD's prepare() to run make localmodconfig reproducibly
    on any machine with the same module set, not just the build machine.

    The snapshot accumulates: each capture is union-merged with the prior
    one by module name, so intermittently-loaded modules (USB devices, VPN,
    container netfilter, …) are never dropped just because they weren't
    loaded during this particular capture. There is no reset flag — delete
    <state_dir>/lsmod.snapshot to start over.
    """
    snapshot_path = Path(state_dir) / "lsmod.snapshot"
    if dry_run:
        _log.ui(f"[dry-run] would capture lsmod snapshot → {snapshot_path}")
        return

    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    if result.returncode != 0:
        _log.warn(
            f"lsmod failed (exit {result.returncode}) — skipping snapshot"
        )
        return

    out = result.stdout
    if snapshot_path.exists():
        try:
            out = merge_lsmod(snapshot_path.read_text(), out)
            _log.info(f"Merged lsmod snapshot (accumulating): {snapshot_path}")
        except (ValueError, UnicodeDecodeError) as e:
            _log.warn(
                f"Existing lsmod snapshot unreadable ({e}) — starting fresh. "
                f"(Delete {snapshot_path} to reset the accumulated module set.)"
            )

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(out)
    _log.info(f"Captured lsmod snapshot: {snapshot_path}")


# ---------------------------------------------------------------------------
# kconfig overrides
# ---------------------------------------------------------------------------

import re as _re

_KCONFIG_OPTION_RE = _re.compile(r"^CONFIG_[A-Z0-9_]+$")


def validate_manual_kconfig(entries):
    """
    Validate [[kconfig]] entries from kernel.toml.
    Returns a dict {option: value} of validated entries.
    Raises RuntimeError on bad format or duplicate options.
    """
    if isinstance(entries, dict):
        # A `[kconfig]` table, not the `[[kconfig]]` array-of-tables this reads.
        # Most often it is the TOML capture trap: a live `[kconfig]` header
        # swallows every top-level key written below it.
        captured = [k for k in entries if not _KCONFIG_OPTION_RE.match(k)]
        detail = (
            f" (top-level key(s) {', '.join(sorted(captured))} were swallowed by the "
            f"table header — move them above it)"
            if captured
            else ""
        )
        raise RuntimeError(
            f"[KERNEL] kernel.toml: kconfig is a table, but manual overrides use the "
            f"[[kconfig]] array-of-tables form "
            f'(e.g. [[kconfig]] / option = "CONFIG_HZ_1000" / value = "y"){detail}'
        )

    seen = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"[KERNEL] kernel.toml [[kconfig]] entry [{i}]: expected a table with "
                f"'option' and 'value' keys, got {type(entry).__name__} {entry!r}"
            )
        option = entry.get("option", "").strip()
        value = str(entry.get("value", "")).strip()

        if not option:
            raise RuntimeError(
                f"[KERNEL] kernel.toml [[kconfig]] entry [{i}] is missing 'option'"
            )
        if not _KCONFIG_OPTION_RE.match(option):
            raise RuntimeError(
                f"[KERNEL] kernel.toml [[kconfig]] entry [{i}]: invalid option {option!r} "
                f"(must match CONFIG_[A-Z0-9_]+)"
            )
        if not value:
            raise RuntimeError(
                f"[KERNEL] kernel.toml [[kconfig]] entry [{i}]: {option} has empty value "
                f"(use 'n' to disable)"
            )
        if option in seen:
            raise RuntimeError(
                f"[KERNEL] kernel.toml [[kconfig]]: duplicate option {option!r}"
            )
        seen[option] = value

    return seen


KCONFIG_UI_TARGETS = ("config", "nconfig", "menuconfig", "xconfig", "gconfig")
KCONFIG_PROMPTING_TARGETS = (
    "oldconfig",
    "localmodconfig",
    "localyesconfig",
    "mod2yesconfig",
)
KCONFIG_SILENT_TARGETS = (
    "olddefconfig",
    "defconfig",
    "allmodconfig",
    "alldefconfig",
    "savedefconfig",
    "listnewconfig",
)
# randconfig deliberately absent: a randomized kernel config on a production
# build path is a boot-safety hazard with no user story.

KCONFIG_ALL_TARGETS = (
    KCONFIG_UI_TARGETS + KCONFIG_PROMPTING_TARGETS + KCONFIG_SILENT_TARGETS
)

KCONFIG_SILENT_EQUIVALENT = {
    "oldconfig": "olddefconfig",
}

# F2: hotplug driver classes re-enabled as modules AFTER config minimization
# (localmodconfig strips drivers for hardware not currently plugged in). Merged
# via a dedicated post-minimization fragment (sysforge.hotplug.config) so the
# minimizer can't strip them back out. Curated — NOT hardware-derived: F2 only
# *adds* modules; boot safety (kernel_safety.py) remains the authority for what
# must be present. Tristate symbols are "m" (loadable modules); the handful of
# `bool` symbols must be "y" — kconfig rejects "m" for a bool and discards the
# whole assignment with `symbol value 'm' invalid for X`, silently losing the
# intent (2.6.1-B17). Check the symbol's type in the kernel tree before adding.
HOTPLUG_KCONFIG = {
    # USB host + gadget
    "CONFIG_USB": "m",
    "CONFIG_USB_XHCI_HCD": "m",
    "CONFIG_USB_EHCI_HCD": "m",
    "CONFIG_USB_OHCI_HCD": "m",
    "CONFIG_USB_STORAGE": "m",
    "CONFIG_USB_UAS": "m",
    "CONFIG_USB_ACM": "m",
    "CONFIG_USB_SERIAL": "m",
    # USB4 / Thunderbolt (CONFIG_THUNDERBOLT was renamed CONFIG_USB4 in 5.6)
    "CONFIG_USB4": "m",
    # MMC / SD
    "CONFIG_MMC": "m",
    "CONFIG_MMC_BLOCK": "m",
    "CONFIG_MMC_SDHCI": "m",
    "CONFIG_MMC_SDHCI_PCI": "m",
    # Hot-plug PCI + CardBus / PCMCIA (HOTPLUG_PCI*, CARDBUS are bool → "y")
    "CONFIG_HOTPLUG_PCI": "y",
    "CONFIG_HOTPLUG_PCI_PCIE": "y",
    "CONFIG_PCCARD": "m",
    "CONFIG_CARDBUS": "y",
    # Hot-plug HID / input
    "CONFIG_HID_GENERIC": "m",
    "CONFIG_USB_HID": "m",
    "CONFIG_HID_MULTITOUCH": "m",
}


def resolve_kconfig_targets(kernel_cfg, *, interactive):
    """
    Validate and reorder the [kernel] kconfig_targets list from kernel.toml.

    Returns None when the key is unset (feature off, zero behavior change).
    Otherwise returns the validated list with any UI target (menuconfig,
    nconfig, xconfig, gconfig, config) moved last, since those must run
    after any non-interactive targets have shaped the .config.

    Raises ValueError on: an unknown target, more than one UI target, or a
    prompting target (oldconfig/localmodconfig/localyesconfig/mod2yesconfig)
    requested when interactive=False.
    """
    targets = kernel_cfg.kconfig_targets
    if not targets:
        return None

    ui_targets = []
    other_targets = []

    for target in targets:
        if target == "randconfig":
            raise ValueError(
                "kconfig_targets: randconfig is not allowed — a randomized "
                "kernel config is a boot-safety hazard"
            )
        if target not in KCONFIG_ALL_TARGETS:
            raise ValueError(
                f"kconfig_targets: unknown target {target!r} — allowed targets "
                f"are {', '.join(KCONFIG_ALL_TARGETS)}"
            )

        if not interactive and target in KCONFIG_PROMPTING_TARGETS:
            if target in KCONFIG_SILENT_EQUIVALENT:
                raise ValueError(
                    f"kconfig_targets: {target!r} requires interactive input — "
                    f"use {KCONFIG_SILENT_EQUIVALENT[target]} instead"
                )
            raise ValueError(
                f"kconfig_targets: {target!r} requires interactive input — "
                f"run the stage interactively"
            )

        if target in KCONFIG_UI_TARGETS:
            ui_targets.append(target)
        else:
            other_targets.append(target)

    if len(ui_targets) > 1:
        raise ValueError(
            f"kconfig_targets: at most one UI target is allowed, got "
            f"{ui_targets}"
        )

    for target in other_targets + ui_targets:
        if target in ("localmodconfig", "localyesconfig"):
            _log.warn(
                f"kconfig_targets: {target} over-minimizes the config to "
                "modules currently loaded on this machine — high risk, low "
                "reward. It accumulates an lsmod snapshot at "
                "<state_dir>/lsmod.snapshot; delete that file to reset it."
            )

    return other_targets + ui_targets


def load_hardware_kconfig(sysforge_config, state_dir=None):
    """
    Load the [kconfig] and [kconfig_devices] tables from hardware_profile.toml.
    Returns (kconfig, device_kconfig) dicts {option: value}; both empty if the
    hardware profile is absent. hardware_profile.toml is emitted by the
    hardware stage; its absence is not an error here — kconfig entries are
    simply skipped with an INFO log.

    The path comes from ``sysforge_config["hardware_profile"]`` (set when the full
    pipeline runs in one process); when the kernel stage runs standalone that
    key is empty, so we fall back to ``state_dir / "hardware_profile.toml"`` —
    where the hardware stage actually writes it. Mirrors the resolution in
    reconfigure.py's hardware-profile review step.
    """
    hw_path = sysforge_config.get("hardware_profile")
    if not hw_path and state_dir is not None:
        hw_path = state_dir / "hardware_profile.toml"
    if not hw_path:
        _log.ui(
            "No hardware_profile configured — hardware kconfig entries skipped "
            "(hardware stage not run)",
        )
        return {}, {}

    hw_path = Path(hw_path).expanduser()
    if not hw_path.exists():
        _log.ui(
            f"hardware_profile.toml not found at {hw_path} — hardware kconfig entries skipped",
        )
        return {}, {}

    with hw_path.open("rb") as f:
        hw = tomllib.load(f)

    kconfig = hw.get("kconfig", {})
    device_kconfig = hw.get("kconfig_devices", {})
    if kconfig or device_kconfig:
        _log.ui(
            f"Loaded {len(kconfig)} hardware + {len(device_kconfig)} device "
            "kconfig entry/entries from hardware_profile.toml",
        )
    return kconfig, device_kconfig


def format_kconfig_line(option, value):
    """Format a single option=value pair as a kernel .config line."""
    if value in ("y", "m"):
        return f"{option}={value}"
    if value == "n":
        return f"# {option} is not set"
    return f'{option}="{value}"'


def write_hotplug_fragment(kernel_cfg, options, dry_run):
    """Write (or remove) the post-minimization hotplug fragment (F2).

    When ``keep_hotplug_drivers`` resolves true, writes ``sysforge.hotplug.config``
    next to the PKGBUILD with the curated ``HOTPLUG_KCONFIG`` set at each
    symbol's kconfig-legal value (``=m`` for tristate, ``=y`` for bool) and
    returns its path. This fragment is merged AFTER the minimization sequence
    (see kconfig_plan.hotplug_merge_step) so ``localmodconfig`` can't
    strip the modules back out.

    When disabled (or dry-run), removes any stale fragment from a prior run so
    "off" means off, and returns ``None``. Mirrors the stale-cleanup contract of
    ``write_kconfig_fragment`` (kconfig_merge = false path).
    """
    fragment_path = config.pkgbuild_path(kernel_cfg).parent / "sysforge.hotplug.config"

    if not config.resolve_keep_hotplug_drivers(kernel_cfg, options):
        if not dry_run and fragment_path.exists():
            try:
                fragment_path.unlink()
                _log.info(f"Removed stale hotplug fragment: {fragment_path}")
            except OSError as exc:
                _log.warn(
                    f"Could not remove stale hotplug fragment {fragment_path}: {exc}"
                )
        return None

    if dry_run:
        _log.ui(
            f"[dry-run] would write hotplug fragment ({len(HOTPLUG_KCONFIG)} "
            "modules re-enabled post-minimization)"
        )
        return None

    lines = [
        "# Generated by SysForge — do not edit manually",
        "# Merged into .config AFTER minimization (localmodconfig) so hotplug",
        "# driver classes stay available as modules. keep_hotplug_drivers = true.",
    ]
    lines += [f"{option}={value}" for option, value in HOTPLUG_KCONFIG.items()]
    fragment_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log.info(
        f"Wrote hotplug fragment ({len(HOTPLUG_KCONFIG)} modules): {fragment_path}"
    )
    return fragment_path


def write_kconfig_fragment(
    kernel_cfg, sysforge_config, dry_run, provenance=None, state_dir=None, extra_kconfig=None
):
    """
    Build and write the sysforge.config fragment to the PKGBUILD directory.

    Sources (in merge order, later wins):
      1. hardware_profile.toml [kconfig_devices] — device-driven (tree-derived
         map ∪ curated table), set by hardware stage; gated by kernel.toml
         ``device_kconfig`` (default true)
      2. hardware_profile.toml [kconfig]         — hardware-driven, set by hardware stage
      3. ``extra_kconfig``                       — feature-driven (e.g. the kernel
         FDO ``CONFIG_AUTOFDO_CLANG``/``CONFIG_PROPELLER_CLANG`` entries supplied
         by the stage for an ``--autofdo`` build); labeled ``fdo`` in the fragment
      4. kernel.toml [[kconfig]]                 — manual overrides, validated

    Manual-vs-hardware conflicts are logged as WARN; manual wins. Device
    entries are machine-derived advisories — a hardware/manual entry overrides
    them silently. If no entries exist from any source, no fragment is written.

    The whole merge is gated by ``kernel.toml kconfig_merge`` (default true);
    set false to disable the fragment entirely (and skip the post-build drift
    check, which keys off the fragment's existence). When disabled, a stale
    ``sysforge.config`` from a prior run is removed so "off" means off — the
    PKGBUILD won't merge a leftover fragment.

    ``provenance`` (optional) is a one-line toolchain trail (e.g.
    "toolchain variant: pgo_llvm  cc: /usr/bin/clang") stamped into the
    fragment header so a ``.config`` diff between two builds carries the
    toolchain identity that produced it.

    Returns ``(path | None, hw_count, manual_count, device_count, fdo_count)`` —
    path is None when no fragment was written (no entries, or dry-run).
    """
    extra_kconfig = extra_kconfig or {}
    # Master gate: kconfig_merge = false disables the fragment outright. Remove
    # any stale fragment so a prior run's sysforge.config isn't merged by the
    # PKGBUILD — "off" must mean off.
    if not kernel_cfg.kconfig_merge:
        _log.info("kconfig_merge = false — skipping kconfig fragment merge")
        if not dry_run:
            stale = config.pkgbuild_path(kernel_cfg).parent / "sysforge.config"
            if stale.exists():
                try:
                    stale.unlink()
                    _log.info(f"Removed stale kconfig fragment: {stale}")
                except OSError as exc:
                    _log.warn(f"Could not remove stale kconfig fragment {stale}: {exc}")
        return None, 0, 0, 0, 0

    # Load and validate the sources
    hw_kconfig, device_kconfig = load_hardware_kconfig(sysforge_config, state_dir)
    if device_kconfig and not kernel_cfg.device_kconfig:
        _log.info(
            f"device_kconfig = false — skipping {len(device_kconfig)} "
            "device-driven kconfig entry/entries",
        )
        device_kconfig = {}
    manual_entries = list(kernel_cfg.manual_kconfig)
    manual_kconfig = validate_manual_kconfig(manual_entries) if manual_entries else {}

    # Detect conflicts (manual wins over hardware and over feature-driven fdo)
    for option, manual_val in manual_kconfig.items():
        if option in hw_kconfig and hw_kconfig[option] != manual_val:
            _log.warn(
                f"kconfig conflict on {option}: hardware_profile={hw_kconfig[option]!r}, "
                f"kernel.toml={manual_val!r} — manual override wins",
            )
        if option in extra_kconfig and extra_kconfig[option] != manual_val:
            _log.warn(
                f"kconfig conflict on {option}: feature(fdo)={extra_kconfig[option]!r}, "
                f"kernel.toml={manual_val!r} — manual override wins (this may "
                "disable the requested optimization)",
            )

    # Merge: device base, hardware, feature(fdo), manual on top
    merged = {**device_kconfig, **hw_kconfig, **extra_kconfig, **manual_kconfig}

    if not merged:
        _log.info("No kconfig entries from any source — skipping fragment")
        return None, 0, 0, 0, 0

    pkgbuild = config.pkgbuild_path(kernel_cfg)
    fragment_path = pkgbuild.parent / "sysforge.config"

    lines = [
        "# Generated by SysForge — do not edit manually",
        "# Merged into .config by the PKGBUILD's prepare() via merge_config.sh",
    ]
    if provenance:
        lines.append(f"# {provenance}")
    lines.append("")
    hw_count = 0
    manual_count = 0
    device_count = 0
    fdo_count = 0
    for option, value in merged.items():
        if option in manual_kconfig:
            source = "manual"
            manual_count += 1
        elif option in extra_kconfig:
            source = "fdo"
            fdo_count += 1
        elif option in hw_kconfig:
            source = "hardware"
            hw_count += 1
        else:
            source = "device"
            device_count += 1
        lines.append(f"# source: {source}")
        lines.append(format_kconfig_line(option, value))

    counts = f"{hw_count} hardware, {device_count} device, {manual_count} manual"
    if fdo_count:
        counts += f", {fdo_count} fdo"
    if dry_run:
        _log.ui(
            f"[dry-run] would write kconfig fragment ({counts}): {fragment_path}",
        )
        for line in lines:
            _log.ui(f"  {line}")
        return None, hw_count, manual_count, device_count, fdo_count

    fragment_path.write_text("\n".join(lines) + "\n")
    _log.ui(
        f"Wrote kconfig fragment: {fragment_path} ({counts})",
    )
    return fragment_path, hw_count, manual_count, device_count, fdo_count


def resolve_base_config(kernel_cfg, options=None):
    """Resolve the base ``.config`` text to seed before the fragment merge.

    The base config selects where the build's *starting* ``.config`` comes from
    (the ``sysforge.config`` fragment is overlaid on top of it). Resolution
    order: ``--base-config`` CLI flag (``options.base_config``) > ``kernel.toml
    base_config`` > the ``"pkgbuild"`` default. The resolved value is one of:

      * ``"pkgbuild"`` (default) — no seeding; the PKGBUILD provides its own base.
      * ``"running"``            — the running kernel's config (``/proc/config.gz``
                                   then ``/boot/config-$(uname -r)``).
      * ``<path>``               — read the base config from that file.

    Returns ``(source_label, text)`` where ``text`` is ``None`` for the
    ``"pkgbuild"`` default or when a ``"running"`` source is unavailable (warned).
    Unknown non-path values raise.
    """
    cli = getattr(options, "base_config", None)
    raw = cli or kernel_cfg.base_config
    src = "--base-config" if cli else "kernel.toml base_config"
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(
            f"[KERNEL] invalid {src} {raw!r}: expected "
            '"pkgbuild", "running", or a path to a kernel .config file'
        )
    if raw == "pkgbuild":
        return raw, None
    if raw == "running":
        from sysforge.primitives.dep_analysis import read_running_kconfig_text

        text = read_running_kconfig_text()
        if text is None:
            _log.warn(
                'base_config = "running" but no running-kernel config found '
                "(/proc/config.gz or /boot/config-$(uname -r)); falling back to "
                "the PKGBUILD's own base config"
            )
        return raw, text
    # Anything else is treated as a path to a .config file.
    path = Path(raw).expanduser()
    if not path.is_file():
        raise RuntimeError(
            f"[KERNEL] {src} path does not exist: {path}"
        )
    return raw, path.read_text()


def write_base_config(kernel_cfg, dry_run, options=None):
    """Resolve ``base_config`` and, when not ``"pkgbuild"``, write the chosen base
    ``.config`` to ``<pkgbuild_dir>/sysforge.base.config``.

    Mirrors the ``sysforge.config`` fragment contract: sysforge writes the file,
    the PKGBUILD's ``prepare()`` copies ``sysforge.base.config`` to ``.config``
    (then runs ``make olddefconfig``) *before* merging ``sysforge.config``. This
    keeps sysforge from mutating tracked source files. Returns the source label
    for the resolution summary. ``options`` carries the ``--base-config`` CLI
    override (see ``resolve_base_config``).
    """
    source_label, text = resolve_base_config(kernel_cfg, options)
    if text is None:
        return source_label
    pkgbuild = config.pkgbuild_path(kernel_cfg)
    base_path = pkgbuild.parent / "sysforge.base.config"
    if dry_run:
        _log.ui(f"[dry-run] would write base kernel config ({source_label}): {base_path}")
        return source_label
    base_path.write_text(text if text.endswith("\n") else text + "\n")
    _log.info(f"Wrote base kernel config ({source_label}): {base_path}")
    return source_label


# ---------------------------------------------------------------------------
