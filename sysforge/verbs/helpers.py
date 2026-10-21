# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/helpers.py — shared helpers for CLI verb implementations.

Small utilities used by more than one verb module. Kept out of ``base.py``
(which is the verb protocol) and out of ``cli.py``/``verbs/registry.py`` (a verb
module importing from either would close an import cycle, since both import the
verb modules).
"""
from sysforge.primitives.config import load_config

#: Help text for a ``--packages FILE`` flag that names the packages.toml path.
PACKAGES_FILE_HELP = (
    "Path to packages.toml (default: /etc/sysforge/packages.toml; "
    "override the dir with $SYSFORGE_CONFIG_DIR)."
)

#: Help text for ``--cleansrc-force``, the same on every verb that syncs
#: sources (one wording, so sysforge(1) lists it once under SHARED OPTIONS).
CLEANSRC_FORCE_HELP = (
    "Like --cleansrc but bypasses the dirty/diverged guard and overwrites the "
    "local clone unconditionally. Use when the upstream rewrote history (e.g. "
    "Arch packaging repos force-push every release) and the local commits have "
    "no value to preserve."
)


def load_config_with_overrides(args) -> dict:
    """Load flag_profiles config and apply CLI overrides (--packages, --profile-conf)."""
    config = load_config() or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages
    if getattr(args, "profile_conf", None):
        config["profile_conf"] = args.profile_conf
    return config
