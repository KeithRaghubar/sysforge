"""
stages/kernel.py — stage 8: kernel build

Builds a custom kernel from a PKGBUILD and runs post-install steps.

kernel.toml is loaded from /etc/sysforge/kernel.toml.

If kernel.toml is absent the stage exits cleanly — systems using a stock
kernel (installed via packages stage) skip this without needing --start-from.

kernel.toml structure:
  pkgname      = "linux-custom"
  pkgbuild_dir = "~/src"           # parent directory that contains the pkgname/ PKGBUILD dir
                                   # PKGBUILD is expected at pkgbuild_dir/pkgname/PKGBUILD
  bootloader   = "systemd-boot"    # systemd-boot | grub | none

  [[kconfig]]                      # manual kconfig overrides (optional)
  option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
  value  = "y"                     # y | m | n | non-empty string (for string/int options)

kconfig fragment:
  Hardware-driven kconfig comes from hardware_profile.toml [kconfig] table
  (emitted by the hardware stage). Manual overrides from kernel.toml [[kconfig]]
  are merged on top — manual wins on conflict with a WARN.

  The combined fragment is written to <pkgbuild_dir>/<pkgname>/sysforge.config
  before makepkg runs. The PKGBUILD must merge this into its .config;
  a compatible PKGBUILD calls scripts/kconfig/merge_config.sh in prepare().

  If neither source provides any kconfig entries, no fragment is written.

Manual override validation:
  - option: must match CONFIG_[A-Z0-9_]+
  - value:  must be y, m, n, or a non-empty string (string/int options)
  - duplicates within kernel.toml: error
  - conflict with hardware_profile kconfig: warn, manual wins

Post-install steps (run after makepkg succeeds):
  1. sudo mkinitcpio -P   (always)
  2. Bootloader update    (configured via bootloader = ...)
"""

import subprocess
import tomllib
from pathlib import Path

import sysforge.log as _log
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.config import CONFIG_BASE
from sysforge.primitives.makepkg_wrapper import run as makepkg_run

KERNEL_PATH = CONFIG_BASE / "etc/sysforge/kernel.toml"


# ---------------------------------------------------------------------------
# kernel.toml loading
# ---------------------------------------------------------------------------


def _load_kernel_config():
    """
    Load kernel.toml. Returns the parsed dict, or None if the file does not
    exist (making the stage a no-op).
    """
    path = KERNEL_PATH

    if not path.exists():
        return None

    with open(path, "rb") as f:
        data = tomllib.load(f)

    _log.ui("[KERNEL]", f"Loaded kernel config from {path}")
    return data


def _pkgbuild_path(kernel_cfg):
    """
    Resolve the PKGBUILD for the configured kernel package.
    Returns Path to the PKGBUILD file.

    Looks for <pkgbuild_dir>/<pkgname>/PKGBUILD.
    pkgbuild_dir is the parent of PKGBUILD source repositories (e.g. ~/src),
    analogous to [paths] pkgbuild_dir in flag_profiles.toml.
    """
    pkgbuild_dir = kernel_cfg.get("pkgbuild_dir")
    if not pkgbuild_dir:
        raise RuntimeError(
            "[KERNEL] kernel.toml is missing pkgbuild_dir. "
            "Set pkgbuild_dir to the directory that contains your kernel PKGBUILD directory "
            '(e.g. pkgbuild_dir = "~/src" if the PKGBUILD is at ~/src/linux-custom/PKGBUILD).'
        )
    srcdir = kernel_cfg.get("srcdir")
    if not srcdir:
        raise RuntimeError("[KERNEL] kernel.toml is missing srcdir.")

    candidate = Path(pkgbuild_dir).expanduser() / srcdir / "PKGBUILD"
    if not candidate.exists():
        raise RuntimeError(
            f"[KERNEL] PKGBUILD not found: {candidate}. "
            f"Clone the kernel PKGBUILD into {Path(pkgbuild_dir).expanduser() / pkgname!r} first."
        )
    return candidate


# ---------------------------------------------------------------------------
# lsmod snapshot
# ---------------------------------------------------------------------------


def _capture_lsmod_snapshot(state_dir, dry_run):
    """
    Capture current lsmod output to <state_dir>/lsmod.snapshot.
    Used by the PKGBUILD's prepare() to run make localmodconfig reproducibly
    on any machine with the same module set, not just the build machine.
    """
    snapshot_path = Path(state_dir) / "lsmod.snapshot"
    if dry_run:
        _log.ui("[KERNEL]", f"[dry-run] would capture lsmod snapshot → {snapshot_path}")
        return

    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    if result.returncode != 0:
        _log.warn(
            "[KERNEL]", f"lsmod failed (exit {result.returncode}) — skipping snapshot"
        )
        return

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(result.stdout)
    _log.ui("[KERNEL]", f"Captured lsmod snapshot: {snapshot_path}")


# ---------------------------------------------------------------------------
# kconfig overrides
# ---------------------------------------------------------------------------

import re as _re

_KCONFIG_OPTION_RE = _re.compile(r"^CONFIG_[A-Z0-9_]+$")


def _validate_manual_kconfig(entries):
    """
    Validate [[kconfig]] entries from kernel.toml.
    Returns a dict {option: value} of validated entries.
    Raises RuntimeError on bad format or duplicate options.
    """
    seen = {}
    for i, entry in enumerate(entries):
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


def _load_hardware_kconfig(config):
    """
    Load [kconfig] table from hardware_profile.toml.
    Returns dict {option: value}, or empty dict if hardware profile is absent.
    hardware_profile.toml is emitted by the hardware stage; its absence is not
    an error here — kconfig entries are simply skipped with an INFO log.
    """
    hw_path = config.get("hardware_profile")
    if not hw_path:
        _log.ui(
            "[KERNEL]",
            "No hardware_profile configured — hardware kconfig entries skipped (hardware stage not run)",
        )
        return {}

    hw_path = Path(hw_path).expanduser()
    if not hw_path.exists():
        _log.ui(
            "[KERNEL]",
            f"hardware_profile.toml not found at {hw_path} — hardware kconfig entries skipped",
        )
        return {}

    with open(hw_path, "rb") as f:
        hw = tomllib.load(f)

    kconfig = hw.get("kconfig", {})
    if kconfig:
        _log.ui(
            "[KERNEL]",
            f"Loaded {len(kconfig)} kconfig entry/entries from hardware_profile.toml",
        )
    return kconfig


def _format_kconfig_line(option, value):
    """Format a single option=value pair as a kernel .config line."""
    if value in ("y", "m"):
        return f"{option}={value}"
    if value == "n":
        return f"# {option} is not set"
    return f'{option}="{value}"'


def _write_kconfig_fragment(kernel_cfg, config, dry_run):
    """
    Build and write the sysforge.config fragment to the PKGBUILD directory.

    Sources (in merge order, later wins):
      1. hardware_profile.toml [kconfig]  — hardware-driven, set by hardware stage
      2. kernel.toml [[kconfig]]          — manual overrides, validated

    Conflicts between the two sources are logged as WARN; manual wins.
    If no entries exist from either source, no fragment is written.

    Returns the fragment path if written, None otherwise.
    """
    # Load and validate both sources
    hw_kconfig = _load_hardware_kconfig(config)
    manual_entries = kernel_cfg.get("kconfig", [])
    manual_kconfig = _validate_manual_kconfig(manual_entries) if manual_entries else {}

    # Detect conflicts
    for option, manual_val in manual_kconfig.items():
        if option in hw_kconfig and hw_kconfig[option] != manual_val:
            _log.warn(
                "[KERNEL]",
                f"kconfig conflict on {option}: hardware_profile={hw_kconfig[option]!r}, "
                f"kernel.toml={manual_val!r} — manual override wins",
            )

    # Merge: hardware base, manual on top
    merged = {**hw_kconfig, **manual_kconfig}

    if not merged:
        _log.ui("[KERNEL]", "No kconfig entries from any source — skipping fragment")
        return None

    pkgbuild = _pkgbuild_path(kernel_cfg)
    fragment_path = pkgbuild.parent / "sysforge.config"

    lines = [
        "# Generated by SysForge — do not edit manually",
        "# Merged into .config by the PKGBUILD's prepare() via merge_config.sh",
        "",
    ]
    hw_count = 0
    manual_count = 0
    for option, value in merged.items():
        source = "manual" if option in manual_kconfig else "hardware"
        lines.append(f"# source: {source}")
        lines.append(_format_kconfig_line(option, value))
        if source == "manual":
            manual_count += 1
        else:
            hw_count += 1

    if dry_run:
        _log.ui(
            "[KERNEL]",
            f"[dry-run] would write kconfig fragment ({hw_count} hardware, {manual_count} manual): {fragment_path}",
        )
        for line in lines:
            _log.ui("[KERNEL]", f"  {line}")
        return None

    fragment_path.write_text("\n".join(lines) + "\n")
    _log.ui(
        "[KERNEL]",
        f"Wrote kconfig fragment: {fragment_path} ({hw_count} hardware, {manual_count} manual)",
    )
    return fragment_path


# ---------------------------------------------------------------------------
# Post-install steps
# ---------------------------------------------------------------------------


def _run_mkinitcpio(dry_run):
    """Regenerate all initramfs presets."""
    if dry_run:
        _log.ui("[KERNEL]", "[dry-run] would run: sudo mkinitcpio -P")
        return
    _log.ui("[KERNEL]", "Running mkinitcpio -P")
    result = subprocess.run(["sudo", "mkinitcpio", "-P"])
    if result.returncode != 0:
        raise RuntimeError(f"[KERNEL] mkinitcpio -P failed (exit {result.returncode})")


def _update_bootloader(bootloader, dry_run):
    """Update the bootloader config to pick up the new kernel."""
    if bootloader == "none" or not bootloader:
        _log.ui("[KERNEL]", "Bootloader update skipped (bootloader = 'none')")
        return

    if bootloader == "grub":
        cmd = ["sudo", "grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
        label = "grub-mkconfig"
    elif bootloader == "systemd-boot":
        cmd = ["sudo", "bootctl", "update"]
        label = "bootctl update"
    else:
        _log.warn("[KERNEL]", f"Unknown bootloader {bootloader!r} — skipping update")
        return

    if dry_run:
        _log.ui("[KERNEL]", f"[dry-run] would run: {' '.join(cmd)}")
        return

    _log.ui("[KERNEL]", f"Updating bootloader: {label}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"[KERNEL] {label} failed (exit {result.returncode})")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class KernelStage(Stage):
    name = "kernel"
    description = "Build and install a custom kernel"
    depends_on = ["packages"]

    def run(self, config, state, options):
        kernel_cfg = _load_kernel_config()
        if kernel_cfg is None or not kernel_cfg.get("enabled", False):
            _log.ui("[KERNEL]", "kernel.toml absent or disabled — stage is a no-op")
            return

        pkgname = kernel_cfg.get("pkgname", "unknown")
        bootloader = kernel_cfg.get("bootloader", "systemd-boot")

        # Resolve state_dir for lsmod snapshot
        from sysforge.pipeline.state import resolve_state_dir

        state_dir, _ = resolve_state_dir(options.state_dir)

        # lsmod snapshot — captured before build for localmodconfig reproducibility
        _capture_lsmod_snapshot(state_dir, options.dry_run)

        # kconfig fragment — requires hardware_profile.toml (hardware stage)
        _write_kconfig_fragment(kernel_cfg, config, options.dry_run)

        # Build
        pkgbuild = _pkgbuild_path(kernel_cfg)

        toolchain = state.get_stage_result("toolchain")
        cc = toolchain.get("cc") if toolchain else None
        cxx = toolchain.get("cxx") if toolchain else None
        if cc:
            _log.ui(
                "[KERNEL]",
                f"Toolchain override from pipeline: cc={cc} cxx={cxx or '-'}",
            )

        if options.dry_run:
            _log.ui("[KERNEL]", f"[dry-run] would build {pkgname} from {pkgbuild}")
        else:
            _log.ui("[KERNEL]", f"Building kernel: {pkgname} from {pkgbuild}")
            makepkg_run(
                pkgbuild,
                pkg_log=not options.no_pkg_logs,
                persist_log=options.persist_log,
                log_dir=options.log_dir,
                update=not options.no_update,
                cc_override=cc,
                cxx_override=cxx,
                abi_check=options.abi_check,
            )

        # Post-install
        _run_mkinitcpio(options.dry_run)
        _update_bootloader(bootloader, options.dry_run)

        _log.ui("[KERNEL]", f"Kernel stage complete: {pkgname}")
