"""
stages/packages.py — stage 5: package builds

Walks packages.toml and builds/installs each entry:
  source = "repo"  → pacman -S --needed <name> (no build, pacman owns it)
  source = "aur"   → find PKGBUILD in pkgbuild_dir, call makepkg_wrapper.run()
  source = "git"   → same as aur

Per-package checkpointing: state is written after every outcome (built/failed).
On resume with failed packages the user is prompted (or --force-retry bypasses).

packages.toml is read from the path in config["packages_file"], or
configs/packages.toml relative to the repo root as a fallback.

AUR/git packages require PKGBUILDs to be pre-cloned in packages.toml [build]
pkgbuild_dir. AUR fetch (auto-clone) is V2.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

from sysforge.pipeline.stages.base import Stage
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
        # Fallback: look relative to this file (repo layout)
        here = Path(__file__).parent
        path = here.parent.parent.parent / "configs" / "packages.toml"

    if not path.exists():
        raise RuntimeError(
            f"[PACKAGES] packages.toml not found at {path}. "
            f"Set packages_file in flag_profiles.toml or pass --packages."
        )

    with open(path, "rb") as f:
        data = tomllib.load(f)

    build_cfg = data.get("build", {})
    packages = data.get("package", [])
    print(f"[PACKAGES] Loaded {len(packages)} package(s) from {path}")
    return build_cfg, packages


def _pkgbuild_path(pkg, build_cfg):
    """
    Resolve the PKGBUILD path for an aur/git package.
    Returns Path or raises RuntimeError if not found.
    """
    pkgbuild_dir = build_cfg.get("pkgbuild_dir")
    if not pkgbuild_dir:
        raise RuntimeError(
            f"[PACKAGES] packages.toml [build] pkgbuild_dir is not set. "
            f"Cannot locate PKGBUILD for {pkg['name']!r}."
        )
    pkgbuild_dir = Path(pkgbuild_dir).expanduser()
    candidate = pkgbuild_dir / pkg["name"] / "PKGBUILD"
    if not candidate.exists():
        raise RuntimeError(
            f"[PACKAGES] PKGBUILD not found: {candidate}. "
            f"Clone {pkg['name']!r} into pkgbuild_dir first."
        )
    return candidate


# ---------------------------------------------------------------------------
# Hardware gate
# ---------------------------------------------------------------------------

def _hardware_gate(pkg, config):
    """
    Return True if the package should be built on this machine.
    Checks requires_hardware against hardware_profile.toml if present.
    Missing hardware_profile means all packages without requires_hardware pass.
    """
    required = pkg.get("requires_hardware")
    if not required:
        return True

    hw_path = config.get("hardware_profile")
    if not hw_path:
        # No hardware profile available — skip hardware-gated packages
        print(
            f"[PACKAGES] Skipping {pkg['name']!r}: requires_hardware={required!r} "
            f"but no hardware_profile configured"
        )
        return False

    hw_path = Path(hw_path).expanduser()
    if not hw_path.exists():
        print(
            f"[PACKAGES] Skipping {pkg['name']!r}: requires_hardware={required!r} "
            f"but {hw_path} does not exist"
        )
        return False

    with open(hw_path, "rb") as f:
        hw = tomllib.load(f)

    if not hw.get(required):
        print(
            f"[PACKAGES] Skipping {pkg['name']!r}: requires_hardware={required!r} "
            f"not present in hardware profile"
        )
        return False

    return True


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
        print(f"[PACKAGES] --force-retry: retrying all {len(failed_names)} failed package(s)")
        return set(failed_names), set()

    print(f"\n[PACKAGES] Resuming with {len(failed_names)} failed package(s):")
    for name in failed_names:
        err = errors.get(name, "unknown error")
        print(f"  - {name}  ({err})")

    print(
        "\nOptions:\n"
        "  [r] Retry all failed\n"
        "  [s] Skip all failed (mark as skipped, continue)\n"
        "  [c] Choose per package\n"
        "  [a] Abort\n"
    )

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
            print("  Please enter r, s, c, or a.")


# ---------------------------------------------------------------------------
# Individual package installers
# ---------------------------------------------------------------------------

def _install_repo(pkg, options):
    """Install a repo package via pacman -S --needed."""
    name = pkg["name"]
    if options.dry_run:
        print(f"[PACKAGES] [dry-run] pacman -S --needed {name}")
        return
    print(f"[PACKAGES] Installing from repo: {name}")
    result = subprocess.run(["pacman", "-S", "--needed", "--noconfirm", name])
    if result.returncode != 0:
        raise RuntimeError(f"pacman -S failed for {name!r} (exit {result.returncode})")


def _build_aur(pkg, build_cfg, options):
    """Build an AUR/git package via makepkg_wrapper.run()."""
    name = pkg["name"]
    pkgbuild = _pkgbuild_path(pkg, build_cfg)
    if options.dry_run:
        print(f"[PACKAGES] [dry-run] build {name} from {pkgbuild}")
        return
    print(f"[PACKAGES] Building {name} from {pkgbuild}")
    makepkg_run(pkgbuild)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class PackagesStage(Stage):
    name = "packages"
    description = "Build and install packages from packages.toml"
    depends_on = ["toolchain"]

    def run(self, config, state, options):
        build_cfg, packages = _load_packages(config)

        # Filter hardware-gated packages
        eligible = [p for p in packages if _hardware_gate(p, config)]
        if len(eligible) < len(packages):
            print(
                f"[PACKAGES] {len(packages) - len(eligible)} package(s) excluded "
                f"by hardware gate"
            )

        # Build ordered name list and initialise progress (idempotent on resume)
        all_names = [p["name"] for p in eligible]
        pkg_map = {p["name"]: p for p in eligible}
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
                print(f"[PACKAGES] Skipping {name} (already built)")
                continue
            if name in skipped or name in skip_set:
                print(f"[PACKAGES] Skipping {name} (user skipped)")
                continue
            if name not in progress.get("remaining", []) and name not in retry_set:
                # Was already handled (built or skipped) in a prior run
                continue

            pkg = pkg_map[name]
            source = pkg.get("source", "aur")

            state.mark_package_building(name)
            state.save()

            try:
                if source == "repo":
                    _install_repo(pkg, options)
                elif source in ("aur", "git"):
                    _build_aur(pkg, build_cfg, options)
                else:
                    raise RuntimeError(f"Unknown source type {source!r} for {name!r}")

                state.mark_package_built(name)
                state.save()
                built.add(name)
                print(f"[PACKAGES] {name}: done")

            except RuntimeError as e:
                state.mark_package_failed(name, str(e))
                state.save()
                print(f"[PACKAGES] {name}: FAILED — {e}", file=sys.stderr)
                # Non-fatal: continue with remaining packages

        # Check if any packages are still failed after the loop
        final = state.get_package_progress()
        still_failed = final.get("failed", [])
        if still_failed:
            print(
                f"\n[PACKAGES] Stage complete with {len(still_failed)} failed package(s): "
                f"{still_failed}",
                file=sys.stderr,
            )
            raise RuntimeError(
                f"packages stage finished with failures: {still_failed}"
            )

        print(
            f"[PACKAGES] All packages done. "
            f"Built: {len(final.get('built', []))}, "
            f"Skipped: {len(final.get('skipped', []))}"
        )
