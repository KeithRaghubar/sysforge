# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/source.py — getting the right kernel source tree, safely.

Locating and syncing the PKGBUILD tree, plus the checks that must pass before
anything is built from it: that the PKGBUILD's pkgname matches what the config
asked for, and that a custom pkgname does not collide with a repo package.

The collision check matters more here than elsewhere in sysforge. A kernel
package that shadows a repo name can be replaced by ``pacman -Syu`` without
warning, and discovering that after a reboot is how a machine stops booting.
"""
from pathlib import Path
import subprocess

from sysforge.primitives.config import load_sysforge_toml
from sysforge.primitives.config import resolve_repo_track
from sysforge.primitives.source_sync import STATUS_DIVERGED
from sysforge.primitives.source_sync import SyncRequest
from sysforge.primitives.source_sync import get_scheduler

from sysforge.pipeline.stages.kernel import constants

from sysforge import log

_log = log.get_logger("KERNEL")



def probe_installed_bootloader():
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


def validate_pkgname_matches_pkgbuild(pkgbuild_path, expected_pkgname):
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


def check_pkgname_repo_collision(pkgname, options):
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
        # Dry-run verdict, not narration: the real run prompts/aborts here,
        # so this line IS the answer at the shipped default (3.1.0-B6).
        _log.ui(f"{msg} (dry-run: not prompting)")
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


def resolve_already_built_action(options, interactive):
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

def presync_kernel_source(pkgbuild_dir, options, state_dir, source="local"):
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
    if result.status in constants.SYNC_BLOCKING_STATUSES:
        raise RuntimeError(
            f"[KERNEL] source sync failed for {pkgbuild_dir.name}: "
            f"{result.error or result.status}"
        )
    if result.status == STATUS_DIVERGED:
        warn_and_confirm_diverged(pkgbuild_dir, options)
    return True


def warn_and_confirm_diverged(pkgbuild_dir, options):
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
