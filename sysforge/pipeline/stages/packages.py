"""
stages/packages.py — stage 7: package builds

Walks packages.toml and builds/installs each entry:
  source = "repo", effective_mode = "pacman"   → pacman -S --needed <name>
  source = "repo", effective_mode = "profiled" → locate PKGBUILD, build like AUR
  source = "aur"                               → locate PKGBUILD via find_pkgbuild(), call makepkg_wrapper.run()
  source = "git"                               → same as aur

Effective build mode for repo packages:
  global [build] repo_mode = "pacman" | "profiled"  (default "pacman")
  per-package pkgbuild_patch = true  →  forces "profiled" regardless of global

Per-package checkpointing: state is written after every outcome (built/failed).
On resume with failed packages the user is prompted (or --force-retry bypasses).

packages.toml search order:
  1. config["packages_file"]  (from --packages or flag_profiles.toml)
  2. /etc/sysforge/packages.toml  (system default)

AUR/git package PKGBUILD lookup order (via find_pkgbuild):
  1. packages.toml [build] pkgbuild_dir/<name>/PKGBUILD  (if set)
  2. flag_profiles.toml [paths] pkgbuild_dir/<name>/PKGBUILD  (if set)
  3. AUR clone into pkgbuild_dir if package is found on AUR
"""
import subprocess
import tomllib
import sysforge.log as _log
from pathlib import Path

from sysforge.pipeline.stages.base import Stage
from sysforge.primitives.config import find_pkgbuild, PACKAGES_PATH
from sysforge.primitives.makepkg_wrapper import run as makepkg_run


# ---------------------------------------------------------------------------
# packages.toml loading
# ---------------------------------------------------------------------------

def _load_packages(config):
    """
    Load packages.toml. Path resolved from config["packages_file"] or
    relative to the installed package configs dir.
    Returns (build_config dict, list of package dicts).
    """
    path = config.get("packages_file")
    if path:
        path = Path(path)
    else:
        path = PACKAGES_PATH

    if not path.exists():
        raise RuntimeError(
            f"[PACKAGES] packages.toml not found at {path}. "
            f"Pass --packages or place packages.toml at {PACKAGES_PATH}."
        )

    with open(path, "rb") as f:
        data = tomllib.load(f)

    build_cfg = data.get("build", {})
    packages = data.get("package", [])

    repo_mode = build_cfg.get("repo_mode", "pacman")
    if repo_mode not in ("pacman", "profiled"):
        raise ValueError(
            f"[PACKAGES] Invalid [build] repo_mode={repo_mode!r} in {path}. "
            f"Must be 'pacman' or 'profiled'."
        )

    _log.ui("[PACKAGES]", f"Loaded {len(packages)} package(s) from {path}")
    return build_cfg, packages


def _resolve_pkgbuild(name, build_cfg, config):
    """
    Resolve the PKGBUILD path for an aur/git package.

    Builds a lookup config for find_pkgbuild using packages.toml [build]
    pkgbuild_dir first, falling back to flag_profiles [paths] pkgbuild_dir.
    find_pkgbuild handles cwd lookup, local dir lookup, and AUR auto-clone.

    Raises RuntimeError (wrapping FileNotFoundError) if nothing is found.
    """
    pkgbuild_dir = (
        build_cfg.get("pkgbuild_dir")
        or config.get("paths", {}).get("pkgbuild_dir")
    )
    lookup_config = {"paths": {"pkgbuild_dir": pkgbuild_dir}} if pkgbuild_dir else None
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
        _log.ui("[PACKAGES]", f"--force-retry: retrying all {len(failed_names)} failed package(s)")
        return set(failed_names), set()

    _log.warn("[PACKAGES]", f"Resuming with {len(failed_names)} failed package(s):")
    for name in failed_names:
        err = errors.get(name, "unknown error")
        _log.warn("[PACKAGES]", f"  - {name}  ({err})")

    _log.warn("[PACKAGES]", "\nOptions:\n  [r] Retry all failed\n  [s] Skip all failed\n  [c] Choose per package\n  [a] Abort")

    while True:
        choice = input("Choice [r/s/c/a]: ").strip().lower()
        if choice == "r":
            return set(failed_names), set()
        elif choice == "s":
            return set(), set(failed_names)
        elif choice == "a":
            raise RuntimeError("[PACKAGES] Aborted by user at failed-package prompt")
        elif choice == "c":
            retry, skip = set(), set()
            for name in failed_names:
                err = errors.get(name, "unknown error")
                while True:
                    ans = input(f"  {name} ({err}) — [r]etry / [s]kip / [a]bort: ").strip().lower()
                    if ans == "r":
                        retry.add(name)
                        break
                    elif ans == "s":
                        skip.add(name)
                        break
                    elif ans == "a":
                        raise RuntimeError("[PACKAGES] Aborted by user")
            return retry, skip
        else:
            _log.warn("[PACKAGES]", "  Please enter r, s, c, or a.")


# ---------------------------------------------------------------------------
# Individual package installers
# ---------------------------------------------------------------------------

def _install_repo(pkg, options):
    """Install a repo package via sudo pacman -S --needed."""
    name = pkg["name"]
    if options.dry_run:
        _log.ui("[PACKAGES]", f"[dry-run] sudo pacman -S --needed {name}")
        return
    _log.ui("[PACKAGES]", f"Installing from repo: {name}")
    result = subprocess.run(["sudo", "pacman", "-S", "--needed", "--noconfirm", name])
    if result.returncode != 0:
        raise RuntimeError(f"pacman -S failed for {name!r} (exit {result.returncode})")


def _toolchain_overrides(state):
    """
    Read cc/cxx/ld from the toolchain stage result in state.
    Returns a dict with only the keys that were set (empty dict if toolchain
    stage was not run or produced no result).
    """
    result = state.get_stage_result("toolchain")
    overrides = {}
    if result.get("cc"):
        overrides["cc_override"] = result["cc"]
    if result.get("cxx"):
        overrides["cxx_override"] = result["cxx"]
    if result.get("ld"):
        overrides["ld_override"] = result["ld"]
    return overrides


def _build_aur(pkg, build_cfg, config, options, toolchain):
    """Build an AUR/git package via makepkg_wrapper.run()."""
    name = pkg["name"]
    if options.dry_run:
        pkgbuild_dir = (
            build_cfg.get("pkgbuild_dir")
            or config.get("paths", {}).get("pkgbuild_dir", "")
        )
        expected = Path(pkgbuild_dir).expanduser() / name / "PKGBUILD" if pkgbuild_dir else f"<pkgbuild_dir>/{name}/PKGBUILD"
        parts = []
        if toolchain.get("cc_override"):
            parts.append("cc=" + toolchain["cc_override"])
        suffix = f" ({', '.join(parts)})" if parts else ""
        _log.ui("[PACKAGES]", f"[dry-run] build {name} from {expected}{suffix}")
        return
    pkgbuild = _resolve_pkgbuild(name, build_cfg, config)
    parts = []
    if toolchain:
        parts.append("cc=" + toolchain.get("cc_override", ""))
    suffix = f" ({', '.join(p for p in parts if p)})" if parts else ""
    _log.ui("[PACKAGES]", f"Building {name} from {pkgbuild}{suffix}")
    makepkg_run(pkgbuild,
                pkg_log=not options.no_pkg_logs,
                persist_log=options.persist_log,
                log_dir=options.log_dir,
                profile_conf=config.get("profile_conf"),
                update=not options.no_update,
                abi_check=options.abi_check,
                **toolchain)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class PackagesStage(Stage):
    name = "packages"
    description = "Build and install packages from packages.toml"
    depends_on = ["toolchain"]

    def run(self, config, state, options):
        toolchain = _toolchain_overrides(state)
        if toolchain:
            _log.ui("[PACKAGES]", f"Toolchain override from pipeline: cc={toolchain.get('cc_override', '-')} cxx={toolchain.get('cxx_override', '-')}")

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

        # Main build loop — walk all_names in manifest order
        for name in all_names:
            progress = state.get_package_progress()
            if name in built and name not in retry_set:
                _log.ui("[PACKAGES]", f"Skipping {name} (already built)")
                continue
            if name in skipped or name in skip_set:
                _log.ui("[PACKAGES]", f"Skipping {name} (user skipped)")
                continue
            if name not in progress.get("remaining", []) and name not in retry_set:
                # Was already handled (built or skipped) in a prior run
                continue

            pkg = pkg_map[name]
            source = pkg.get("source", "aur")

            # Effective build mode for repo packages:
            # global repo_mode default, overridden by per-package pkgbuild_patch.
            repo_mode = build_cfg.get("repo_mode", "pacman")
            effective_mode = "profiled" if pkg.get("pkgbuild_patch") else repo_mode

            state.mark_package_building(name)
            state.save()

            try:
                if source == "repo" and effective_mode == "profiled":
                    _log.ui("[PACKAGES]", f"{name}: repo source with profiled build mode — building from source")
                    _build_aur(pkg, build_cfg, config, options, toolchain)
                elif source == "repo":
                    _install_repo(pkg, options)
                elif source in ("aur", "git"):
                    _build_aur(pkg, build_cfg, config, options, toolchain)
                else:
                    raise RuntimeError(f"Unknown source type {source!r} for {name!r}")

                state.mark_package_built(name)
                state.save()
                built.add(name)
                _log.ui("[PACKAGES]", f"{name}: done")

            except RuntimeError as e:
                state.mark_package_failed(name, str(e))
                state.save()
                _log.error("[PACKAGES]", f"{name}: FAILED — {e}")
                # Non-fatal: continue with remaining packages

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
            _log.error("[PACKAGES]", f"Stage complete with failures.\n[SYSFORGE][ERROR][PACKAGES] {summary}\n[SYSFORGE][ERROR][PACKAGES] Failed packages: {still_failed}")
            raise RuntimeError(
                f"packages stage finished with failures: {still_failed}"
            )

        _log.ui("[PACKAGES]", f"Stage complete.\n[SYSFORGE][INFO][PACKAGES] {summary}")
