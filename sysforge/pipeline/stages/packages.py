# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/packages.py — stage 7: package builds (bootstrap context)

This is the bootstrap leg of the packages.toml dual role (see
DESIGN.md §Package Manifest): on a fresh system with only the pacstrap base
installed, every manifest entry is treated as an install target. At
steady-state (`sysforge update`, `sysforge build`), the same file behaves as
build-rule overrides — that path lives in update.py and does not iterate
the manifest.

Walks packages.toml and builds/installs each entry:
  source = "repo", effective_mode = "pacman"           → pacman -S --needed <name>
  source = "repo", effective_mode = "build_from_source" → locate PKGBUILD, build like AUR
  source = "aur"                                        → locate PKGBUILD via find_pkgbuild(), call makepkg_wrapper.run()
  source = "git"                                        → same as aur

Effective build mode for repo packages:
  global [build] repo_mode = "pacman" | "build_from_source"  (default "pacman")
  per-package enable_build_from_source = true  →  forces "build_from_source" regardless of global

Per-package checkpointing: state is written after every outcome (built/failed).
On resume with failed packages the user is prompted (or --force-retry bypasses).

packages.toml search order:
  1. config["packages_file"]  (from --packages or profiles.toml)
  2. /etc/sysforge/packages.toml  (system default)

AUR/git package PKGBUILD lookup order (via find_pkgbuild):
  1. packages.toml [build] pkgbuild_src_dir/<name>/PKGBUILD  (if set)
  2. profiles.toml [paths] pkgbuild_src_dir/<name>/PKGBUILD  (if set)
  3. AUR clone into pkgbuild_src_dir if package is found on AUR
"""
import subprocess
import tomllib
from sysforge import log
_log = log.get_logger("PACKAGES")
from pathlib import Path

from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.config import (
    expand_package_groups,
    find_pkgbuild,
    resolve_pkgbuild_src_dir,
    resolve_repo_mode,
    REPO_MODE_PACMAN,
    REPO_MODE_SOURCE,
    PKG_KEY_BUILD_FROM_SOURCE,
)
from sysforge.primitives.paths import PACKAGES_PATH, resolve_packages_path
from sysforge.primitives.makepkg_wrapper import run as makepkg_run
from sysforge.build_core import make_build_options
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.stage_sentinel import sentinel_scope


# ---------------------------------------------------------------------------
# packages.toml loading
# ---------------------------------------------------------------------------

def _load_packages(config):
    """
    Load packages.toml. Path resolved from config["packages_file"] or
    relative to the installed package configs dir.
    Returns (build_config dict, list of package dicts).
    """
    path = resolve_packages_path(config)

    if not path.exists():
        raise RuntimeError(
            f"[PACKAGES] packages.toml not found at {path}. "
            f"Pass --packages or place packages.toml at {PACKAGES_PATH}."
        )

    with open(path, "rb") as f:
        data = tomllib.load(f)

    build_cfg = data.get("build", {})
    packages = expand_package_groups(data)

    # resolve_repo_mode maps the legacy "profiled" token to "build_from_source"
    # so old configs keep working; validate the resolved (current) vocabulary.
    repo_mode = resolve_repo_mode(build_cfg)
    if repo_mode not in (REPO_MODE_PACMAN, REPO_MODE_SOURCE):
        raise ValueError(
            f"[PACKAGES] Invalid [build] repo_mode={build_cfg.get('repo_mode')!r} "
            f"in {path}. Must be {REPO_MODE_PACMAN!r} or {REPO_MODE_SOURCE!r}."
        )

    _log.info(f"Loaded {len(packages)} package(s) from {path}")
    return build_cfg, packages


def _resolve_pkgbuild(name, build_cfg, config):
    """
    Resolve the PKGBUILD path for an aur/git package.

    Builds a lookup config for find_pkgbuild using packages.toml [build]
    pkgbuild_src_dir first, falling back to flag_profiles [paths] pkgbuild_src_dir.
    find_pkgbuild handles cwd lookup, local dir lookup, and AUR auto-clone.

    Raises RuntimeError (wrapping FileNotFoundError) if nothing is found.
    """
    pkgbuild_src_dir = resolve_pkgbuild_src_dir(config, build_cfg)
    lookup_config = {"paths": {"pkgbuild_src_dir": pkgbuild_src_dir}} if pkgbuild_src_dir else None
    try:
        return find_pkgbuild(name, lookup_config)
    except FileNotFoundError as e:
        raise RuntimeError(f"[PACKAGES] {e}") from None


# ---------------------------------------------------------------------------
# Resume prompt
# ---------------------------------------------------------------------------

def _prompt_failed_packages(failed_names, errors, options):
    """
    Prompt the user about how to handle failed packages on resume.
    Returns (retry_set, skip_set) — sets of package names.

    With --force-retry: retry all without prompt.
    """
    if options.force_retry:
        _log.info(f"--force-retry: retrying all {len(failed_names)} failed package(s)")
        return set(failed_names), set()

    _log.warn(f"Resuming with {len(failed_names)} failed package(s):")
    for name in failed_names:
        err = errors.get(name, "unknown error")
        _log.warn(f"  - {name}  ({err})")

    _log.warn("\nOptions:\n  [r] Retry all failed\n  [s] Skip all failed\n  [c] Choose per package\n  [a] Abort")

    choice = prompt_choice(
        "Choice [r/s/c/a]: ",
        choices=("r", "s", "c", "a"),
        eof_default="a",
        tag="PACKAGES",
    )
    if choice == "r":
        return set(failed_names), set()
    if choice == "s":
        return set(), set(failed_names)
    if choice == "a":
        raise RuntimeError("[PACKAGES] Aborted by user at failed-package prompt")

    # choice == "c": choose per package
    retry, skip = set(), set()
    for name in failed_names:
        err = errors.get(name, "unknown error")
        ans = prompt_choice(
            f"  {name} ({err}) — [r]etry / [s]kip / [a]bort: ",
            choices=("r", "s", "a"),
            eof_default="a",
            tag="PACKAGES",
        )
        if ans == "r":
            retry.add(name)
        elif ans == "s":
            skip.add(name)
        else:  # "a"
            raise RuntimeError("[PACKAGES] Aborted by user")
    return retry, skip


# ---------------------------------------------------------------------------
# Individual package installers
# ---------------------------------------------------------------------------

def _enable_display_managers(packages, built, *, dry_run):
    """Enable the display-manager service for each installed desktop group.

    ``packages`` is the expanded manifest (``expand_package_groups`` output);
    each desktop-group member carries ``group="<de_key>"``. For every distinct
    desktop group whose display-manager package actually built, enable
    ``<dm>.service`` so the system boots into a graphical login instead of a
    TTY. Installing the DM package does NOT enable its unit on Arch, so without
    this the desktop selection lands the user at a console.

    The DM lookup is the single home in :mod:`pkg_catalog`; we never hardcode
    a DE→DM mapping here. Enabling is best-effort and idempotent (``systemctl
    enable`` on an already-enabled unit is a no-op); it takes effect on the
    next boot. We never ``--now`` start it — the install may be running headless
    over SSH.
    """
    from sysforge.primitives.pkg_catalog import display_manager_for

    enabled: set[str] = set()
    for entry in packages:
        group = entry.get("group")
        if not group:
            continue
        dm = display_manager_for(group)
        if not dm or dm in enabled or dm not in built:
            continue
        enabled.add(dm)
        if dry_run:
            _log.ui(f"[dry-run] would enable display manager: {dm}.service")
            continue
        result = subprocess.run(["sudo", "systemctl", "enable", f"{dm}.service"])
        if result.returncode != 0:
            _log.warn(f"Could not enable {dm}.service (enable it manually to boot into the desktop)")
        else:
            _log.ui(f"Display manager enabled: {dm}.service (starts on next boot)")


def _install_repo(pkg, options):
    """Install a repo package via sudo pacman -S --needed."""
    name = pkg["name"]
    if options.dry_run:
        _log.ui(f"[dry-run] sudo pacman -S --needed {name}")
        return
    _log.info(f"Installing from repo: {name}")
    result = subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm", name])
    if result.returncode != 0:
        raise RuntimeError(f"pacman -S failed for {name!r} (exit {result.returncode})")


def _toolchain_overrides(state):
    """
    Read cc/cxx/ld and the toolchain variant from the toolchain stage result.
    Returns a dict with only the keys that were set (empty dict if toolchain
    stage was not run or produced no result).

    ``variant`` (when present) flows into BuildOptions.toolchain_variant so
    every package built by this stage stamps the active toolchain identity
    into build_state — ``sysforge update`` reads it back to flag drift.
    """
    result = state.get_stage_result("toolchain")
    overrides = {}
    if result.get("cc"):
        overrides["cc_override"] = result["cc"]
    if result.get("cxx"):
        overrides["cxx_override"] = result["cxx"]
    if result.get("ld"):
        overrides["ld_override"] = result["ld"]
    if result.get("variant"):
        overrides["variant"] = result["variant"]
    return overrides


def _build_aur(pkg, build_cfg, config, options, toolchain):
    """Build an AUR/git package via makepkg_wrapper.run()."""
    name = pkg["name"]
    if options.dry_run:
        pkgbuild_src_dir = (
            build_cfg.get("pkgbuild_src_dir")
            or config.get("paths", {}).get("pkgbuild_src_dir", "")
        )
        expected = Path(pkgbuild_src_dir).expanduser() / name / "PKGBUILD" if pkgbuild_src_dir else f"<pkgbuild_src_dir>/{name}/PKGBUILD"
        parts = []
        if toolchain.get("cc_override"):
            parts.append("cc=" + toolchain["cc_override"])
        suffix = f" ({', '.join(parts)})" if parts else ""
        _log.ui(f"[dry-run] build {name} from {expected}{suffix}")
        return
    pkgbuild = _resolve_pkgbuild(name, build_cfg, config)
    parts = []
    if toolchain:
        parts.append("cc=" + toolchain.get("cc_override", ""))
    suffix = f" ({', '.join(p for p in parts if p)})" if parts else ""
    _log.info(f"Building {name} from {pkgbuild}{suffix}")
    build_opts = make_build_options(
        "packages", options,
        log_dir=options.log_dir,
        profile_conf=config.get("profile_conf"),
        update=not options.no_update,
        cc_override=toolchain.get("cc_override"),
        cxx_override=toolchain.get("cxx_override"),
        ld_override=toolchain.get("ld_override"),
        source=pkg.get("source"),
        toolchain_variant=toolchain.get("variant"),
    )
    makepkg_run(pkgbuild, options=build_opts)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class PackagesStage(Stage):
    name = "packages"
    description = "Build and install packages from packages.toml"
    depends_on = ["toolchain"]
    makepkg_bearing = True

    def run(self, config, state, options):
        toolchain = _toolchain_overrides(state)
        if toolchain:
            _log.info(f"Toolchain override from pipeline: cc={toolchain.get('cc_override', '-')} cxx={toolchain.get('cxx_override', '-')}")

        build_cfg, packages = _load_packages(config)

        # Build ordered name list and initialise progress (idempotent on resume)
        all_names = [p["name"] for p in packages]
        pkg_map = {p["name"]: p for p in packages}
        state.init_package_list(all_names)
        state.save()

        progress = state.get_package_progress()
        failed = progress.get("failed", [])
        built = set(progress.get("built", []))
        skipped = set(progress.get("skipped", []))

        # On resume: prompt about failed packages
        retry_set = set()
        skip_set = set()
        if failed:
            errors = state.get_package_errors()
            retry_set, skip_set = _prompt_failed_packages(failed, errors, options)
            for name in skip_set:
                state.mark_package_skipped(name)
            # Move retried packages back to remaining so the main loop picks them up
            for name in retry_set:
                progress = state.get_package_progress()
                if name not in progress.get("remaining", []):
                    state._progress()["remaining"].append(name)
                state._progress()["failed"] = [
                    n for n in state._progress().get("failed", []) if n != name
                ]
            state.save()

        # Resolve and build AUR deps for all AUR/profiled packages
        from sysforge.primitives.aur_resolve import (
            resolve_aur_deps,
            build_resolved_deps,
        )
        aur_names = []
        for name in all_names:
            if name in built or name in skipped or name in skip_set:
                continue
            pkg = pkg_map[name]
            source = pkg.get("source", "aur")
            rm = resolve_repo_mode(build_cfg)
            effective = REPO_MODE_SOURCE if pkg.get(PKG_KEY_BUILD_FROM_SOURCE) else rm
            # `local` source has no remote AUR deps to resolve — its
            # build-time deps come from the local PKGBUILD's depends=()
            # which makepkg handles directly. Group only aur/git/source-built-repo
            # for the AUR-dep resolution pass.
            if source in ("aur", "git") or (source == "repo" and effective == REPO_MODE_SOURCE):
                aur_names.append(name)

        # Stage-level sentinel covers the AUR-dep build and the per-package
        # install loop. Interruption inside either path leaves the sentinel
        # in place so the next sysforge invocation blocks at the CLI-entry
        # recovery prompt. No recovery_cmd is recorded — there's no single
        # shell command that restores a partially-installed package set;
        # the operator verifies (pacman -Dk / pacman -Qkk) and re-runs
        # `sysforge run packages` to retry/skip failed entries.
        n_remaining = sum(
            1 for n in all_names
            if n not in built and n not in skipped and n not in skip_set
        )
        with sentinel_scope(
            options.state_dir,
            "packages",
            retry_cmd="sysforge run packages",
            remaining=n_remaining,
        ):
            if aur_names and not options.dry_run:
                all_aur_deps = []
                for name in aur_names:
                    try:
                        pkgbuild = _resolve_pkgbuild(name, build_cfg, config)
                        deps = resolve_aur_deps(pkgbuild, config, fetch=True)
                        # Filter out packages already in the manifest (they'll be built in order)
                        deps = [d for d in deps if d.name not in pkg_map]
                        all_aur_deps.extend(deps)
                    except RuntimeError as e:
                        _log.warn(f"{name}: dep resolution failed ({e}), will attempt build anyway")

                # De-duplicate by name, keeping first occurrence (topo order)
                seen_deps: set[str] = set()
                unique_deps = []
                for dep in all_aur_deps:
                    if dep.name not in seen_deps:
                        seen_deps.add(dep.name)
                        unique_deps.append(dep)

                if unique_deps:
                    build_resolved_deps(
                        unique_deps,
                        profile_conf=config.get("profile_conf"),
                        cc_override=toolchain.get("cc_override"),
                        cxx_override=toolchain.get("cxx_override"),
                        ld_override=toolchain.get("ld_override"),
                    )

            # Main build loop — walk all_names in manifest order
            from sysforge.ui import progress as _ui_progress
            with _ui_progress.tracker(len(all_names), "packages") as _tick:
                for name in all_names:
                    progress = state.get_package_progress()

                    pkg = pkg_map[name]
                    source = pkg.get("source", "aur")

                    # Effective build mode for repo packages:
                    # global repo_mode default, overridden by per-package
                    # enable_build_from_source.
                    repo_mode = resolve_repo_mode(build_cfg)
                    effective_mode = REPO_MODE_SOURCE if pkg.get(PKG_KEY_BUILD_FROM_SOURCE) else repo_mode

                    # Narrate the actual action, not a blanket "building": repo
                    # packages in pacman mode are installed with `pacman -S`, so
                    # showing a build progress line for e.g. gnome-shell (a
                    # pacman install) is misleading. The verb must track the
                    # branch taken below.
                    building_from_source = (
                        source in ("aur", "git", "local")
                        or (source == "repo" and effective_mode == REPO_MODE_SOURCE)
                    )
                    verb = "building" if building_from_source else "installing"
                    _tick(f"{verb} {name}")

                    if name in built and name not in retry_set:
                        _log.info(f"Skipping {name} (already built)")
                        continue
                    if name in skipped or name in skip_set:
                        _log.info(f"Skipping {name} (user skipped)")
                        continue
                    if name not in progress.get("remaining", []) and name not in retry_set:
                        # Was already handled (built or skipped) in a prior run
                        continue

                    state.mark_package_building(name)
                    state.save()

                    try:
                        if source == "repo" and effective_mode == REPO_MODE_SOURCE:
                            _log.info(f"{name}: repo source with build-from-source mode — building from source")
                            _build_aur(pkg, build_cfg, config, options, toolchain)
                        elif source == "repo":
                            _install_repo(pkg, options)
                        elif source in ("aur", "git", "local"):
                            # `local` shares the build path with aur/git — the
                            # PKGBUILD already lives in pkgbuild_src_dir and no
                            # source sync is performed for it.
                            _build_aur(pkg, build_cfg, config, options, toolchain)
                        else:
                            raise RuntimeError(f"Unknown source type {source!r} for {name!r}")

                        state.mark_package_built(name)
                        state.save()
                        built.add(name)
                        _log.info(f"{name}: done")

                    except RuntimeError as e:
                        state.mark_package_failed(name, str(e))
                        state.save()
                        _log.error(f"{name}: FAILED — {e}")
                        # Non-fatal: continue with remaining packages

        # Enable the display-manager service for any desktop group that just
        # installed, so the system boots into a graphical login rather than a
        # TTY. Runs outside the sentinel scope (cosmetic — a failure here never
        # blocks the next invocation).
        final_built = set(state.get_package_progress().get("built", []))
        _enable_display_managers(packages, final_built, dry_run=options.dry_run)

        # Check if any packages are still failed after the loop
        final = state.get_package_progress()
        still_failed = final.get("failed", [])
        n_built   = len(final.get("built", []))
        n_failed  = len(still_failed)
        n_skipped = len(final.get("skipped", []))
        n_total   = n_built + n_failed + n_skipped

        summary = (
            f"Total: {n_total}  |  "
            f"Built: {n_built}  |  "
            f"Failed: {n_failed}  |  "
            f"Skipped: {n_skipped}"
        )

        if still_failed:
            _log.error(f"Stage complete with failures.\n[SYSFORGE][ERROR][PACKAGES] {summary}\n[SYSFORGE][ERROR][PACKAGES] Failed packages: {still_failed}")
            raise RuntimeError(
                f"[PACKAGES] stage finished with failures: {still_failed}"
            )

        _log.ui(f"Stage complete. {summary}")
