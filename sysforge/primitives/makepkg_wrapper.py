import os
import fnmatch
import re
import sys
import tempfile
import subprocess
import tomllib
from pathlib import Path

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

CONFIG_PATHS = [
    Path.home() / ".config/sysforge/flag_profiles.toml",
    CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
]


def load_config():
    pass


def resolve_profile(pkgmeta, config):
    pass


def merge_extends(profile_name, profiles, visited=None):
    """
    Resolve a profile's full inheritance chain via 'extends'.
    Returns a flat dict of all keys with child values overriding parent values.
    Raises ValueError on missing profiles or inheritance cycles.
    """
    if visited is None:
        visited = []

    if profile_name in visited:
        cycle = " -> ".join(visited + [profile_name])
        raise ValueError(f"[PROFILE] Inheritance cycle detected: {cycle}")

    if profile_name not in profiles:
        raise ValueError(f"[PROFILE] Profile not found: '{profile_name}'")

    profile = dict(profiles[profile_name])
    parent_name = profile.pop("extends", None)

    if parent_name is None:
        return profile

    parent = merge_extends(parent_name, profiles, visited + [profile_name])

    # Parent provides the base; child keys win
    return {**parent, **profile}


def match_rules(pkgmeta, rules):
    """
    Evaluate rules against parsed PKGBUILD metadata.
    Conditions within a rule are AND'd; rules are OR'd.
    Returns list of matched rules, preserving order.
    """
    globals_ = pkgmeta.get("globals", {})

    # Normalize pkgname — may be a string (single) or list (split package)
    pkgname = globals_.get("pkgname", "")
    if isinstance(pkgname, list):
        pkgnames = list(pkgname)
    else:
        pkgnames = [pkgname]

    def _glob_any_match(patterns, values):
        """True if ANY pattern matches ANY value."""
        return any(fnmatch.fnmatch(val, pat) for pat in patterns for val in values)

    def _glob_all_match(patterns, values):
        """True if ALL patterns match at least one value."""
        return all(any(fnmatch.fnmatch(val, pat) for val in values) for pat in patterns)

    def _glob_all_absent(patterns, values):
        """True if ALL patterns match NO value."""
        return all(
            not any(fnmatch.fnmatch(val, pat) for val in values) for pat in patterns
        )

    def _exact_any(rule_key, meta_key):
        """True if ANY rule item appears in pkgmeta."""
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or bool(rule_vals & meta_vals)

    def _exact_all(rule_key, meta_key):
        """True if ALL rule items appear in pkgmeta."""
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or rule_vals.issubset(meta_vals)

    def _exact_all_absent(rule_key, meta_key):
        """True if ALL rule items are absent from pkgmeta."""
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or not bool(rule_vals & meta_vals)

    matched = []
    for rule in rules:
        # pkgnames — ANY + glob
        if "pkgnames" in rule:
            if not _glob_any_match(rule["pkgnames"], pkgnames):
                continue

        # not_pkgnames — ALL absent + glob
        if "not_pkgnames" in rule:
            if not _glob_all_absent(rule["not_pkgnames"], pkgnames):
                continue

        # groups — ALL + glob
        if "groups" in rule:
            meta_groups = globals_.get("groups", [])
            if not _glob_all_match(rule["groups"], meta_groups):
                continue

        # not_groups — ALL absent, no glob
        if "not_groups" in rule:
            if not _exact_all_absent("not_groups", "groups"):
                continue

        # depends_any — ANY exact
        if "depends_any" in rule:
            if not _exact_any("depends_any", "depends"):
                continue

        # depends_all — ALL exact
        if "depends_all" in rule:
            if not _exact_all("depends_all", "depends"):
                continue

        # not_depends — ALL absent, exact
        if "not_depends" in rule:
            if not _exact_all_absent("not_depends", "depends"):
                continue

        # makedepends_any — ANY exact
        if "makedepends_any" in rule:
            if not _exact_any("makedepends_any", "makedepends"):
                continue

        # makedepends_all — ALL exact
        if "makedepends_all" in rule:
            if not _exact_all("makedepends_all", "makedepends"):
                continue

        # not_makedepends — ALL absent, exact
        if "not_makedepends" in rule:
            if not _exact_all_absent("not_makedepends", "makedepends"):
                continue

        matched.append(rule)

    return matched


def resolve_groups(pkgmeta, matched_rules, defaults):
    pass


def emit_makepkg_conf(resolved_profile):
    pass


def invoke_makepkg(pkgbuild_path, conf_path):
    pass


def run(pkgbuild_path):
    pkgmeta = parse_pkgbuild(pkgbuild_path)
    config = load_config()

    resolved_profile = resolve_profile(pkgmeta, config)
    groups = resolve_groups(pkgmeta, resolved_profile, config.get("defaults", {}))

    build_mode = resolved_profile.get("build_mode", None)

    if build_mode == "pgo_llvm_toolchain":
        pass  # hand off to pgo handler
    elif build_mode == "patch_linker":
        pass  # hand off to linker patcher
    else:
        with emit_makepkg_conf(resolved_profile) as conf_path:
            invoke_makepkg(pkgbuild_path, conf_path)


if __name__ == "__main__":
    run(sys.argv[1])
