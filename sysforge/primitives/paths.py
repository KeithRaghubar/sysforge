# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
paths.py — centralised path constants for sysforge config files

CONFIG_DIR is the directory that *directly contains* the sysforge TOML files.
It is the SYSFORGE_CONFIG_DIR env var when set (so a from-repo dev setup can
point at e.g. ~/sf-config and keep the files right there, mirroring how
SYSFORGE_STATE_DIR holds state files directly), else the FHS system location
/etc/sysforge. The env var is the *config dir itself*, NOT an FHS root prefix —
sysforge no longer composes an `etc/sysforge` subpath under it. The installed
system (env unset) is unaffected: it resolves to /etc/sysforge as before.

User-side paths follow the XDG Base Directory Specification: config lives
under $XDG_CONFIG_HOME (default ~/.config), regenerable cache under
$XDG_CACHE_HOME (default ~/.cache), fallback runtime state under
$XDG_STATE_HOME (default ~/.local/state), and authoritative user-authored
data under $XDG_DATA_HOME (default ~/.local/share) — four separate roots,
each honouring its env var when set.
"""
import os
import shutil
from pathlib import Path

# The config dir holds the TOML files directly (env override) or falls back to
# the FHS system path. Empty-string env is treated as unset.
_CONFIG_DIR_ENV = os.environ.get("SYSFORGE_CONFIG_DIR")
CONFIG_DIR = Path(_CONFIG_DIR_ENV) if _CONFIG_DIR_ENV else Path("/etc/sysforge")


def _xdg_base(env: str, default_rel: str) -> Path:
    """Return the XDG base dir from `env`, or ~/`default_rel` when unset/empty."""
    val = os.environ.get(env)
    return Path(val) if val else Path.home() / default_rel


USER_CONFIG_DIR = _xdg_base("XDG_CONFIG_HOME", ".config")      / "sysforge"
USER_CACHE_DIR  = _xdg_base("XDG_CACHE_HOME",  ".cache")       / "sysforge"
USER_STATE_DIR  = _xdg_base("XDG_STATE_HOME",  ".local/state") / "sysforge"
# Authoritative user-authored data (managed artifact content). Distinct from
# config (not user-edited settings), cache (not regenerable) and state (not
# derived) — losing it is unrecoverable, so it gets XDG's data root.
USER_DATA_DIR   = _xdg_base("XDG_DATA_HOME",   ".local/share") / "sysforge"

# profiles.toml search order (user, then system)
# Holds [paths] [defaults] [profiles.*] [[rules]] plus the consolidated
# [append_conflict_groups] and [consumes_inference] sections.
CONFIG_PATHS = [
    USER_CONFIG_DIR / "profiles.toml",
    CONFIG_DIR / "profiles.toml",
]

# Individual config files
PACKAGES_PATH = CONFIG_DIR / "packages.toml"
KERNEL_PATH = CONFIG_DIR / "kernel.toml"
TOOLCHAIN_PATH = CONFIG_DIR / "toolchain.toml"
SYSFORGE_TOML_PATH = CONFIG_DIR / "sysforge.toml"
BOOTSTRAP_PATH = CONFIG_DIR / "bootstrap.toml"


def resolve_packages_path(config: dict) -> Path:
    """Resolve packages.toml from config override or default PACKAGES_PATH."""
    raw = config.get("packages_file")
    if raw:
        return Path(raw).expanduser()
    return PACKAGES_PATH


_LEGACY_USER_DIRS = (
    (Path.home() / ".config/sysforge/cache", USER_CACHE_DIR),
    (Path.home() / ".config/sysforge/state", USER_STATE_DIR),
)


def migrate_legacy_user_dirs() -> None:
    """Best-effort one-shot move of the legacy consolidated dirs
    ~/.config/sysforge/{cache,state} into their XDG-correct homes
    ($XDG_CACHE_HOME/sysforge and $XDG_STATE_HOME/sysforge, default
    ~/.cache/sysforge and ~/.local/state/sysforge). Idempotent; never raises."""
    from sysforge import log
    from sysforge.primitives import deprecations
    _log = log.get_logger("PATHS")

    for old, new in _LEGACY_USER_DIRS:
        try:
            if not old.exists() or old == new or old.resolve() == new.resolve():
                continue
            deprecations.warn_used("paths.legacy_user_dirs")
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
