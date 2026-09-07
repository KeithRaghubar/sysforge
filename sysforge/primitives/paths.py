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


# ---------------------------------------------------------------------------
# State dir resolution
# ---------------------------------------------------------------------------
# Lives here, not in ``pipeline/state.py``, because it is a path computation
# with no pipeline knowledge and half the leaf layer needs it (3.2.0-F1a):
# makepkg_wrapper, state_probe, source_sync, llvm_state and init_notice all
# used to reach *up* into the pipeline layer for it at function level, which is
# a cycle deferred rather than removed. ``pipeline.state`` re-exports the name
# for one cycle so existing imports keep working.

DEFAULT_STATE_DIR = Path("/var/lib/sysforge")
FALLBACK_STATE_DIR = USER_STATE_DIR


def _state_dir_is_writable(path: Path) -> bool:
    """Return True if path exists and is writable, or its parent is writable
    (so mkdir can succeed)."""
    if path.exists():
        return os.access(path, os.W_OK)
    return path.parent.exists() and os.access(path.parent, os.W_OK)


def resolve_state_dir(cli_override=None):
    """
    Resolve the state directory from CLI flag, env var, or default.
    Returns (Path, source_str) where source_str describes which was used.
    Logs both CLI and env sources whenever present.

    If the system default (/var/lib/sysforge) is not writable (e.g. running
    from source without root), falls back to the XDG state dir
    ($XDG_STATE_HOME/sysforge, default ~/.local/state/sysforge) and logs
    a one-time info message so the location is transparent.
    """
    from sysforge import log

    _log = log.get_logger("STATE")
    env_val = os.environ.get("SYSFORGE_STATE_DIR")
    sources = []

    if cli_override:
        sources.append(f"--state-dir={cli_override}")
    if env_val:
        sources.append(f"SYSFORGE_STATE_DIR={env_val}")

    if sources:
        _log.info(f"State dir source(s) found: {', '.join(sources)}")

    if cli_override:
        chosen = Path(cli_override)
        _log.info(f"Using state dir (--state-dir takes priority): {chosen}")
        return chosen, "--state-dir"

    if env_val:
        chosen = Path(env_val)
        _log.info(f"Using state dir (SYSFORGE_STATE_DIR): {chosen}")
        return chosen, "SYSFORGE_STATE_DIR"

    if not _state_dir_is_writable(DEFAULT_STATE_DIR):
        # Attempt to provision the default into the shared root:sysforge tree
        # (the one home for sysforge dir ownership). Falls back to the XDG
        # state dir only when sudo is unavailable, so a non-root run-from-repo
        # invocation still works without prompting.
        from sysforge.primitives import fs_provision

        try:
            fs_provision.ensure_writable_dir(DEFAULT_STATE_DIR)
            return DEFAULT_STATE_DIR, "default"
        except fs_provision.FsProvisionError:
            _log.info(
                f"State dir {DEFAULT_STATE_DIR} is not writable and could not be "
                f"provisioned — falling back to {FALLBACK_STATE_DIR} "
                "(set SYSFORGE_STATE_DIR or install sysforge via PKGBUILD to use "
                "/var/lib/sysforge)",
            )
            return FALLBACK_STATE_DIR, "xdg-fallback"

    return DEFAULT_STATE_DIR, "default"
