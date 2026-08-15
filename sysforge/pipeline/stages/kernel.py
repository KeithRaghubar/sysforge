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
import os
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("KERNEL")
from sysforge.pipeline.stages.base import Stage
from sysforge.primitives import device_probe, kbuild_map, kernel_fdo, kernel_safety
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.config import load_sysforge_toml, resolve_repo_track
from sysforge.primitives.paths import KERNEL_PATH
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives.render import arrow, ellipsis_glyph
from sysforge.primitives.makepkg_wrapper import (
    AlreadyBuilt,
    install_built_packages,
    run as makepkg_run,
)
from sysforge.build_core import make_build_options
from sysforge.primitives.source_sync import (
    STATUS_DIVERGED,
    STATUS_FAILED,
    STATUS_FROZEN,
    STATUS_PURGE_REFUSED,
    STATUS_RATE_LIMITED,
    SyncRequest,
    get_scheduler,
)
from sysforge.primitives.stage_sentinel import sentinel_scope

_SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED, STATUS_FROZEN,
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


def _resolve_subpackages(kernel_cfg, options):
    """Resolve whether to build the kernel -headers / -docs subpackages.

    Precedence per toggle: CLI flag (``--headers``/``--no-headers``,
    ``--docs``/``--no-docs``) > kernel.toml (``build_headers``/``build_docs``) >
    hard default (headers on, docs off). The CLI fields default to ``None`` when
    the flag is unset, so ``None`` falls through to the TOML value.

    Returns ``(build_headers, build_docs)``.
    """
    cli_headers = getattr(options, "build_headers", None)
    build_headers = (
        cli_headers if cli_headers is not None
        else bool(kernel_cfg.get("build_headers", True))
    )
    cli_docs = getattr(options, "build_docs", None)
    build_docs = (
        cli_docs if cli_docs is not None
        else bool(kernel_cfg.get("build_docs", False))
    )
    return build_headers, build_docs


def _resolve_keep_hotplug_drivers(kernel_cfg, options):
    """Resolve whether to re-enable hotplug driver classes as modules after
    config minimization (F2).

    Precedence: CLI (``--keep-hotplug-drivers`` / ``--no-keep-hotplug-drivers``)
    > kernel.toml (``keep_hotplug_drivers``) > hard default (off). The CLI field
    defaults to ``None`` when the flag is unset, so ``None`` falls through to the
    TOML value.
    """
    cli = getattr(options, "keep_hotplug_drivers", None)
    if cli is not None:
        return bool(cli)
    return bool(kernel_cfg.get("keep_hotplug_drivers", False))


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


_VALID_SOURCES = ("local", "repo", "aur")


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


def _resolve_already_built_action(options, interactive):
    """Decide what an AlreadyBuilt (makepkg exit 13) kernel build does next (B5).

    Policy lives in the one-home seam (2.5.1-F2):
    ``primitives.already_built.resolve_already_built`` with the
    ``review-gated`` posture — a stale same-version package in PKGDEST made
    makepkg skip the build, so the in-prepare() kconfig review an interactive
    run promised never happened. This wrapper only adapts the seam's action
    enum to the stage's ``"install"``/``"rebuild"`` vocabulary.
    """
    from sysforge.primitives.already_built import (
        AlreadyBuiltAction,
        resolve_already_built,
    )

    action = resolve_already_built(
        "review-gated",
        interactive=interactive,
        non_interactive=bool(getattr(options, "non_interactive", False)),
        tag="KERNEL",
        abort_hint=(
            "the kconfig review did not run. Rebuild with `-f` (choice r), "
            "bump pkgver/pkgrel, or remove the stale package from PKGDEST "
            "for a fresh build."
        ),
    )
    return "rebuild" if action is AlreadyBuiltAction.REBUILD else "install"


def _resolve_names(kernel_cfg):
    """Resolve ``(upstream_pkgname, pkgname)`` from kernel.toml (F40).

    ``upstream_pkgname`` is what sysforge pulls/tracks (e.g. ``linux-zen``);
    ``pkgname`` is the local name it builds/installs as, defaulting to
    ``upstream_pkgname`` when omitted. Pure-local configs set only ``pkgname``
    (upstream is None → no sync remote, no rename). At least one must be set.
    """
    upstream = kernel_cfg.get("upstream_pkgname") or None
    pkgname = kernel_cfg.get("pkgname") or upstream
    if not pkgname:
        raise RuntimeError(
            "[KERNEL] kernel.toml is missing pkgname (set pkgname, or "
            "upstream_pkgname to track an upstream kernel)."
        )
    return upstream, pkgname


def _resolve_source(kernel_cfg, srcdir_path):
    """Resolve the kernel PKGBUILD source classification (F40).

    Explicit ``source`` (``local`` | ``repo`` | ``aur``) is honored — ``git``
    was a phantom value (no URL field ever existed) and now yields a clear
    error. When omitted, auto-resolve ``local → repo → aur``:

    * ``srcdir_path`` exists without ``.git`` → hand-maintained tree →
      ``local`` (never clobbered by a re-clone).
    * ``srcdir_path`` is an existing git clone → ``repo``: the scheduler's
      generic fetch path rebases via the tree's own origin (and skips the
      AUR RPC, which is wrong for non-AUR upstreams).
    * ``srcdir_path`` missing → pick the clone remote by whether the tracked
      name is in a pacman sync DB (the same probe the pkgname-collision
      check uses): in a repo → ``repo`` (pkgctl), else → ``aur``.
    """
    src = kernel_cfg.get("source")
    if src is not None:
        if src == "git":
            raise RuntimeError(
                "[KERNEL] kernel.toml source = \"git\" is no longer supported "
                "(it never had a URL to clone from). Use \"local\", \"repo\", "
                "or \"aur\" — or omit source to auto-resolve."
            )
        if src not in _VALID_SOURCES:
            raise RuntimeError(
                f"[KERNEL] invalid kernel.toml source {src!r}: "
                f"must be one of {_VALID_SOURCES}"
            )
        return src

    srcdir_path = Path(srcdir_path)
    if srcdir_path.is_dir():
        if (srcdir_path / ".git").exists():
            return "repo"
        return "local"

    from sysforge.primitives.aur import is_repo_package
    upstream, pkgname = _resolve_names(kernel_cfg)
    tracked = upstream or pkgname
    return "repo" if is_repo_package(tracked) else "aur"


def _presync_kernel_source(pkgbuild_dir, options, state_dir, source="local"):
    """
    Sync the kernel source tree through the SourceSyncScheduler.

    Runs whenever --cleansrc/--cleansrc-force is set (forcing a purge even
    when --no-update is also set) or when --no-update was not passed.
    Skipped otherwise. Returns True if a sync was attempted.

    ``source = "local"`` short-circuits the scheduler — there's no remote to
    fetch against. The PKGBUILD must already be present at ``pkgbuild_dir``.
    """
    cleansrc = bool(
        getattr(options, "cleansrc", False) or getattr(options, "cleansrc_force", False)
    )
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
        repo_track=resolve_repo_track(load_sysforge_toml().get("build", {})),
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
# Sample-based FDO (AutoFDO / Propeller) — kernel_fdo orchestration
#
# Three steps spanning reboots: `record` builds a profiling kernel
# (CONFIG_AUTOFDO_CLANG=y, stock name); `capture` is a read-only step that prints
# host-tailored perf + create_llvm_prof commands; `use` rebuilds consuming the
# collected profile (injected via the build's extra_env make-variables) and earns
# the -sysforge coexist rename. LLVM-only — there is no GCC path. See
# DESIGN.md §Kernel stage.
# ---------------------------------------------------------------------------


def _resolve_fdo(options):
    """Resolve the kernel FDO request from CLI options.

    Returns ``(mode, propeller)`` — ``mode`` is one of ``kernel_fdo.VALID_MODES``
    (``record``/``capture``/``use``) or ``None`` when no FDO flag was passed.
    ``--propeller`` is a modifier that layers Propeller on the AutoFDO cycle, so
    it requires a mode. Raises on an invalid combination.
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
    return mode, propeller


def _fdo_is_llvm(compiler, cc):
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


def _gate_fdo_llvm(mode, propeller, compiler, cc):
    """Hard-abort when kernel FDO is requested under a non-LLVM toolchain.

    AutoFDO/Propeller have no GCC path in sysforge, and the kernel's
    CONFIG_AUTOFDO_CLANG/CONFIG_PROPELLER_CLANG have no GCC equivalent at all — so
    this is a clean refusal before any build work. The single home for the
    kernel-FDO LLVM gate.
    """
    if _fdo_is_llvm(compiler, cc):
        return
    from sysforge.primitives.profile import LLVM_REQUIRED_HINT
    feature = "kernel Propeller" if propeller else "kernel AutoFDO"
    raise RuntimeError(
        f"[KERNEL] {feature} (--autofdo={mode}) requires the LLVM toolchain. "
        f"{LLVM_REQUIRED_HINT}"
    )


def _run_fdo_capture(pkgname, propeller, dry_run):
    """The ``--autofdo=capture`` step: print host-tailored perf + create_llvm_prof
    commands for the operator to run on the booted profiling kernel.

    Read-only and non-mutating — no build, no install, no sentinel. Provisions
    the store so the printed ``create_llvm_prof --out=<store>/…`` can write, then
    resolves this host's branch-sampling event and the matching vmlinux and
    prints the command block. ``--autofdo=use`` later consumes whatever profile
    landed in the store.
    """
    store = kernel_fdo.resolve_store(pkgname, propeller=propeller)
    sampling = kernel_fdo.detect_branch_sampling()
    # The profiling kernel is built+installed under its coexist record name
    # (F26), so its build tree — and the vmlinux create_llvm_prof needs — lives
    # under that name, not the stock pkgname. The store stays keyed on the stock
    # pkgname (record/capture/use share it).
    record_name = kernel_fdo.record_pkgname(pkgname)
    vmlinux = kernel_fdo.resolve_vmlinux(record_name)

    if dry_run:
        _log.ui(
            f"[dry-run] would print AutoFDO capture commands for {pkgname} "
            f"(store {store})"
        )
        return

    from sysforge.primitives import fs_provision
    try:
        fs_provision.ensure_writable_dir(store)
    except fs_provision.FsProvisionError as e:
        _log.warn(
            f"FDO store {store} could not be group-provisioned ({e}) — "
            "create_llvm_prof may be unable to write the profile there"
        )

    if sampling.supported:
        _log.info(f"branch sampling: {sampling.note}")
    else:
        _log.warn(f"branch sampling unsupported on this CPU: {sampling.note}")
    if vmlinux is None:
        _log.warn(
            "no uncompressed vmlinux found in the build tree — build the "
            "profiling kernel with `--autofdo=record` first, then substitute the "
            "real vmlinux path in the command below."
        )

    _log.ui(
        f"AutoFDO{' + Propeller' if propeller else ''} capture — reboot into "
        f"{record_name}, then run these while exercising the machine:"
    )
    for line in kernel_fdo.capture_commands(
        store, sampling=sampling, vmlinux=vmlinux, propeller=propeller
    ):
        _log.ui(f"  {line}")
    _log.ui(
        "Then rebuild the optimized kernel: "
        f"sysforge run kernel --autofdo=use{' --propeller' if propeller else ''}"
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

    with path.open("rb") as f:
        data = tomllib.load(f)

    _log.info(f"Loaded kernel config from {path}")
    return data


def _srcdir_path(kernel_cfg):
    """Resolve the kernel PKGBUILD *directory* (no existence requirement).

    Split out of :func:`_pkgbuild_path` so the build entry can hand the dir to
    the source-sync scheduler *before* requiring a PKGBUILD — a missing tree is
    then bootstrapped by the scheduler's clone-if-missing path (F40) instead of
    aborting here.
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
    upstream, pkgname = _resolve_names(kernel_cfg)
    srcdir = kernel_cfg.get("srcdir") or upstream or pkgname
    return Path(pkgbuild_src_dir).expanduser() / srcdir


def _pkgbuild_path(kernel_cfg):
    """
    Resolve the PKGBUILD for the configured kernel package.
    Returns Path to the PKGBUILD file.

    Looks for <pkgbuild_src_dir>/<srcdir>/PKGBUILD where srcdir resolves as
    srcdir → upstream_pkgname → pkgname (first set wins): the explicit
    override, else the tracked upstream's name, else the local name. srcdir
    allows the source directory name to differ from either (e.g.
    pkgname="linux-custom", srcdir="linux").
    """
    srcdir_path = _srcdir_path(kernel_cfg)
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


def _merge_lsmod(prior_text: str, current_text: str) -> str:
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


def _capture_lsmod_snapshot(state_dir, dry_run):
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
            out = _merge_lsmod(snapshot_path.read_text(), out)
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


def _validate_manual_kconfig(entries):
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

_KCONFIG_ALL_TARGETS = (
    KCONFIG_UI_TARGETS + KCONFIG_PROMPTING_TARGETS + KCONFIG_SILENT_TARGETS
)

_KCONFIG_SILENT_EQUIVALENT = {
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
_HOTPLUG_KCONFIG = {
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
    targets = kernel_cfg.get("kconfig_targets")
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
        if target not in _KCONFIG_ALL_TARGETS:
            raise ValueError(
                f"kconfig_targets: unknown target {target!r} — allowed targets "
                f"are {', '.join(_KCONFIG_ALL_TARGETS)}"
            )

        if not interactive and target in KCONFIG_PROMPTING_TARGETS:
            if target in _KCONFIG_SILENT_EQUIVALENT:
                raise ValueError(
                    f"kconfig_targets: {target!r} requires interactive input — "
                    f"use {_KCONFIG_SILENT_EQUIVALENT[target]} instead"
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


def _format_kconfig_line(option, value):
    """Format a single option=value pair as a kernel .config line."""
    if value in ("y", "m"):
        return f"{option}={value}"
    if value == "n":
        return f"# {option} is not set"
    return f'{option}="{value}"'


def _write_hotplug_fragment(kernel_cfg, options, dry_run):
    """Write (or remove) the post-minimization hotplug fragment (F2).

    When ``keep_hotplug_drivers`` resolves true, writes ``sysforge.hotplug.config``
    next to the PKGBUILD with the curated ``_HOTPLUG_KCONFIG`` set at each
    symbol's kconfig-legal value (``=m`` for tristate, ``=y`` for bool) and
    returns its path. This fragment is merged AFTER the minimization sequence
    (see kconfig_plan.hotplug_merge_step) so ``localmodconfig`` can't
    strip the modules back out.

    When disabled (or dry-run), removes any stale fragment from a prior run so
    "off" means off, and returns ``None``. Mirrors the stale-cleanup contract of
    ``_write_kconfig_fragment`` (kconfig_merge = false path).
    """
    fragment_path = _pkgbuild_path(kernel_cfg).parent / "sysforge.hotplug.config"

    if not _resolve_keep_hotplug_drivers(kernel_cfg, options):
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
            f"[dry-run] would write hotplug fragment ({len(_HOTPLUG_KCONFIG)} "
            "modules re-enabled post-minimization)"
        )
        return None

    lines = [
        "# Generated by SysForge — do not edit manually",
        "# Merged into .config AFTER minimization (localmodconfig) so hotplug",
        "# driver classes stay available as modules. keep_hotplug_drivers = true.",
    ]
    lines += [f"{option}={value}" for option, value in _HOTPLUG_KCONFIG.items()]
    fragment_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log.info(
        f"Wrote hotplug fragment ({len(_HOTPLUG_KCONFIG)} modules): {fragment_path}"
    )
    return fragment_path


def _write_kconfig_fragment(
    kernel_cfg, config, dry_run, provenance=None, state_dir=None, extra_kconfig=None
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
        return None, 0, 0, 0, 0

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
        lines.append(_format_kconfig_line(option, value))

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
    _log.info(f"Wrote base kernel config ({source_label}): {base_path}")
    return source_label


# ---------------------------------------------------------------------------
# Post-install steps
# ---------------------------------------------------------------------------


def _run_mkinitcpio(dry_run):
    """Regenerate all initramfs presets."""
    if dry_run:
        _log.ui("[dry-run] would run: sudo mkinitcpio -P")
        return
    _log.info("Running mkinitcpio -P")
    result = subprocess.run(privileged_argv(["mkinitcpio", "-P"]))
    if result.returncode != 0:
        raise RuntimeError(f"[KERNEL] mkinitcpio -P failed (exit {result.returncode})")


def _update_bootloader(bootloader, dry_run):
    """Update the bootloader config to pick up the new kernel."""
    if bootloader == "none" or not bootloader:
        _log.info("Bootloader update skipped (bootloader = 'none')")
        return

    if bootloader == "grub":
        cmd = privileged_argv(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
        label = "grub-mkconfig"
    elif bootloader == "systemd-boot":
        cmd = privileged_argv(["bootctl", "update"])
        label = "bootctl update"
    else:
        _log.warn(f"Unknown bootloader {bootloader!r} — skipping update")
        return

    if dry_run:
        _log.ui(f"[dry-run] would run: {' '.join(cmd)}")
        return

    _log.info(f"Updating bootloader: {label}")
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
    build_headers, _ = _resolve_subpackages(kernel_cfg, options)
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

    Returns the drift list so the change summary can relocate the same result
    into its own block (2.6.1-F25) without re-running the comparison; the
    mid-run warnings stay exactly as they were. ``None`` — as distinct from
    ``[]`` — means the check did not run at all, which the summary renders as
    an explicit "did NOT run" rather than as silence.
    """
    if fragment_path is None:
        return None

    config_path = _resolve_built_config(pkgbuild_dir)
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
_KCONFIG_DIFF_CAP = 40


def _record_and_diff_kconfig(state_dir, pkgname, pkgbuild_dir):
    """Archive this build's resolved .config and diff it against the previous.

    Returns ``(previous_release, changes)`` or ``None`` when there is nothing
    to compare — no build tree this run (the AlreadyBuilt path) or no earlier
    archive (a first build). Best-effort throughout: this is advisory output
    and must never affect the build.
    """
    from sysforge.primitives import kconfig_history

    try:
        config_path = _resolve_built_config(pkgbuild_dir)
        if config_path is None:
            return None
        release = _built_kernel_release(config_path) or "unknown"
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


def _kconfig_diff_lines(prev_release: str, changes) -> list[str]:
    """Render the build-to-build kconfig diff, capped, with a full-list pointer."""
    if not changes:
        return [f"no kconfig changes since {prev_release}"]

    lines = [f"{len(changes)} symbol(s) changed since {prev_release}:"]
    for change in changes[:_KCONFIG_DIFF_CAP]:
        if change.kind == "added":
            lines.append(f"  +{change.option}={change.new}")
        elif change.kind == "removed":
            lines.append(f"  -{change.option} (was {change.old})")
        else:
            lines.append(f"  {change.option}: {change.old} {arrow()} {change.new}")

    remaining = len(changes) - _KCONFIG_DIFF_CAP
    if remaining > 0:
        lines.append(f"  {ellipsis_glyph()} and {remaining} more (full list in the run log)")
    return lines


def _kconfig_drift_lines(drifts) -> list[str]:
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
    kernel_cfg, skip_boot_audit, build_headers, build_docs,
    fdo_kconfig_count=0, fdo_label="off",
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
    _log.ui(
        f"  compiler:   {compiler_label} (from {compiler_origin}; "
        f"cc={cc or '-'} cxx={cxx or '-'})"
    )
    _log.ui(f"  variant:    {variant}")
    _log.ui(f"  bootloader: {bootloader}{boot_note}")
    _log.ui(f"  source:     {source}")
    kconfig_counts = (
        f"{hw_kconfig_count} hardware, {device_kconfig_count} device, "
        f"{manual_kconfig_count} manual"
    )
    if fdo_kconfig_count:
        kconfig_counts += f", {fdo_kconfig_count} fdo"
    _log.ui(f"  kconfig:    {kconfig_target} ({kconfig_counts})")
    _log.ui(f"  base cfg:   {base_config_source}")
    _log.ui(f"  fdo:        {fdo_label}")
    _log.ui(
        f"  subpkgs:    headers={'on' if build_headers else 'off'} "
        f"docs={'on' if build_docs else 'off'}"
    )
    _log.ui(f"  gates:      {gates}")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class KernelStage(Stage):
    name = "kernel"
    description = "Build and install a custom kernel"
    depends_on = ["packages"]
    makepkg_bearing = True
    reports_changes = True

    # Captured during run() for the F25 change-summary blocks. Class attributes,
    # not constructor fields: stages/__init__.py instantiates every stage once
    # at import, so this instance is effectively a module-level singleton, and
    # each run() must reset them rather than inherit a prior run's result.
    _kconfig_drift = None    # list[KconfigDrift] | None; None = check did not run
    _kconfig_diff = None     # (prev_release, list[KconfigChange]) | None

    def change_extras(self, config, state, options):
        """Kconfig diff + drift blocks (2.6.1-F25). Advisory; never raises."""
        from sysforge.primitives.change_report import ExtraBlock

        blocks = []
        if self._kconfig_diff is not None:
            prev_release, changes = self._kconfig_diff
            blocks.append(ExtraBlock(
                label="Kconfig vs previous build:",
                lines=_kconfig_diff_lines(prev_release, changes),
            ))
            # The inline block is capped; the log gets everything.
            for change in changes[_KCONFIG_DIFF_CAP:]:
                _log.info(
                    f"kconfig diff (full): {change.option}: "
                    f"{change.old or '(absent)'} → {change.new or '(absent)'} "
                    f"({change.kind})"
                )
        if self._reported_kconfig_merge:
            blocks.append(ExtraBlock(
                label="Kconfig merge drift:",
                lines=_kconfig_drift_lines(self._kconfig_drift),
            ))
        return blocks

    # True once a run has reached the point where the merge-drift check either
    # ran or was skipped. Without it, a stage that no-opped early (kernel.toml
    # disabled) would render a "did NOT run" block about a check that was never
    # applicable.
    _reported_kconfig_merge = False

    def run(self, config, state, options):
        from sysforge.pipeline.state import get_toolchain_variant, resolve_state_dir
        from sysforge.primitives.build_state import BuildState

        self._kconfig_drift = None
        self._kconfig_diff = None
        self._reported_kconfig_merge = False

        kernel_cfg = _load_kernel_config()
        if kernel_cfg is None or not kernel_cfg.get("enabled", False):
            _log.ui("kernel.toml absent or disabled — stage is a no-op")
            return

        from sysforge.primitives import snapshot
        snapshot.ensure_pre_build_snapshot(config, dry_run=options.dry_run)

        # pkgbuild_src_dir is optional in kernel.toml: when unset, fall back to
        # the global [paths] pkgbuild_src_dir. Resolve once and stamp it back so
        # the kernel_cfg-only _pkgbuild_path() call sites pick up the global
        # value without each needing the full config dict.
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        eff_src_dir = resolve_pkgbuild_src_dir(config, build_cfg=kernel_cfg)
        if eff_src_dir:
            kernel_cfg["pkgbuild_src_dir"] = eff_src_dir

        # F40: decouple what to pull (upstream_pkgname) from what to build/
        # install as (pkgname); source auto-resolves local → repo → aur when
        # omitted, keyed off the (possibly not-yet-cloned) source dir.
        upstream_pkgname, pkgname = _resolve_names(kernel_cfg)
        bootloader = _resolve_bootloader(kernel_cfg, options)
        srcdir_path = _srcdir_path(kernel_cfg)
        source = _resolve_source(kernel_cfg, srcdir_path)

        # Hoisted once: shared by the drift check below, the compiler nudge
        # downstream, and the BuildState.record() variant stamp later in run.
        variant = get_toolchain_variant(state)
        state_dir, _ = resolve_state_dir(options.state_dir)

        # Sample-based FDO (AutoFDO / Propeller). Resolved up front so the
        # LLVM-only gate fires before any work, the read-only `capture` step can
        # short-circuit (print perf/create_llvm_prof commands, no build), and the
        # `use` step's profile presence is checked fail-fast. `fdo_env` (the
        # CLANG_AUTOFDO_PROFILE/CLANG_PROPELLER_PROFILE_PREFIX make-variables) and
        # `fdo_opt_build_mode` (autofdo_kernel/propeller_kernel → -sysforge coexist
        # rename) thread into the build call below; `fdo_eff_pkgname` is the
        # installed name the post-install gates must verify (the use build is
        # renamed inside makepkg_wrapper).
        fdo_mode, fdo_propeller = _resolve_fdo(options)
        fdo_env = None
        fdo_opt_build_mode = None
        fdo_eff_pkgname = pkgname
        if fdo_mode:
            _fdo_compiler, _fdo_cc, _ = _resolve_compiler(kernel_cfg, options, state)
            _gate_fdo_llvm(fdo_mode, fdo_propeller, _fdo_compiler, _fdo_cc)
            if fdo_mode == "capture":
                _run_fdo_capture(pkgname, fdo_propeller, options.dry_run)
                return
            if fdo_mode == "record":
                # F26: the instrumented profiling kernel must not overwrite the
                # production kernel. Install it under a distinct sysforge-owned
                # coexist name (its own /boot entry, bootloader fallback), applied
                # via the same rename_pkgbase_to seam as the use-build. The coexist
                # name is itself the ownership gate — a reinstall only ever
                # replaces a prior sysforge profiling kernel.
                fdo_eff_pkgname = kernel_fdo.record_pkgname(pkgname)
                _log.ui(
                    f"AutoFDO{' + Propeller' if fdo_propeller else ''} record-build: "
                    f"profiling kernel installs as {fdo_eff_pkgname} "
                    f"(coexists with {pkgname}; boot into it to collect samples)"
                )
            if fdo_mode == "use":
                _fdo_store = kernel_fdo.resolve_store(pkgname, propeller=fdo_propeller)
                # Clean pre-build abort if the record→capture profile is missing.
                kernel_fdo.require_profile(_fdo_store, propeller=fdo_propeller)
                fdo_env = kernel_fdo.use_env(_fdo_store, propeller=fdo_propeller)
                fdo_opt_build_mode = kernel_fdo.build_mode(propeller=fdo_propeller)
                if not pkgname.endswith("-sysforge"):
                    fdo_eff_pkgname = f"{pkgname}-sysforge"
                _log.ui(
                    f"AutoFDO{' + Propeller' if fdo_propeller else ''} use-build: "
                    f"consuming {_fdo_store} → {fdo_eff_pkgname} "
                    f"(coexists with {pkgname})"
                )

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
        # the plan's non-interactive rewrite so the user's PKGBUILD kconfig target
        # (typically `make nconfig`) runs as written, and the makepkg
        # subprocess inherits the parent's stdout/stderr so ncurses-driven
        # kconfig UIs render on the controlling TTY.
        # B8: interactive requires config ∧ ¬--non-interactive ∧ TTY — the
        # same three-way resolution the stage's sibling prompts (repo-pkgname
        # collision, diverged-source confirm) already use. Without the TTY leg
        # the stage promised an nconfig review that a piped/captured run could
        # never render, then silently EOF'd through it.
        from sysforge.primitives.prompt import is_interactive as _tty
        cfg_interactive = bool(kernel_cfg.get("interactive", True))
        flag_non_interactive = bool(getattr(options, "non_interactive", False))
        interactive = cfg_interactive and not flag_non_interactive and _tty()
        if cfg_interactive and not flag_non_interactive and not interactive:
            _log.warn(
                "interactive kconfig review requested (kernel.toml "
                "interactive = true) but no TTY is attached — running "
                "unattended; the config review will NOT run. Pass "
                "--non-interactive to make this explicit."
            )

        # F37: configured kconfig_targets sequence — resolved/validated here
        # (config-load time) so a bad list (unknown target, two UI targets, a
        # prompting target requested non-interactively) raises ValueError and
        # aborts before any makepkg invocation, rather than failing mid-build.
        # None when the key is unset — zero behavior change.
        kconfig_targets = resolve_kconfig_targets(kernel_cfg, interactive=interactive)

        # lsmod snapshot — captured before build for localmodconfig
        # reproducibility. Opt-out via `capture_lsmod_snapshot = false` (Gate 1
        # warns that localmodconfig strips drivers for inactive hardware).
        if bool(kernel_cfg.get("capture_lsmod_snapshot", True)):
            _capture_lsmod_snapshot(state_dir, options.dry_run)

        # Pre-sync the kernel PKGBUILD tree through SourceSyncScheduler. This
        # is the only allowed code path for refreshing sources (CLAUDE.md #3).
        # Running it here (vs. relying on makepkg_wrapper's internal sync)
        # makes --cleansrc work even when --no-update is also set. Sync runs
        # BEFORE the PKGBUILD path is required (F40) so a missing tree is
        # bootstrapped by the scheduler's clone-if-missing path instead of
        # aborting with "clone it first".
        synced = _presync_kernel_source(srcdir_path, options, state_dir, source=source)
        pkgbuild = _pkgbuild_path(kernel_cfg)

        # A3: static-parse the freshly-synced PKGBUILD and confirm its pkgbase
        # matches the *pre-rename* name of the tree — upstream_pkgname when
        # tracking an upstream, else the local pkgname. Catches a mis-cloned/
        # typo'd tree before makepkg --install fails late after a multi-hour
        # build; the local rename is a patch applied later in makepkg_wrapper.
        _validate_pkgname_matches_pkgbuild(pkgbuild, upstream_pkgname or pkgname)

        # A4: warn + confirm if the *installed* kernel name shadows a pacman repo
        # package (would overwrite the official package on install). For an FDO
        # use-build that is the -sysforge name, which never collides; for
        # record/no-FDO it is the stock pkgname.
        _check_pkgname_repo_collision(fdo_eff_pkgname, options)

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
            _log.info(f"Kernel compiler override: {compiler}  (cc={cc}  cxx={cxx})")
        elif cc:
            _log.info(f"Toolchain override from pipeline: cc={cc} cxx={cxx or '-'}")
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
        # FDO (record/use) needs CONFIG_AUTOFDO_CLANG (+ CONFIG_PROPELLER_CLANG)
        # in the fragment — and therefore the fragment itself. Refuse the
        # contradictory "FDO requested but kconfig_merge = false" combo rather
        # than silently building an unprofiled kernel.
        fdo_extra_kconfig = (
            kernel_fdo.fdo_kconfig(propeller=fdo_propeller) if fdo_mode else None
        )
        if fdo_extra_kconfig and not bool(kernel_cfg.get("kconfig_merge", True)):
            raise RuntimeError(
                "[KERNEL] --autofdo needs the kconfig fragment to set "
                f"{', '.join(fdo_extra_kconfig)}, but kernel.toml kconfig_merge = "
                "false disables it. Set kconfig_merge = true to use kernel FDO."
            )

        provenance = f"toolchain variant: {variant}  cc: {cc or '-'}"
        (
            fragment_path, hw_kconfig_count, manual_kconfig_count,
            device_kconfig_count, fdo_kconfig_count,
        ) = _write_kconfig_fragment(
            kernel_cfg, config, options.dry_run, provenance=provenance,
            state_dir=state_dir, extra_kconfig=fdo_extra_kconfig,
        )
        _write_hotplug_fragment(kernel_cfg, options, options.dry_run)

        if kconfig_targets:
            kconfig_target = f"{' → '.join(kconfig_targets)} (configured)"
        elif interactive:
            kconfig_target = "make nconfig (user-supplied)"
        else:
            kconfig_target = "make olddefconfig (patched)"
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
        build_headers, build_docs = _resolve_subpackages(kernel_cfg, options)

        if fdo_mode:
            fdo_label = (
                f"autofdo={fdo_mode}{' +propeller' if fdo_propeller else ''}"
            )
            if fdo_eff_pkgname != pkgname:
                fdo_label += f" → {fdo_eff_pkgname}"
        else:
            fdo_label = "off"

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
            bootloader_installed=(
                bootloader == "none" or bootloader in _probe_installed_bootloader()
            ),
            source=source,
            kconfig_target=kconfig_target,
            base_config_source=base_config_source,
            hw_kconfig_count=hw_kconfig_count,
            manual_kconfig_count=manual_kconfig_count,
            device_kconfig_count=device_kconfig_count,
            kernel_cfg=kernel_cfg,
            skip_boot_audit=skip_boot_audit,
            build_headers=build_headers,
            build_docs=build_docs,
            fdo_kconfig_count=fdo_kconfig_count,
            fdo_label=fdo_label,
        )

        # FDO feasibility advisory (record only — a `use` build already has its
        # profile). Surfaces this host's branch-sampling capability before the
        # operator commits to building + booting a profiling kernel: AMD BRS
        # (Zen 3+) is experimental for AutoFDO, and pre-Zen3 AMD has no path.
        if fdo_mode == "record":
            _sampling = kernel_fdo.detect_branch_sampling()
            if _sampling.supported:
                _log.warn(
                    "AutoFDO profiling kernel: after install, reboot into it and "
                    f"run `sysforge run kernel --autofdo=capture`. {_sampling.note}"
                )
            else:
                _log.warn(
                    "AutoFDO profiling kernel requested, but branch sampling is "
                    f"unsupported on this CPU — {_sampling.note} The collected "
                    "profile may be unusable."
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
            else build_lock(state_dir / "kernel-build.lock", label="kernel", noun="build")
        )
        with _lock:
            # Build WITHOUT installing, then audit the resolved .config, then
            # install — so a boot-critical kconfig drop (Gate 2) aborts before
            # any mutation and the running kernel stays bootable. The build
            # itself mutates nothing, so it runs outside the install sentinel;
            # a Gate 2 abort therefore leaves no sentinel behind.
            # B7: True when the install below installs a previously built
            # (stale) package rather than this run's fresh build — the
            # install-failure guidance keys on it to break the re-run loop.
            already_built = False
            if options.dry_run:
                _log.ui(f"[dry-run] would build {pkgname} (no install) from {pkgbuild}")
            else:
                # B6: the pre-nconfig pause now lives *inside* the patched
                # PKGBUILD's prepare() (kconfig_plan),
                # right after the base seed + fragment merge assemble the final
                # .config and immediately before `make nconfig`. A stage-level
                # pause here would fire before makepkg runs those in-prepare()
                # merges — the exact "confirm before the config is assembled"
                # defeat B6 describes — so it is deliberately not emitted here.
                _log.info(f"Building kernel (no install): {pkgname} from {pkgbuild}")

                def _kernel_build_options(extra_flags=None):
                    # One builder for both the normal build and the B5
                    # AlreadyBuilt "rebuild to review" retry (which adds -f) —
                    # so the retry can never drift from the real build's options.
                    return make_build_options(
                        "kernel", options,
                        extra_flags=extra_flags,
                        log_dir=options.log_dir,
                        profile_conf=(
                            getattr(options, "profile_conf", None)
                            or config.get("profile_conf")
                        ),
                        update=(not options.no_update) and not synced,
                        interactive=interactive,
                        cc_override=cc,
                        cxx_override=cxx,
                        source=source,
                        toolchain_variant=variant if variant != "system" else None,
                        kernel_build_headers=build_headers,
                        kernel_build_docs=build_docs,
                        kconfig_targets=kconfig_targets,
                        # FDO use-build: profile path make-variables (extra_env →
                        # `make`) + the optimization build_mode that earns the
                        # -sysforge coexist rename. Both None for record/no-FDO.
                        extra_env=fdo_env,
                        optimization_build_mode=fdo_opt_build_mode,
                        # Coexist pkgbase rename (patch_pkgbase_rename). For an
                        # --autofdo=record build the target is the distinct
                        # profiling name (F26), so the instrumented kernel never
                        # overwrites the production one. Otherwise it is the F40
                        # local-rename: patch the cloned upstream's pkgbase to the
                        # local pkgname so the build installs alongside the
                        # official package. None when neither applies (names match
                        # or pure-local) → no patch, upstream name.
                        rename_pkgbase_to=(
                            fdo_eff_pkgname
                            if fdo_mode == "record"
                            else (
                                pkgname
                                if upstream_pkgname and pkgname != upstream_pkgname
                                else None
                            )
                        ),
                    )

                try:
                    makepkg_run(pkgbuild, options=_kernel_build_options())
                except AlreadyBuilt:
                    # B5: makepkg exit 13 skipped the build — and with it the
                    # in-prepare() kconfig review an interactive run promised.
                    # Ask the operator (install as-built / rebuild with -f to
                    # review / abort); unattended runs keep the proceed path.
                    if _resolve_already_built_action(options, interactive) == "rebuild":
                        makepkg_run(
                            pkgbuild,
                            options=_kernel_build_options(extra_flags=["-f"]),
                        )
                    else:
                        already_built = True

                # Gate 2 — resolved-.config audit (raises on brick, pre-install).
                _gate2_audit(
                    pkgbuild.parent, topology,
                    skip_boot_audit=skip_boot_audit, state_dir=state_dir,
                )

                # Advisory: warn if any option sysforge merged didn't survive
                # the build's kconfig resolution (nconfig toggle or olddefconfig
                # dep drop). Never raises; no-op when no fragment was written.
                self._kconfig_drift = _gate2_kconfig_drift(
                    pkgbuild.parent, fragment_path
                )
                self._reported_kconfig_merge = fragment_path is not None

                # Archive this build's resolved .config so the *next* run can
                # diff against it, and capture the previous one for this run's
                # summary. Best-effort: never raises, never blocks the install.
                self._kconfig_diff = _record_and_diff_kconfig(
                    state_dir, pkgname, pkgbuild.parent
                )

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
                pkgname=fdo_eff_pkgname,
                bootloader=bootloader,
                compiler=compiler or "default",
            ):
                if options.dry_run:
                    _log.ui(f"[dry-run] would install {fdo_eff_pkgname} and wire it into boot")
                else:
                    try:
                        install_built_packages(
                            pkgbuild.parent, noconfirm=not interactive)
                    except RuntimeError as e:
                        if already_built:
                            # B7: AlreadyBuilt → install-as-built → install
                            # failure is the state that reproduces itself on
                            # every re-run (the stale package keeps
                            # short-circuiting the build). Point at the exit.
                            raise RuntimeError(
                                f"[KERNEL] {e} — installing a previously built "
                                "(stale) package failed; break the loop with a "
                                "fresh build: bump pkgver/pkgrel, or remove the "
                                "stale package(s) from PKGDEST and re-run."
                            ) from e
                        raise

                _run_mkinitcpio(options.dry_run)
                _update_bootloader(bootloader, options.dry_run)

                # Gate 3 — post-install boot-readiness (raises on brick). Inside
                # the sentinel so an unbootable result blocks the next run for
                # recovery. Keyed on the *installed* name: an FDO use-build is
                # renamed to <pkgname>-sysforge, so /boot/vmlinuz-<that> is what
                # must exist.
                if not options.dry_run:
                    _gate3_verify(pkgbuild.parent, fdo_eff_pkgname, bootloader)

        _log.info(f"Kernel stage complete: {fdo_eff_pkgname}")
