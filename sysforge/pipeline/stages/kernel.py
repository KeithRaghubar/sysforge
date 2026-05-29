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
  source           = "local"           # "local" | "aur" | "git" — PKGBUILD origin.
                                       # "local" (default) means hand-maintained, no
                                       # remote to sync from. Set to "aur"/"git" if the
                                       # kernel PKGBUILD is a clone of an AUR/git remote.

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
from sysforge.primitives.stage_sentinel import sentinel_scope

_SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED,
})


def _kernel_recovery_command():
    """Canonical shell command to restore boot consistency after an interrupted
    kernel install. Always regenerates the initramfs — that's the step whose
    absence makes the system unbootable. Bootloader regen is omitted because
    the sentinel uses naive ``cmd.split()`` (no shell operators) and most
    bootloaders pick up new kernel images automatically once the initramfs is
    present; the operator can re-run ``sysforge run kernel`` for a full
    bootloader-aware regen.
    """
    return "sudo mkinitcpio -P"

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


_VALID_SOURCES = ("local", "aur", "git")


def _probe_installed_bootloader():
    """Return the set of bootloaders detected as installed on this system.

    Primary signal is filesystem markers (cheap, no subprocess). Falls back
    to ``pacman -Qq`` for the systemd-boot/grub package names when neither
    marker is present.

    Returns a subset of ``{"systemd-boot", "grub"}``. Empty set means
    neither was detected (common in containers / VMs where the right
    answer is ``bootloader = "none"``).
    """
    found = set()
    if Path("/boot/loader/loader.conf").exists():
        found.add("systemd-boot")
    if Path("/boot/grub/grub.cfg").exists():
        found.add("grub")
    if found:
        return found

    try:
        result = subprocess.run(
            ["pacman", "-Qq", "systemd", "grub"],
            capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, OSError):
        return found
    for line in result.stdout.splitlines():
        name = line.strip()
        if name == "systemd" and Path("/usr/bin/bootctl").exists():
            found.add("systemd-boot")
        elif name == "grub":
            found.add("grub")
    return found


def _validate_pkgname_matches_pkgbuild(pkgbuild_path, expected_pkgname):
    """Static-parse the PKGBUILD and confirm its pkgbase matches kernel.toml.

    Catches typos in ``kernel.toml pkgname`` (or a cloned PKGBUILD whose
    pkgbase has drifted from the directory name) before a multi-hour build
    fails late at ``makepkg --install``. Split-package kernels declare
    ``pkgbase`` explicitly; non-split PKGBUILDs use ``pkgname`` as the
    effective pkgbase.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    parsed = parse_pkgbuild(pkgbuild_path)
    globals_ = parsed.get("globals", {})
    parsed_pkgbase = globals_.get("pkgbase")
    if not parsed_pkgbase:
        pkgname_field = globals_.get("pkgname")
        if isinstance(pkgname_field, list):
            parsed_pkgbase = pkgname_field[0] if pkgname_field else None
        else:
            parsed_pkgbase = pkgname_field
    if not parsed_pkgbase:
        # Static parse couldn't recover a pkgbase — let makepkg surface this
        # rather than block on a parse limitation.
        return
    if parsed_pkgbase != expected_pkgname:
        raise RuntimeError(
            f"[KERNEL] kernel.toml pkgname={expected_pkgname!r} does not match "
            f"PKGBUILD pkgbase={parsed_pkgbase!r} at {pkgbuild_path}. "
            "Fix kernel.toml or rename the PKGBUILD directory."
        )


def _resolve_source(kernel_cfg):
    """Resolve the kernel PKGBUILD source classification; defaults to ``local``."""
    src = kernel_cfg.get("source", "local")
    if src not in _VALID_SOURCES:
        raise RuntimeError(
            f"[KERNEL] invalid kernel.toml source {src!r}: "
            f"must be one of {_VALID_SOURCES}"
        )
    return src


def _presync_kernel_source(pkgbuild_dir, options, state_dir, source="local"):
    """
    Sync the kernel source tree through the SourceSyncScheduler.

    Runs whenever --cleansrc/--cleansrc-force is set (forcing a purge even
    when --no-update is also set) or when --no-update was not passed.
    Skipped otherwise. Returns True if a sync was attempted.

    ``source = "local"`` short-circuits the scheduler — there's no remote to
    fetch against. The PKGBUILD must already be present at ``pkgbuild_dir``.
    """
    cleansrc = bool(getattr(options, "cleansrc", False) or getattr(options, "cleansrc_force", False))
    if source == "local" and not cleansrc:
        # Hand-maintained PKGBUILD: nothing to sync. Skip silently.
        return False
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
        source=source,
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
        from sysforge.pipeline.state import get_toolchain_variant, resolve_state_dir
        from sysforge.primitives.build_state import BuildState

        kernel_cfg = _load_kernel_config()
        if kernel_cfg is None or not kernel_cfg.get("enabled", False):
            _log.ui("kernel.toml absent or disabled — stage is a no-op")
            return

        pkgname = kernel_cfg.get("pkgname", "unknown")
        bootloader = _resolve_bootloader(kernel_cfg, options)
        source = _resolve_source(kernel_cfg)

        # Hoisted once: shared by the drift check below, the compiler nudge
        # downstream, and the BuildState.record() variant stamp later in run.
        variant = get_toolchain_variant(state)
        state_dir, _ = resolve_state_dir(options.state_dir)

        # A1: per-kernel toolchain drift. update.py's drift sweep skips
        # stage-owned packages (the kernel is one), so this stage owns the
        # drift signal for its own package. Same shape as update.py:1180-1205.
        recorded_variant = (BuildState(state_dir).get(pkgname) or {}).get("toolchain_variant")
        if recorded_variant and variant != "system" and recorded_variant != variant:
            _log.warn(
                f"Installed {pkgname} was built under toolchain variant "
                f"{recorded_variant!r}; active variant is {variant!r}. "
                "Rebuilding will switch toolchains."
            )

        # A2: surface bootloader mismatch before the build runs, so users on a
        # grub-only system who left bootloader defaulting to systemd-boot get
        # a single early warning instead of a post-install bootctl failure.
        if bootloader != "none":
            installed_bootloaders = _probe_installed_bootloader()
            if installed_bootloaders and bootloader not in installed_bootloaders:
                _log.warn(
                    f"kernel.toml bootloader = {bootloader!r} but installation "
                    f"not detected (found: {sorted(installed_bootloaders) or 'none'}). "
                    "Post-install step will likely fail — pass "
                    "--bootloader=<installed> or update kernel.toml."
                )

        # Interactive kconfig is the kernel-stage default — flipped off by
        # --non-interactive (or `interactive = false` in kernel.toml). When
        # interactive=True is passed into BuildOptions, makepkg_wrapper skips
        # patch_noninteractive_kconfig so the user's PKGBUILD kconfig target
        # (typically `make nconfig`) runs as written, and the makepkg
        # subprocess inherits the parent's stdout/stderr so ncurses-driven
        # kconfig UIs render on the controlling TTY.
        cfg_interactive = bool(kernel_cfg.get("interactive", True))
        interactive = cfg_interactive and not getattr(options, "non_interactive", False)

        # lsmod snapshot — captured before build for localmodconfig reproducibility
        _capture_lsmod_snapshot(state_dir, options.dry_run)

        # kconfig fragment — requires hardware_profile.toml (hardware stage)
        _write_kconfig_fragment(kernel_cfg, config, options.dry_run)

        # Pre-sync the kernel PKGBUILD tree through SourceSyncScheduler. This
        # is the only allowed code path for refreshing sources (CLAUDE.md #3).
        # Running it here (vs. relying on makepkg_wrapper's internal sync)
        # makes --cleansrc work even when --no-update is also set.
        pkgbuild = _pkgbuild_path(kernel_cfg)
        synced = _presync_kernel_source(pkgbuild.parent, options, state_dir, source=source)

        # A3: static-parse the freshly-synced PKGBUILD and confirm pkgbase
        # matches kernel.toml. Catches typos before makepkg --install fails
        # late after a multi-hour build.
        _validate_pkgname_matches_pkgbuild(pkgbuild, pkgname)

        # Compiler resolution: CLI > kernel.toml > pipeline state from toolchain.
        compiler, cc, cxx = _resolve_compiler(kernel_cfg, options, state)
        if compiler:
            _log.ui(f"Kernel compiler override: {compiler}  (cc={cc}  cxx={cxx})")
        elif cc:
            _log.ui(f"Toolchain override from pipeline: cc={cc} cxx={cxx or '-'}")
        else:
            _log.info("No kernel compiler override — profile defaults apply")

        # 3.3: surface the inherited toolchain variant when the user hasn't
        # set `compiler` explicitly. The pipeline-state fallback already
        # routes the right cc/cxx through; this just makes the inheritance
        # visible so the operator can persist it in kernel.toml (and survive
        # a future toolchain-stage disable that clears [stages.toolchain.result]).
        if compiler is None and variant == "pgo_llvm":
            _log.warn(
                "Active toolchain variant: pgo_llvm — kernel will inherit PGO "
                "clang via pipeline-state fallback. Set `compiler = \"llvm\"` "
                "in kernel.toml (or pass --compiler=llvm) to make this explicit "
                "and survive future toolchain-stage disable."
            )
        elif compiler is None and variant == "stock_llvm":
            _log.info(
                "Active toolchain variant: stock_llvm — kernel will inherit "
                "clang via pipeline-state fallback. Set `compiler = \"llvm\"` "
                "in kernel.toml to make this explicit."
            )

        kconfig_target = "make nconfig (user-supplied)" if interactive else "make olddefconfig (patched)"
        _log.info(f"Kernel kconfig target: {kconfig_target}")

        # Install the stage sentinel + interrupt scope around the build and
        # the boot-critical post-install steps. The makepkg --install path,
        # mkinitcpio -P, and the bootloader update are the mutation window
        # whose interruption can leave the system kernel-installed but
        # initramfs-missing → unbootable. Sentinel write-on-entry / clear-
        # on-success / leave-on-exception mirrors the toolchain stage.
        with sentinel_scope(
            options.state_dir,
            "kernel",
            recovery_cmd=_kernel_recovery_command(),
            retry_cmd="sysforge run kernel",
            pkgname=pkgname,
            bootloader=bootloader,
            compiler=compiler or "default",
        ):
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
                    source=source,
                    owner_stage="kernel",
                    toolchain_variant=variant if variant != "system" else None,
                ))

            # Post-install — inside the sentinel so an interrupted
            # mkinitcpio or bootloader regen blocks the next sysforge run.
            _run_mkinitcpio(options.dry_run)
            _update_bootloader(bootloader, options.dry_run)

        _log.ui(f"Kernel stage complete: {pkgname}")
