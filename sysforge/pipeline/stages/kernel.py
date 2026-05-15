"""
stages/kernel.py — stage 8: kernel build

Builds a custom kernel from a PKGBUILD and runs post-install steps.

kernel.toml is loaded from /etc/sysforge/kernel.toml.

If kernel.toml is absent the stage exits cleanly — systems using a stock
kernel (installed via packages stage) skip this without needing --start-from.

kernel.toml structure:
  pkgname          = "linux-custom"
  pkgbuild_src_dir = "~/src"       # parent directory that contains the pkgname/ PKGBUILD dir
                                   # PKGBUILD is expected at pkgbuild_src_dir/pkgname/PKGBUILD
  bootloader       = "systemd-boot"    # systemd-boot | grub | none

  [[kconfig]]                      # manual kconfig overrides (optional)
  option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
  value  = "y"                     # y | m | n | non-empty string (for string/int options)

kconfig fragment:
  Hardware-driven kconfig comes from hardware_profile.toml [kconfig] table
  (emitted by the hardware stage). Manual overrides from kernel.toml [[kconfig]]
  are merged on top — manual wins on conflict with a WARN.

  The combined fragment is written to <pkgbuild_src_dir>/<pkgname>/sysforge.config
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

from sysforge import log
_log = log.get_logger("KERNEL")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.paths import KERNEL_PATH
from sysforge.primitives.makepkg_wrapper import run as makepkg_run, BuildOptions
from sysforge.primitives.source_sync import (
    STATUS_DIVERGED,
    STATUS_FAILED,
    STATUS_PURGE_REFUSED,
    STATUS_RATE_LIMITED,
    SyncRequest,
    get_scheduler,
)

_SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED,
})

_VALID_COMPILERS = ("gcc", "llvm")
_VALID_BOOTLOADERS = ("systemd-boot", "grub", "none")


def _resolve_compiler(kernel_cfg, options, state):
    """
    Resolve the kernel-stage compiler. Precedence:
      1. options.compiler (CLI --compiler)
      2. kernel_cfg["compiler"]
      3. pipeline state's toolchain.cc (mapped back to "gcc" or "llvm")
      4. None (let makepkg_wrapper fall through to profile defaults)

    Returns ("gcc"|"llvm"|None, cc_path|None, cxx_path|None).
    """
    cli = getattr(options, "compiler", None)
    cfg = kernel_cfg.get("compiler")
    for source, val in (("--compiler", cli), ("kernel.toml", cfg)):
        if val and val not in _VALID_COMPILERS:
            raise RuntimeError(
                f"[KERNEL] invalid {source} value {val!r}: "
                f"must be one of {_VALID_COMPILERS}"
            )

    compiler = cli or cfg
    if compiler:
        from sysforge.pipeline.stages.toolchain import _compiler_paths
        cc, cxx, _ = _compiler_paths(compiler)
        return compiler, cc, cxx

    toolchain = state.get_stage_result("toolchain") if state else None
    cc = toolchain.get("cc") if toolchain else None
    cxx = toolchain.get("cxx") if toolchain else None
    return None, cc, cxx


def _resolve_bootloader(kernel_cfg, options):
    """Resolve bootloader: CLI override > kernel.toml > 'systemd-boot' default."""
    cli = getattr(options, "bootloader", None)
    if cli and cli not in _VALID_BOOTLOADERS:
        raise RuntimeError(
            f"[KERNEL] invalid --bootloader value {cli!r}: "
            f"must be one of {_VALID_BOOTLOADERS}"
        )
    cfg = kernel_cfg.get("bootloader", "systemd-boot")
    if cfg not in _VALID_BOOTLOADERS:
        raise RuntimeError(
            f"[KERNEL] invalid kernel.toml bootloader {cfg!r}: "
            f"must be one of {_VALID_BOOTLOADERS}"
        )
    return cli or cfg


def _presync_kernel_source(pkgbuild_dir, options, state_dir):
    """
    Sync the kernel source tree through the SourceSyncScheduler.

    Runs whenever --cleansrc/--cleansrc-force is set (forcing a purge even
    when --no-update is also set) or when --no-update was not passed.
    Skipped otherwise. Returns True if a sync was attempted.
    """
    cleansrc = bool(getattr(options, "cleansrc", False) or getattr(options, "cleansrc_force", False))
    if not cleansrc and getattr(options, "no_update", False):
        _log.info("--no-update: skipping kernel source sync")
        return False

    if getattr(options, "dry_run", False):
        kind = "purge + re-clone" if cleansrc else "git fetch + rebase"
        _log.ui(f"[dry-run] would sync kernel source ({kind}): {pkgbuild_dir}")
        return False

    scheduler = get_scheduler(
        state_dir=state_dir,
        cleansrc=cleansrc,
        cleansrc_force=bool(getattr(options, "cleansrc_force", False)),
    )
    result = scheduler.request(SyncRequest(
        pkgbase=pkgbuild_dir.name,
        pkgbuild_dir=pkgbuild_dir,
        force_fetch=True,
    ))
    if result.status in _SYNC_BLOCKING_STATUSES:
        raise RuntimeError(
            f"[KERNEL] source sync failed for {pkgbuild_dir.name}: "
            f"{result.error or result.status}"
        )
    if result.status == STATUS_DIVERGED:
        _log.warn(
            f"{pkgbuild_dir.name}: upstream diverged — building with local PKGBUILD "
            "(rerun with --cleansrc to discard local edits)"
        )
    return True


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

    _log.ui(f"Loaded kernel config from {path}")
    return data


def _pkgbuild_path(kernel_cfg):
    """
    Resolve the PKGBUILD for the configured kernel package.
    Returns Path to the PKGBUILD file.

    Looks for <pkgbuild_src_dir>/<srcdir>/PKGBUILD where srcdir defaults to pkgname
    when not specified. srcdir allows the source directory name to differ from
    pkgname (e.g. pkgname="linux-custom", srcdir="linux").
    """
    pkgbuild_src_dir = kernel_cfg.get("pkgbuild_src_dir")
    if not pkgbuild_src_dir:
        raise RuntimeError(
            "[KERNEL] kernel.toml is missing pkgbuild_src_dir. "
            "Set pkgbuild_src_dir to the directory that contains your kernel PKGBUILD directory "
            '(e.g. pkgbuild_src_dir = "~/src" if the PKGBUILD is at ~/src/linux-custom/PKGBUILD).'
        )
    pkgname = kernel_cfg.get("pkgname")
    if not pkgname:
        raise RuntimeError("[KERNEL] kernel.toml is missing pkgname.")

    srcdir = kernel_cfg.get("srcdir") or pkgname

    candidate = Path(pkgbuild_src_dir).expanduser() / srcdir / "PKGBUILD"
    if not candidate.exists():
        raise RuntimeError(
            f"[KERNEL] PKGBUILD not found: {candidate}. "
            f"Clone the kernel PKGBUILD into {Path(pkgbuild_src_dir).expanduser() / srcdir!r} first."
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
        _log.ui(f"[dry-run] would capture lsmod snapshot → {snapshot_path}")
        return

    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    if result.returncode != 0:
        _log.warn(
            f"lsmod failed (exit {result.returncode}) — skipping snapshot"
        )
        return

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(result.stdout)
    _log.ui(f"Captured lsmod snapshot: {snapshot_path}")


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
            "No hardware_profile configured — hardware kconfig entries skipped (hardware stage not run)",
        )
        return {}

    hw_path = Path(hw_path).expanduser()
    if not hw_path.exists():
        _log.ui(
            f"hardware_profile.toml not found at {hw_path} — hardware kconfig entries skipped",
        )
        return {}

    with open(hw_path, "rb") as f:
        hw = tomllib.load(f)

    kconfig = hw.get("kconfig", {})
    if kconfig:
        _log.ui(
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
                f"kconfig conflict on {option}: hardware_profile={hw_kconfig[option]!r}, "
                f"kernel.toml={manual_val!r} — manual override wins",
            )

    # Merge: hardware base, manual on top
    merged = {**hw_kconfig, **manual_kconfig}

    if not merged:
        _log.ui("No kconfig entries from any source — skipping fragment")
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
            f"[dry-run] would write kconfig fragment ({hw_count} hardware, {manual_count} manual): {fragment_path}",
        )
        for line in lines:
            _log.ui(f"  {line}")
        return None

    fragment_path.write_text("\n".join(lines) + "\n")
    _log.ui(
        f"Wrote kconfig fragment: {fragment_path} ({hw_count} hardware, {manual_count} manual)",
    )
    return fragment_path


# ---------------------------------------------------------------------------
# Post-install steps
# ---------------------------------------------------------------------------


def _run_mkinitcpio(dry_run):
    """Regenerate all initramfs presets."""
    if dry_run:
        _log.ui("[dry-run] would run: sudo mkinitcpio -P")
        return
    _log.ui("Running mkinitcpio -P")
    result = subprocess.run(["sudo", "mkinitcpio", "-P"])
    if result.returncode != 0:
        raise RuntimeError(f"[KERNEL] mkinitcpio -P failed (exit {result.returncode})")


def _update_bootloader(bootloader, dry_run):
    """Update the bootloader config to pick up the new kernel."""
    if bootloader == "none" or not bootloader:
        _log.ui("Bootloader update skipped (bootloader = 'none')")
        return

    if bootloader == "grub":
        cmd = ["sudo", "grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
        label = "grub-mkconfig"
    elif bootloader == "systemd-boot":
        cmd = ["sudo", "bootctl", "update"]
        label = "bootctl update"
    else:
        _log.warn(f"Unknown bootloader {bootloader!r} — skipping update")
        return

    if dry_run:
        _log.ui(f"[dry-run] would run: {' '.join(cmd)}")
        return

    _log.ui(f"Updating bootloader: {label}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _log.warn(
            f"{label} exited {result.returncode} — bootloader may already be current; "
            "kernel is installed, continuing",
        )


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
            _log.ui("kernel.toml absent or disabled — stage is a no-op")
            return

        pkgname = kernel_cfg.get("pkgname", "unknown")
        bootloader = _resolve_bootloader(kernel_cfg, options)

        # Interactive kconfig is the kernel-stage default — flipped off by
        # --non-interactive (or `interactive = false` in kernel.toml). When
        # interactive=True is passed into BuildOptions, makepkg_wrapper skips
        # patch_noninteractive_kconfig so the user's PKGBUILD kconfig target
        # (typically `make nconfig`) runs as written.
        cfg_interactive = bool(kernel_cfg.get("interactive", True))
        interactive = cfg_interactive and not getattr(options, "non_interactive", False)

        # Resolve state_dir for lsmod snapshot and scheduler init.
        from sysforge.pipeline.state import resolve_state_dir

        state_dir, _ = resolve_state_dir(options.state_dir)

        # lsmod snapshot — captured before build for localmodconfig reproducibility
        _capture_lsmod_snapshot(state_dir, options.dry_run)

        # kconfig fragment — requires hardware_profile.toml (hardware stage)
        _write_kconfig_fragment(kernel_cfg, config, options.dry_run)

        # Pre-sync the kernel PKGBUILD tree through SourceSyncScheduler. This
        # is the only allowed code path for refreshing sources (CLAUDE.md #3).
        # Running it here (vs. relying on makepkg_wrapper's internal sync)
        # makes --cleansrc work even when --no-update is also set.
        pkgbuild = _pkgbuild_path(kernel_cfg)
        synced = _presync_kernel_source(pkgbuild.parent, options, state_dir)

        # Compiler resolution: CLI > kernel.toml > pipeline state from toolchain.
        compiler, cc, cxx = _resolve_compiler(kernel_cfg, options, state)
        if compiler:
            _log.ui(f"Kernel compiler override: {compiler}  (cc={cc}  cxx={cxx})")
        elif cc:
            _log.ui(f"Toolchain override from pipeline: cc={cc} cxx={cxx or '-'}")
        else:
            _log.info("No kernel compiler override — profile defaults apply")

        kconfig_target = "make nconfig (user-supplied)" if interactive else "make olddefconfig (patched)"
        _log.info(f"Kernel kconfig target: {kconfig_target}")

        if options.dry_run:
            _log.ui(f"[dry-run] would build {pkgname} from {pkgbuild}")
        else:
            _log.ui(f"Building kernel: {pkgname} from {pkgbuild}")
            makepkg_run(pkgbuild, options=BuildOptions(
                pkg_log=not options.no_pkg_logs,
                persist_log=options.persist_log,
                log_dir=options.log_dir,
                profile_conf=getattr(options, "profile_conf", None) or config.get("profile_conf"),
                update=(not options.no_update) and not synced,
                interactive=interactive,
                cc_override=cc,
                cxx_override=cxx,
                abi_check=options.abi_check,
                state_dir=options.state_dir,
            ))

        # Post-install
        _run_mkinitcpio(options.dry_run)
        _update_bootloader(bootloader, options.dry_run)

        _log.ui(f"Kernel stage complete: {pkgname}")
