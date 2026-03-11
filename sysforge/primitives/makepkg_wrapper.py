import contextlib
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

CONFIG_PATHS = [
    Path.home() / ".config/sysforge/flag_profiles.toml",
    CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
]


def load_config():
    """
    Load flag_profiles.toml from CONFIG_PATHS (user, then system).
    If the user config sets extends_system = true, deep-merge onto system config.
    Otherwise the first found file wins outright.
    Rule priorities must be in range 0-99. User rules are bumped by 100 on merge
    to guarantee precedence over system rules (effective range 100-199).
    Raises FileNotFoundError if no config is found.
    Raises ValueError if any rule has an invalid or missing priority.
    """

    def _load(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    def _deep_merge(base, override):
        """Merge override onto base, recursing into dicts."""
        result = dict(base)
        for key, val in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(val, dict)
            ):
                result[key] = _deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    user_path, system_path = CONFIG_PATHS[0], CONFIG_PATHS[1]

    user_config = _load(user_path) if user_path.exists() else None
    system_config = _load(system_path) if system_path.exists() else None

    if user_config is None and system_config is None:
        raise FileNotFoundError(
            f"[CONFIG] No flag_profiles.toml found. Searched:\n"
            + "\n".join(f"  {p}" for p in CONFIG_PATHS)
        )

    if user_config is None:
        print(f"[CONFIG] Loaded system config: {system_path}")
        return system_config

    if system_config is None:
        print(f"[CONFIG] Loaded user config: {user_path}")
        return user_config

    if user_config.get("extends_system", False):
        print(f"[CONFIG] Merging user config onto system config")

        # Merge profiles and defaults normally (user wins per key)
        merged = _deep_merge(
            {k: v for k, v in system_config.items() if k != "rules"},
            {
                k: v
                for k, v in user_config.items()
                if k not in ("rules", "extends_system")
            },
        )

        # Validate priority range before merging
        VALID_RANGE = range(0, 100)
        for source, rules in [
            ("system", system_config.get("rules", [])),
            ("user", user_config.get("rules", [])),
        ]:
            for i, rule in enumerate(rules):
                p = rule.get("priority")
                if p is None:
                    raise ValueError(
                        f"[CONFIG] {source} rule [{i}] is missing required 'priority'"
                    )
                if p not in VALID_RANGE:
                    raise ValueError(
                        f"[CONFIG] {source} rule [{i}] has invalid priority {p!r} "
                        f"(must be 0-99)"
                    )

        # Merge rules: user rules are bumped by 100 to guarantee precedence over system rules.
        # Valid priority range is 0-99 per config; effective range after bump is 100-199.
        user_rules = [
            {**r, "priority": r.get("priority", 0) + 100}
            for r in user_config.get("rules", [])
        ]
        system_rules = system_config.get("rules", [])

        merged["rules"] = system_rules + user_rules
        return merged

    print(f"[CONFIG] User config overrides system config: {user_path}")
    return user_config


def resolve_profile(pkgmeta, config):
    """
    Match rules against pkgmeta, select winning profile by priority,
    resolve its extends chain, and return the flat merged profile dict.
    Highest priority wins; ties go to first occurrence. Losers are logged.
    Falls back to defaults.profile if no rules match.
    """
    profiles = config.get("profiles", {})
    rules = config.get("rules", [])
    defaults = config.get("defaults", {})
    default_profile = defaults.get("profile", "bare")

    pkgname = pkgmeta.get("globals", {}).get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    matched = match_rules(pkgmeta, rules)

    winner = None
    discarded = []
    for rule in matched:
        if "profile" not in rule:
            continue
        if winner is None or rule.get("priority", 0) > winner.get("priority", 0):
            if winner is not None:
                discarded.append(winner)
            winner = rule
        else:
            discarded.append(rule)

    for rule in discarded:
        print(
            f"[PROFILE][{pkgname}] Discarded rule "
            f"(priority {rule.get('priority', 0)}): profile={rule.get('profile')!r}"
        )
        if winner:
            profile_name = winner["profile"]
            print(
                f"[PROFILE][{pkgname}] Matched profile {profile_name!r} "
                f"(priority {winner.get('priority', 0)})"
            )
        else:
            if matched:
                print(
                    f"[PROFILE][{pkgname}] Rules matched but none specified a profile, "
                    f"using default: {default_profile!r}"
                )
            else:
                print(
                    f"[PROFILE][{pkgname}] No rules matched, using default: {default_profile!r}"
                )
            profile_name = default_profile

    return merge_extends(profile_name, profiles)


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
    """
    Build the final groups list for a package.
    Starts with the PKGBUILD's own groups, then appends:
      - defaults.append_groups (always)
      - append_groups from each matched rule, in rule order
    Deduplicates while preserving insertion order.
    """
    pkgname = pkgmeta.get("globals", {}).get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    existing = list(pkgmeta.get("globals", {}).get("groups", []))
    to_append = list(defaults.get("append_groups", []))

    for rule in matched_rules:
        to_append.extend(rule.get("append_groups", []))

    # Deduplicate preserving order, existing groups take precedence
    seen = set(existing)
    for group in to_append:
        if group not in seen:
            existing.append(group)
            seen.add(group)

    print(f"[GROUPS][{pkgname}] Resolved groups: {existing}")
    return existing


# Keys that are sysforge-internal and must not be written to makepkg.conf
_SYSFORGE_KEYS = {"build_mode", "pgo_store"}


@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile):
    """
    Write makepkg-relevant keys from resolved_profile to a temp file.
    Yields the path to the temp file for use with MAKEPKG_CONF.
    Cleans up the temp file on exit.
    Skips sysforge-internal keys (build_mode, pgo_store, etc.).
    """
    conf_lines = []
    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue
        conf_lines.append(f'{key}="{val}"')

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="sysforge_makepkg_",
        suffix=".conf",
        delete=False,
    ) as f:
        f.write("\n".join(conf_lines) + "\n")
        tmp_path = f.name

    print(f"[CONF] Wrote temp makepkg.conf: {tmp_path}")
    try:
        yield tmp_path
    finally:
        os.unlink(tmp_path)
        print(f"[CONF] Removed temp makepkg.conf: {tmp_path}")


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
