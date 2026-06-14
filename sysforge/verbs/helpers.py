# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/helpers.py — shared helpers for CLI verb implementations.

Small utilities used by more than one verb module. Kept out of ``base.py``
(which is the verb protocol) and out of ``cli.py`` (a verb module importing
from cli would close an import cycle, since cli imports the verb modules).
"""
from sysforge.primitives.config import load_config


def load_config_with_overrides(args) -> dict:
    """Load flag_profiles config and apply CLI overrides (--packages, --profile-conf)."""
    config = load_config() or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages
    if getattr(args, "profile_conf", None):
        config["profile_conf"] = args.profile_conf
    return config
