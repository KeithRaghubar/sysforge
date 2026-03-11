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
    pass


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
        pkgnames = set(pkgname)
    else:
        pkgnames = {pkgname}

    def _any_overlap(rule_key, meta_key):
        """True if any item in the rule's list matches any item in pkgmeta."""
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or bool(rule_vals & meta_vals)

    def _any_overlap_glob(rule_key, meta_key):
        """True if any rule glob pattern matches any meta value."""
        rule_pats = rule.get(rule_key, [])
        meta_vals = globals_.get(meta_key, [])
        if not rule_pats:
            return True
        return any(
            fnmatch.fnmatch(meta_val, pat)
            for pat in rule_pats
            for meta_val in meta_vals
        )

    def _any_overlap_not(rule_key, meta_key):
        """True if NONE of the rule's list items appear in pkgmeta."""
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or not bool(rule_vals & meta_vals)

    matched = []
    for rule in rules:
        # pkgname and pkgname_regex are mutually exclusive; pkgnames is a list variant
        if "pkgname" in rule:
            if rule["pkgname"] not in pkgnames:
                continue
        elif "pkgnames" in rule:
            if not pkgnames & set(rule["pkgnames"]):
                continue
        elif "pkgname_regex" in rule:
            pattern = re.compile(rule["pkgname_regex"])
            if not any(pattern.fullmatch(n) for n in pkgnames):
                continue

        if "not_pkgname" in rule:
            if rule["not_pkgname"] in pkgnames:
                continue

        if not _any_overlap_glob("groups", "groups"):
            continue
        if not _any_overlap("depends", "depends"):
            continue
        if not _any_overlap("makedepends", "makedepends"):
            continue

        if not _any_overlap_not("not_groups", "groups"):
            continue
        if not _any_overlap_not("not_depends", "depends"):
            continue
        if not _any_overlap_not("not_makedepends", "makedepends"):
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
