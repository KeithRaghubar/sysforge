"""
paths.py — centralised path constants for sysforge config files

All /etc/sysforge/* paths live here.  CONFIG_BASE is derived from the
SYSFORGE_CONFIG_DIR env var (default: "/"), allowing tests and
alternate installs to relocate config lookup.

User-side paths share a single root (~/.config/sysforge) for config
overrides, regenerable cache, and fallback runtime state.
"""
import os
import shutil
from pathlib import Path

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

USER_CONFIG_DIR = Path.home() / ".config/sysforge"
USER_CACHE_DIR  = USER_CONFIG_DIR / "cache"
USER_STATE_DIR  = USER_CONFIG_DIR / "state"

# profiles.toml search order (user, then system)
# Holds [paths] [defaults] [profiles.*] [[rules]] plus the consolidated
# [append_conflict_groups] and [consumes_inference] sections.
CONFIG_PATHS = [
    USER_CONFIG_DIR / "profiles.toml",
    CONFIG_BASE / "etc/sysforge/profiles.toml",
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


_LEGACY_USER_DIRS = (
    (Path.home() / ".cache/sysforge",       USER_CACHE_DIR),
    (Path.home() / ".local/state/sysforge", USER_STATE_DIR),
)


def migrate_legacy_user_dirs() -> None:
    """Best-effort one-shot move of ~/.cache/sysforge and ~/.local/state/sysforge
    into ~/.config/sysforge/{cache,state}. Idempotent; never raises."""
    from sysforge import log
    _log = log.get_logger("PATHS")

    for old, new in _LEGACY_USER_DIRS:
        try:
            if not old.exists() or old == new or old.resolve() == new.resolve():
                continue
            if new.exists() and any(new.iterdir()):
                # Don't clobber: legacy dir is informational only at this point.
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            if new.exists():
                new.rmdir()
            shutil.move(str(old), str(new))
            _log.info(f"migrated {old} → {new}")
        except (OSError, shutil.Error) as e:
            _log.warn(f"could not migrate {old} → {new}: {e}")
