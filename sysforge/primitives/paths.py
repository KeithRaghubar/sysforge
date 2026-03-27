"""
paths.py — centralised path constants for sysforge config files

All /etc/sysforge/* paths live here.  CONFIG_BASE is derived from the
SYSFORGE_CONFIG_DIR env var (default: "/"), allowing tests and
alternate installs to relocate config lookup.
"""
import os
from pathlib import Path

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

# flag_profiles.toml search order (user, then system)
CONFIG_PATHS = [
    Path.home() / ".config/sysforge/flag_profiles.toml",
    CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
]

CONFLICT_GROUP_PATHS = [
    Path.home() / ".config/sysforge/append_conflict_groups.toml",
    CONFIG_BASE / "etc/sysforge/append_conflict_groups.toml",
]

CONSUMES_INFERENCE_PATHS = [
    Path.home() / ".config/sysforge/consumes_inference.toml",
    CONFIG_BASE / "etc/sysforge/consumes_inference.toml",
]

# Individual config files
PACKAGES_PATH = CONFIG_BASE / "etc/sysforge/packages.toml"
KERNEL_PATH = CONFIG_BASE / "etc/sysforge/kernel.toml"
TOOLCHAIN_PATH = CONFIG_BASE / "etc/sysforge/toolchain.toml"
SYSFORGE_TOML_PATH = CONFIG_BASE / "etc/sysforge/sysforge.toml"
BOOTSTRAP_PATH = Path("/etc/sysforge/bootstrap.toml")


def resolve_packages_path(config: dict) -> Path:
    """Resolve packages.toml from config override or default PACKAGES_PATH."""
    raw = config.get("packages_file")
    if raw:
        return Path(raw).expanduser()
    return PACKAGES_PATH
