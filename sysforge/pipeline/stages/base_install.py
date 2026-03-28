"""
stages/base_install.py — stage 2: base system install

Installs a minimal Arch base system to the target mount point using pacstrap,
then generates an fstab.

The package list is deliberately minimal — just enough to boot and connect to
the network. Everything else (compiler, desktop, utilities) is installed by the
packages stage (stage 7) from packages.toml.

Reads target from /etc/sysforge/bootstrap.toml.
"""

import subprocess
from pathlib import Path

from sysforge import log
_log = log.get_logger("BASE_INSTALL")
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import load_bootstrap


# Minimal packages installed via pacstrap.
# - base-devel:       build toolchain meta-package (make, gcc, fakeroot, binutils, etc.); required for makepkg
# - base:             core userspace (glibc, bash, coreutils, systemd, pacman, ...)
# - git:              required for cloning PKGBUILDs and sysforge itself
# - linux-firmware:   hardware firmware blobs
# - linux:            default Arch kernel (replaced by custom kernel stage if configured)
# - networkmanager:   network management daemon (needed for post-boot connectivity)
# - openssh:          SSH server/client (required for remote access)
# - python:           required by sysforge itself
# - reflector:        mirror ranking tool; run during configure stage to select fastest pacman mirrors
# - sudo:             privilege escalation for the build user
# - uv:               Python package installer (required by sysforge PKGBUILD)
_BASE_PACKAGES = [
    "base",
    "base-devel",
    "bash-completion",
    "git",
    "linux",
    "linux-firmware",
    "networkmanager",
    "openssh",
    "python",
    "reflector",
    "sudo",
    "uv",
]


def _verify_target_mounted(target: str) -> None:
    """Raise RuntimeError if target doesn't look like a mounted filesystem."""
    target_path = Path(target)
    if not target_path.is_dir():
        raise RuntimeError(
            f"[BASE_INSTALL] Target {target!r} does not exist or is not a directory. "
            f"Run the partition stage first to partition and mount the disk."
        )

    # Check that something is actually mounted there
    result = subprocess.run(
        ["findmnt", "--noheadings", "--target", target],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"[BASE_INSTALL] Nothing appears to be mounted at {target!r}. "
            f"Run the partition stage first to partition and mount the disk."
        )


def _run_pacstrap(target: str, packages: list[str], dry_run: bool) -> None:
    cmd = ["pacstrap", "-K", target] + packages
    _log.ui(f"pacstrap: {' '.join(packages)}")
    if dry_run:
        _log.ui(f"[dry-run] would run: {' '.join(cmd)}")
        return
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            f"[BASE_INSTALL] pacstrap failed (exit {result.returncode}). "
            f"Check network connectivity and pacman keyring."
        )
    _log.ui("pacstrap complete.")


def _generate_fstab(target: str, dry_run: bool) -> None:
    fstab_path = Path(target) / "etc/fstab"
    cmd = ["genfstab", "-U", target]
    _log.ui(f"Generating fstab: {fstab_path}")

    if dry_run:
        _log.ui(f"[dry-run] would run: {' '.join(cmd)} >> {fstab_path}")
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"[BASE_INSTALL] genfstab failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    fstab_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fstab_path, "a") as f:
        f.write(result.stdout)

    _log.ui("fstab written.")


class BaseInstallStage(Stage):
    name = "base_install"
    description = "Base system install — pacstrap + fstab"
    depends_on = ["partition"]

    def run(self, config, state, options):  # noqa: ARG002
        cfg = load_bootstrap()

        packages = list(_BASE_PACKAGES)
        if cfg.shell == "zsh":
            packages.extend(["zsh", "zsh-completions"])

        _log.ui(f"Installing base system to {cfg.target}")
        _log.ui(f"Packages: {', '.join(packages)}")

        if not options.dry_run:
            _verify_target_mounted(cfg.target)

        _run_pacstrap(cfg.target, packages, options.dry_run)
        _generate_fstab(cfg.target, options.dry_run)

        _log.ui("Base install complete.")
