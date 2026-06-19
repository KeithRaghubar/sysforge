# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/kernel.py — stage 8: kernel build

Builds a custom kernel from a PKGBUILD and runs post-install steps.

kernel.toml is loaded from /etc/sysforge/kernel.toml.

If kernel.toml is absent the stage exits cleanly — systems using a stock
kernel (installed via packages stage) skip this without needing --start-from.

kernel.toml structure:
  pkgname          = "linux-sysforge"
  pkgbuild_src_dir = "~/src"       # parent directory that contains the pkgname/ PKGBUILD dir
                                   # PKGBUILD is expected at pkgbuild_src_dir/pkgname/PKGBUILD
  bootloader       = "systemd-boot"    # systemd-boot | grub | none
  source           = "local"           # "local" | "aur" | "git" — PKGBUILD origin.
                                       # "local" (default) means hand-maintained, no
                                       # remote to sync from. Set to "aur"/"git" if the
                                       # kernel PKGBUILD is a clone of an AUR/git remote.

  device_kconfig   = true          # merge device-driven [kconfig_devices]
                                   # entries from hardware_profile.toml into
                                   # the fragment (default true)

  [[kconfig]]                      # manual kconfig overrides (optional)
  option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
  value  = "y"                     # y | m | n | non-empty string (for string/int options)

kconfig fragment:
  Hardware-driven kconfig comes from hardware_profile.toml [kconfig] table,
  and device-driven entries from its [kconfig_devices] table (both emitted by
  the hardware stage; the latter gated by device_kconfig above). Manual
  overrides from kernel.toml [[kconfig]] are merged on top — precedence is
  manual > hardware > device; manual wins on conflict with a WARN.

  Gate 2 harvests the just-built tree's kbuild module→CONFIG_* map (the
  resolved .config's parent is the version-exact source tree) into
  <state_dir>/kbuild_module_map.json — the hardware stage loads that cache to
  widen [kconfig_devices] beyond device_probe's curated table on later runs.

  The combined fragment is written to <pkgbuild_src_dir>/<pkgname>/sysforge.config
  before makepkg runs. The PKGBUILD must merge this into its .config;
  a compatible PKGBUILD calls scripts/kconfig/merge_config.sh in prepare().

  If neither source provides any kconfig entries, no fragment is written.

base_config (optional, default "pkgbuild"):
  Selects the build's *starting* .config, before the fragment is overlaid:
  "pkgbuild" (the PKGBUILD's own base, no seeding), "running" (the running
  kernel's config from /proc/config.gz or /boot/config-$(uname -r)), or a path.
  For "running"/<path>, the chosen config is written to
  <pkgbuild_src_dir>/<pkgname>/sysforge.base.config; the PKGBUILD's prepare()
  must copy it to .config (then `make olddefconfig`) before merging
  sysforge.config — the same cooperation contract as the fragment.

Manual override validation:
  - option: must match CONFIG_[A-Z0-9_]+
  - value:  must be y, m, n, or a non-empty string (string/int options)
  - duplicates within kernel.toml: error
  - conflict with hardware_profile kconfig: warn, manual wins

Post-install steps (run after makepkg succeeds):
  1. sudo mkinitcpio -P   (always)
  2. Bootloader update    (configured via bootloader = ...)
"""

import contextlib
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("KERNEL")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives import device_probe, kbuild_map, kernel_safety
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.paths import KERNEL_PATH
from sysforge.primitives.makepkg_wrapper import (
    AlreadyBuilt,
    install_built_packages,
    run as makepkg_run,
)
from sysforge.build_core import make_build_options
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


def _check_pkgname_repo_collision(pkgname, options):
    """Warn + confirm when the kernel pkgname shadows a pacman sync-repo package.

    A custom kernel should carry a unique name. If ``pkgname`` matches a package
    in a sync DB (e.g. ``linux``, ``linux-lts``), building and installing it
    produces a package that overwrites the official one on ``pacman -U`` — almost
    never intended. Interactive runs confirm the override; unattended runs abort
    (the safe default); ``--dry-run`` warns without prompting.
    """
    from sysforge.primitives.aur import is_repo_package
    from sysforge.primitives.prompt import is_interactive, prompt_choice

    if not is_repo_package(pkgname):
        return
    msg = (
        f"kernel pkgname {pkgname!r} matches an existing package in a pacman sync "
        "repo — building it will overwrite the official package on install."
    )
    if getattr(options, "dry_run", False):
        _log.warn(f"{msg} (dry-run: not prompting)")
        return
    _log.warn(msg)
    unattended = bool(getattr(options, "non_interactive", False)) or not is_interactive()
    if unattended:
        raise RuntimeError(
            f"[KERNEL] {msg} Aborting unattended — rename pkgname in kernel.toml, "
            "or run interactively to confirm the override."
        )
    choice = prompt_choice(
        "Continue and shadow the repo package? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        retry_on_invalid=False,
        tag="KERNEL",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            f"[KERNEL] kernel build aborted — pkgname {pkgname!r} collides with a "
            "repo package and the override was not confirmed. Rename pkgname in "
            "kernel.toml."
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
        _warn_and_confirm_diverged(pkgbuild_dir, options)
    return True


def _warn_and_confirm_diverged(pkgbuild_dir, options):
    """Warn (with commit detail) that the kernel source diverged, then gate the build.

    ``STATUS_DIVERGED`` means the local tree can't fast-forward to upstream —
    either local commits/uncommitted edits, or upstream advanced while the tree
    is dirty. Building a *kernel* off stale or hand-edited source is exactly the
    case where the old silent WARN was too easy to miss, so:

      * interactive run  → require an explicit y/N confirmation;
      * unattended run (``--non-interactive`` or no TTY) → abort (safe default).

    ``classify_head_vs_upstream`` enriches the message with ahead/behind counts
    so the common "upstream has new commits but the local repo is dirty" case is
    spelled out rather than hidden behind a bare "diverged".
    """
    from sysforge.primitives.aur import classify_head_vs_upstream
    from sysforge.primitives.prompt import is_interactive, prompt_choice

    state, n_local, n_upstream = classify_head_vs_upstream(pkgbuild_dir)
    detail = ""
    if n_upstream:
        detail += f" upstream advanced {n_upstream} commit(s);"
    if n_local:
        detail += f" local tree is {n_local} commit(s) ahead;"
    _log.warn(
        f"{pkgbuild_dir.name}: kernel source diverged ({state}){detail} the local "
        "PKGBUILD will be used as-is. Rerun with --cleansrc to discard local "
        "edits and rebuild from upstream."
    )

    unattended = bool(getattr(options, "non_interactive", False)) or not is_interactive()
    if unattended:
        raise RuntimeError(
            f"[KERNEL] {pkgbuild_dir.name}: refusing to build a kernel from "
            "diverged source unattended. Resolve the divergence, rerun with "
            "--cleansrc to discard local edits, or run interactively to confirm."
        )
    choice = prompt_choice(
        "Continue building the kernel from local (diverged) source? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        retry_on_invalid=False,
        tag="KERNEL",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            f"[KERNEL] {pkgbuild_dir.name}: build aborted — diverged source not "
            "confirmed. Rerun with --cleansrc to discard local edits."
        )


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
    # KernelStage.run() stamps the effective value (kernel.toml override, else
    # the global [paths] pkgbuild_src_dir) into kernel_cfg before this is called,
    # so an empty value here means neither source is set.
    pkgbuild_src_dir = kernel_cfg.get("pkgbuild_src_dir")
    if not pkgbuild_src_dir:
        raise RuntimeError(
            "[KERNEL] no pkgbuild_src_dir configured. Set [paths] pkgbuild_src_dir "
            "in profiles.toml (the global default) or pkgbuild_src_dir in kernel.toml "
            "(per-kernel override) to the directory that contains your kernel PKGBUILD "
            'directory (e.g. "~/src" if the PKGBUILD is at ~/src/linux-custom/PKGBUILD).'
        )
    pkgname = kernel_cfg.get("pkgname")
    if not pkgname:
        raise RuntimeError("[KERNEL] kernel.toml is missing pkgname.")

    srcdir = kernel_cfg.get("srcdir") or pkgname

    srcdir_path = Path(pkgbuild_src_dir).expanduser() / srcdir
    candidate = srcdir_path / "PKGBUILD"
    if not candidate.exists():
        if srcdir_path.is_dir():
            # Directory exists but the PKGBUILD is gone — the signature of an
            # interrupted `--cleansrc` purge (purge_src rmtree's the tree, then
            # the scheduler re-clones; an interruption in between leaves this
            # half-removed state). Recoverable: re-run with --cleansrc to
            # re-clone. See DESIGN.md §Kernel stage source sync.
            raise RuntimeError(
                f"[KERNEL] PKGBUILD missing from existing dir {srcdir_path} — "
                "an interrupted --cleansrc may have left a partial tree. "
                "Re-run with --cleansrc to re-clone, or restore the PKGBUILD."
            )
        raise RuntimeError(
            f"[KERNEL] PKGBUILD not found: {candidate}. "
            f"Clone the kernel PKGBUILD into {srcdir_path!r} first."
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


def _load_hardware_kconfig(config, state_dir=None):
    """
    Load the [kconfig] and [kconfig_devices] tables from hardware_profile.toml.
    Returns (kconfig, device_kconfig) dicts {option: value}; both empty if the
    hardware profile is absent. hardware_profile.toml is emitted by the
    hardware stage; its absence is not an error here — kconfig entries are
    simply skipped with an INFO log.

    The path comes from ``config["hardware_profile"]`` (set when the full
    pipeline runs in one process); when the kernel stage runs standalone that
    key is empty, so we fall back to ``state_dir / "hardware_profile.toml"`` —
    where the hardware stage actually writes it. Mirrors the resolution in
    reconfigure.py's hardware-profile review step.
    """
    hw_path = config.get("hardware_profile")
    if not hw_path and state_dir is not None:
        hw_path = state_dir / "hardware_profile.toml"
    if not hw_path:
        _log.ui(
            "No hardware_profile configured — hardware kconfig entries skipped (hardware stage not run)",
        )
        return {}, {}

    hw_path = Path(hw_path).expanduser()
    if not hw_path.exists():
        _log.ui(
            f"hardware_profile.toml not found at {hw_path} — hardware kconfig entries skipped",
        )
        return {}, {}

    with open(hw_path, "rb") as f:
        hw = tomllib.load(f)

    kconfig = hw.get("kconfig", {})
    device_kconfig = hw.get("kconfig_devices", {})
    if kconfig or device_kconfig:
        _log.ui(
            f"Loaded {len(kconfig)} hardware + {len(device_kconfig)} device "
            "kconfig entry/entries from hardware_profile.toml",
        )
    return kconfig, device_kconfig


def _format_kconfig_line(option, value):
    """Format a single option=value pair as a kernel .config line."""
    if value in ("y", "m"):
        return f"{option}={value}"
    if value == "n":
        return f"# {option} is not set"
    return f'{option}="{value}"'


def _write_kconfig_fragment(kernel_cfg, config, dry_run, provenance=None, state_dir=None):
    """
    Build and write the sysforge.config fragment to the PKGBUILD directory.

    Sources (in merge order, later wins):
      1. hardware_profile.toml [kconfig_devices] — device-driven (tree-derived
         map ∪ curated table), set by hardware stage; gated by kernel.toml
         ``device_kconfig`` (default true)
      2. hardware_profile.toml [kconfig]         — hardware-driven, set by hardware stage
      3. kernel.toml [[kconfig]]                 — manual overrides, validated

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

    Returns ``(path | None, hw_count, manual_count, device_count)`` — path is
    None when no fragment was written (no entries, or dry-run).
    """
    # Master gate: kconfig_merge = false disables the fragment outright. Remove
    # any stale fragment so a prior run's sysforge.config isn't merged by the
    # PKGBUILD — "off" must mean off.
    if not bool(kernel_cfg.get("kconfig_merge", True)):
        _log.info("kconfig_merge = false — skipping kconfig fragment merge")
        if not dry_run:
            stale = _pkgbuild_path(kernel_cfg).parent / "sysforge.config"
            if stale.exists():
                try:
                    stale.unlink()
                    _log.info(f"Removed stale kconfig fragment: {stale}")
                except OSError as exc:
                    _log.warn(f"Could not remove stale kconfig fragment {stale}: {exc}")
        return None, 0, 0, 0

    # Load and validate the sources
    hw_kconfig, device_kconfig = _load_hardware_kconfig(config, state_dir)
    if device_kconfig and not bool(kernel_cfg.get("device_kconfig", True)):
        _log.info(
            f"device_kconfig = false — skipping {len(device_kconfig)} "
            "device-driven kconfig entry/entries",
        )
        device_kconfig = {}
    manual_entries = kernel_cfg.get("kconfig", [])
    manual_kconfig = _validate_manual_kconfig(manual_entries) if manual_entries else {}

    # Detect conflicts
    for option, manual_val in manual_kconfig.items():
        if option in hw_kconfig and hw_kconfig[option] != manual_val:
            _log.warn(
                f"kconfig conflict on {option}: hardware_profile={hw_kconfig[option]!r}, "
                f"kernel.toml={manual_val!r} — manual override wins",
            )

    # Merge: device base, hardware above it, manual on top
    merged = {**device_kconfig, **hw_kconfig, **manual_kconfig}

    if not merged:
        _log.ui("No kconfig entries from any source — skipping fragment")
        return None, 0, 0, 0

    pkgbuild = _pkgbuild_path(kernel_cfg)
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
    for option, value in merged.items():
        if option in manual_kconfig:
            source = "manual"
            manual_count += 1
        elif option in hw_kconfig:
            source = "hardware"
            hw_count += 1
        else:
            source = "device"
            device_count += 1
        lines.append(f"# source: {source}")
        lines.append(_format_kconfig_line(option, value))

    counts = f"{hw_count} hardware, {device_count} device, {manual_count} manual"
    if dry_run:
        _log.ui(
            f"[dry-run] would write kconfig fragment ({counts}): {fragment_path}",
        )
        for line in lines:
            _log.ui(f"  {line}")
        return None, hw_count, manual_count, device_count

    fragment_path.write_text("\n".join(lines) + "\n")
    _log.ui(
        f"Wrote kconfig fragment: {fragment_path} ({counts})",
    )
    return fragment_path, hw_count, manual_count, device_count


def _resolve_base_config(kernel_cfg, options=None):
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
    raw = cli or kernel_cfg.get("base_config", "pkgbuild")
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


def _write_base_config(kernel_cfg, dry_run, options=None):
    """Resolve ``base_config`` and, when not ``"pkgbuild"``, write the chosen base
    ``.config`` to ``<pkgbuild_dir>/sysforge.base.config``.

    Mirrors the ``sysforge.config`` fragment contract: sysforge writes the file,
    the PKGBUILD's ``prepare()`` copies ``sysforge.base.config`` to ``.config``
    (then runs ``make olddefconfig``) *before* merging ``sysforge.config``. This
    keeps sysforge from mutating tracked source files. Returns the source label
    for the resolution summary. ``options`` carries the ``--base-config`` CLI
    override (see ``_resolve_base_config``).
    """
    source_label, text = _resolve_base_config(kernel_cfg, options)
    if text is None:
        return source_label
    pkgbuild = _pkgbuild_path(kernel_cfg)
    base_path = pkgbuild.parent / "sysforge.base.config"
    if dry_run:
        _log.ui(f"[dry-run] would write base kernel config ({source_label}): {base_path}")
        return source_label
    base_path.write_text(text if text.endswith("\n") else text + "\n")
    _log.ui(f"Wrote base kernel config ({source_label}): {base_path}")
    return source_label


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
# Boot-safety gates
#
# The kernel stage must never leave the machine unbootable. Three gates wrap
# the build/install: a cheap pre-build preflight (Gate 1), a resolved-.config
# audit between build and install (Gate 2 — the only placement that catches a
# Kconfig dependency cascade like CONFIG_SND_PCI=n hiding CONFIG_SND_HDA_INTEL),
# and a post-install boot-readiness check (Gate 3). Brick-class findings
# hard-fail; everything else warns. See DESIGN.md §Kernel stage boot-safety.
# ---------------------------------------------------------------------------


def _gate1_preflight(kernel_cfg, options, pkgname, *, dry_run):
    """Cheap pre-build safety checks. Returns the RootTopology for Gate 2.

    Hard-fails (RuntimeError) on brick conditions — no fallback kernel (E1),
    or a missing/too-full /boot (D5) — unless overridden. In dry-run these are
    downgraded to warnings so the run can still preview. The remaining checks
    (localmodconfig strip warning, DKMS rebuild reminder, mkinitcpio HOOKS vs
    root topology) are advisory.
    """
    require_fallback = bool(kernel_cfg.get("require_fallback_kernel", True))
    boot_audit = bool(kernel_cfg.get("boot_audit", True))
    min_free = int(kernel_cfg.get("min_boot_free_mb", 200))
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
                _log.warn(f"{msg} (dry-run: would abort)")
            else:
                raise RuntimeError(f"[KERNEL] {msg}")

    # D5 — /boot mounted with headroom.
    if boot_audit:
        space = kernel_safety.check_boot_mount_space(min_mb=min_free)
        if space is not None:
            if dry_run:
                _log.warn(f"[dry-run] {space.message}")
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
    if bool(kernel_cfg.get("capture_lsmod_snapshot", True)):
        _log.warn(
            "lsmod snapshot captured for `make localmodconfig` — drivers for "
            "hardware not active right now will be stripped from the build. "
            "The post-build boot audit (Gate 2) is the backstop."
        )

    # F1 — DKMS modules will need rebuilding against the new kernel.
    dkms = kernel_safety.list_dkms_modules()
    if dkms:
        _log.warn(
            f"DKMS modules present ({', '.join(dkms)}) — they must rebuild "
            f"against {pkgname}; ensure {pkgname}-headers is installed or they "
            "will not load on reboot (nvidia → black screen)."
        )

    # C3 — mkinitcpio HOOKS vs root topology.
    for finding in kernel_safety.check_mkinitcpio_hooks(topology):
        _log.warn(f"{finding.message} {finding.remediation}".strip())

    return topology


def _resolve_built_config(pkgbuild_dir):
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


def _built_kernel_release(config_path):
    """Read the built kernel's release string from kbuild's kernel.release."""
    if config_path is None:
        return None
    rel = config_path.parent / "include" / "config" / "kernel.release"
    text = rel.read_text().strip() if rel.exists() else ""
    return text or None


def _gate2_audit(pkgbuild_dir, topology, *, skip_boot_audit, state_dir=None):
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
    config_path = _resolve_built_config(pkgbuild_dir)
    if config_path is None:
        _log.warn(
            "Gate 2: resolved kernel .config not found in build tree — "
            "boot-critical config could not be validated before install."
        )
        return

    _log.ui(f"Gate 2: auditing resolved kernel config {config_path}")

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
                cache_path, kconfig_map, _built_kernel_release(config_path),
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
    kver = _built_kernel_release(config_path)
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


def _gate2_kconfig_drift(pkgbuild_dir, fragment_path):
    """Advisory: warn when options sysforge merged didn't survive into the
    resolved .config.

    Runs post-build (beside Gate 2, pre-install) but **never raises** — a drop
    can be a deliberate ``nconfig`` toggle *or* legitimate dependency
    resolution by ``make olddefconfig``, and sysforge can't tell the two apart
    without a full dep solve. ``fragment_path is None`` (merge disabled or no
    entries) makes this a no-op, so the check is on exactly when the merge is.
    """
    if fragment_path is None:
        return

    config_path = _resolve_built_config(pkgbuild_dir)
    if config_path is None:
        _log.info(
            "kconfig drift check skipped — resolved .config not found in build tree"
        )
        return

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
        return

    _log.warn(
        f"kconfig drift: {len(drifts)} option(s) sysforge merged differ in the "
        "resolved .config — possibly toggled in `nconfig`, or dropped by "
        "`make olddefconfig` due to unmet dependencies (advisory, not a failure)"
    )
    for d in drifts:
        _log.warn(f"  {d.option}: {d.requested} → {d.resolved} ({d.kind})")


def _gate3_verify(pkgbuild_dir, pkgname, bootloader):
    """Post-install boot-readiness verification. Raises on brick.

    Runs inside the sentinel: a failure here leaves the sentinel set so the
    operator is told to resolve boot wiring before the next run.
    """
    findings = list(kernel_safety.verify_boot_artifacts(pkgname, bootloader))
    kver = _built_kernel_release(_resolve_built_config(pkgbuild_dir))
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
# Resolution summary
# ---------------------------------------------------------------------------


def _log_resolution_summary(
    *, pkgname, compiler, compiler_origin, cc, cxx, variant, bootloader,
    bootloader_installed, source, kconfig_target, base_config_source,
    hw_kconfig_count, manual_kconfig_count, device_kconfig_count,
    kernel_cfg, skip_boot_audit,
):
    """Emit one labelled block of the resolved kernel-build plan.

    Consolidates what the stage decided (compiler + its origin, inherited
    toolchain variant, bootloader, source, interactive mode, kconfig counts,
    and the boot-safety gate settings) so the operator can eyeball it before a
    long build — and so ``--dry-run`` has a readable summary rather than only
    scattered ``[dry-run] would …`` lines.
    """
    compiler_label = compiler or "profile default"
    boot_note = "" if bootloader_installed else "  (not detected installed!)"
    gates = (
        f"fallback={'required' if kernel_cfg.get('require_fallback_kernel', True) else 'off'} "
        f"boot_audit={'on' if kernel_cfg.get('boot_audit', True) else 'off'}"
        f"{' SKIPPED' if skip_boot_audit else ''} "
        f"min_boot_free={int(kernel_cfg.get('min_boot_free_mb', 200))}MiB "
        f"lsmod_snapshot={'on' if kernel_cfg.get('capture_lsmod_snapshot', True) else 'off'}"
    )
    _log.ui("Kernel build plan:")
    _log.ui(f"  package:    {pkgname}")
    _log.ui(f"  compiler:   {compiler_label} (from {compiler_origin}; cc={cc or '-'} cxx={cxx or '-'})")
    _log.ui(f"  variant:    {variant}")
    _log.ui(f"  bootloader: {bootloader}{boot_note}")
    _log.ui(f"  source:     {source}")
    _log.ui(
        f"  kconfig:    {kconfig_target} ({hw_kconfig_count} hardware, "
        f"{device_kconfig_count} device, {manual_kconfig_count} manual)"
    )
    _log.ui(f"  base cfg:   {base_config_source}")
    _log.ui(f"  gates:      {gates}")


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

        # pkgbuild_src_dir is optional in kernel.toml: when unset, fall back to
        # the global [paths] pkgbuild_src_dir. Resolve once and stamp it back so
        # the kernel_cfg-only _pkgbuild_path() call sites pick up the global
        # value without each needing the full config dict.
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        eff_src_dir = resolve_pkgbuild_src_dir(config, build_cfg=kernel_cfg)
        if eff_src_dir:
            kernel_cfg["pkgbuild_src_dir"] = eff_src_dir

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

        # lsmod snapshot — captured before build for localmodconfig
        # reproducibility. Opt-out via `capture_lsmod_snapshot = false` (Gate 1
        # warns that localmodconfig strips drivers for inactive hardware).
        if bool(kernel_cfg.get("capture_lsmod_snapshot", True)):
            _capture_lsmod_snapshot(state_dir, options.dry_run)

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

        # A4: warn + confirm if the kernel pkgname shadows a pacman repo package
        # (would overwrite the official package on install).
        _check_pkgname_repo_collision(pkgname, options)

        # Compiler resolution: CLI > kernel.toml > pipeline state from toolchain.
        compiler, cc, cxx = _resolve_compiler(kernel_cfg, options, state)
        if getattr(options, "compiler", None):
            compiler_origin = "--compiler"
        elif kernel_cfg.get("compiler"):
            compiler_origin = "kernel.toml"
        elif cc:
            compiler_origin = "toolchain pipeline state"
        else:
            compiler_origin = "profile default"
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

        # 4a: configured-vs-installed toolchain mismatch. The variant nudge above
        # reflects what the toolchain *stage* registered in pipeline state; this
        # reflects on-disk reality via collect_llvm_state (provenance, not a
        # health probe). It fires even when the toolchain stage never populated
        # pipeline state — e.g. toolchain.toml configures PGO LLVM but a stock
        # repo llvm is installed, so the kernel won't be built with the PGO
        # toolchain the user thinks is active. Surfaced standalone via
        # `sysforge doctor --toolchain`.
        from sysforge.primitives.llvm_state import detect_toolchain_config_mismatch
        for finding in detect_toolchain_config_mismatch(config):
            _log.warn(f"{finding.message} — {finding.remediation}")

        # Base .config seeding — written before the fragment so the build order
        # is base → fragment overlay. "pkgbuild" (default) is a no-op; "running"
        # or a path seeds sysforge.base.config for the PKGBUILD to copy to .config.
        base_config_source = _write_base_config(kernel_cfg, options.dry_run, options)

        # kconfig fragment — requires hardware_profile.toml (hardware stage).
        # Written after the source sync so a --cleansrc re-clone doesn't wipe
        # it, and after compiler resolution so the fragment header can carry
        # the toolchain provenance (C2). Still before the build, which reads
        # sysforge.config in the PKGBUILD's prepare().
        provenance = f"toolchain variant: {variant}  cc: {cc or '-'}"
        fragment_path, hw_kconfig_count, manual_kconfig_count, device_kconfig_count = (
            _write_kconfig_fragment(
                kernel_cfg, config, options.dry_run, provenance=provenance,
                state_dir=state_dir,
            )
        )

        kconfig_target = "make nconfig (user-supplied)" if interactive else "make olddefconfig (patched)"
        _log.info(f"Kernel kconfig target: {kconfig_target}")

        # C3: standalone interactive runs require the operator to drive the
        # PKGBUILD's kconfig UI. Pipeline / --non-interactive / dry-run paths
        # don't, so only nudge for a real interactive run.
        if interactive and not options.dry_run:
            _log.info(
                "Running interactively (the PKGBUILD's `make nconfig`/etc. runs "
                "as written); pass --non-interactive for unattended builds."
            )

        skip_boot_audit = bool(getattr(options, "skip_boot_audit", False))

        # B1: consolidated resolution summary — one labelled block instead of
        # decisions scattered across the log. Useful before a multi-hour build
        # and the readable core of a --dry-run preview.
        _log_resolution_summary(
            pkgname=pkgname,
            compiler=compiler,
            compiler_origin=compiler_origin,
            cc=cc,
            cxx=cxx,
            variant=variant,
            bootloader=bootloader,
            bootloader_installed=(bootloader == "none" or bootloader in _probe_installed_bootloader()),
            source=source,
            kconfig_target=kconfig_target,
            base_config_source=base_config_source,
            hw_kconfig_count=hw_kconfig_count,
            manual_kconfig_count=manual_kconfig_count,
            device_kconfig_count=device_kconfig_count,
            kernel_cfg=kernel_cfg,
            skip_boot_audit=skip_boot_audit,
        )

        # Gate 1 — cheap preflight (fallback-kernel guarantee, /boot space,
        # root-topology capture, advisory warnings). Hard-fails *before* the
        # build so a missing fallback / full /boot aborts with nothing spent.
        topology = _gate1_preflight(
            kernel_cfg, options, pkgname, dry_run=options.dry_run,
        )

        # Advisory lock around the whole build → audit → install window so two
        # concurrent `sysforge run kernel` runs sharing this state dir can't
        # clobber ~/builds/<pkgbase> (the second nconfig/makepkg would step on
        # the first's .config). Scoped to state_dir (not /var/tmp) so test runs
        # with a tmp state dir are isolated. Skipped in dry-run — nothing is
        # built, and the lock file would be a side effect. Shared primitive with
        # the toolchain stage's PGO lock (see primitives/build_lock.py).
        _lock = (
            contextlib.nullcontext()
            if options.dry_run
            else build_lock(state_dir / "kernel-build.lock", label="kernel")
        )
        with _lock:
            # Build WITHOUT installing, then audit the resolved .config, then
            # install — so a boot-critical kconfig drop (Gate 2) aborts before
            # any mutation and the running kernel stays bootable. The build
            # itself mutates nothing, so it runs outside the install sentinel;
            # a Gate 2 abort therefore leaves no sentinel behind.
            if options.dry_run:
                _log.ui(f"[dry-run] would build {pkgname} (no install) from {pkgbuild}")
            else:
                _log.ui(f"Building kernel (no install): {pkgname} from {pkgbuild}")
                try:
                    makepkg_run(pkgbuild, options=make_build_options(
                        "kernel", options,
                        log_dir=options.log_dir,
                        profile_conf=getattr(options, "profile_conf", None) or config.get("profile_conf"),
                        update=(not options.no_update) and not synced,
                        interactive=interactive,
                        cc_override=cc,
                        cxx_override=cxx,
                        source=source,
                        toolchain_variant=variant if variant != "system" else None,
                    ))
                except AlreadyBuilt:
                    _log.info(
                        "Kernel package already built — proceeding to audit + install",
                    )

                # Gate 2 — resolved-.config audit (raises on brick, pre-install).
                _gate2_audit(
                    pkgbuild.parent, topology,
                    skip_boot_audit=skip_boot_audit, state_dir=state_dir,
                )

                # Advisory: warn if any option sysforge merged didn't survive
                # the build's kconfig resolution (nconfig toggle or olddefconfig
                # dep drop). Never raises; no-op when no fragment was written.
                _gate2_kconfig_drift(pkgbuild.parent, fragment_path)

            # Install + boot wiring are the mutation window — wrap them in the
            # sentinel so an interrupted install / mkinitcpio / bootloader regen
            # blocks the next run with a recovery command (the failure mode that
            # leaves the system kernel-installed but initramfs-missing →
            # unbootable).
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
                    _log.ui(f"[dry-run] would install {pkgname} and wire it into boot")
                else:
                    install_built_packages(pkgbuild.parent, noconfirm=not interactive)

                _run_mkinitcpio(options.dry_run)
                _update_bootloader(bootloader, options.dry_run)

                # Gate 3 — post-install boot-readiness (raises on brick). Inside
                # the sentinel so an unbootable result blocks the next run for
                # recovery.
                if not options.dry_run:
                    _gate3_verify(pkgbuild.parent, pkgname, bootloader)

        _log.ui(f"Kernel stage complete: {pkgname}")
