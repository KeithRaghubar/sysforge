import contextlib
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild, patch_pkgbuild_groups

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

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

# Keys that are sysforge-internal and must not be written to any conf file
_SYSFORGE_KEYS = {
    "batch",
    "build_mode",
    "clean_builddir",
    "consumes",
    "failure_handling",
    "makepkg_flags",
    "pgo_store",
}

# ---------------------------------------------------------------------------
# Consumes key map
#
# Maps conf file type → the set of profile keys that belong in that file.
# Keys not in any conf type's set (and not in _SYSFORGE_KEYS) are destined
# for explicit env var injection (future env pass).
# ---------------------------------------------------------------------------

_CONF_KEY_MAP: dict[str, set[str]] = {
    "makepkg": {
        # Standard makepkg.conf compiler/linker variables
        "CC", "CXX", "AR", "NM", "RANLIB", "STRIP",
        "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
        "DEBUG_CFLAGS", "DEBUG_CXXFLAGS", "DEBUG_LDFLAGS",
        "MAKEFLAGS",
        # makepkg behaviour knobs written as shell vars
        "BUILDENV", "OPTIONS", "INTEGRITY_CHECK",
        "PKGEXT", "SRCEXT",
    },
    "rust": {
        "RUSTFLAGS",
        "CARGO_PROFILE_RELEASE_LTO",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
        "CARGO_PROFILE_RELEASE_OPT_LEVEL",
        "CARGO_INCREMENTAL",
        "RUSTC_WRAPPER",
    },
    "cmake": {
        "CMAKE_BUILD_TYPE",
        "CMAKE_C_FLAGS",
        "CMAKE_CXX_FLAGS",
        "CMAKE_EXE_LINKER_FLAGS",
        "CMAKE_SHARED_LINKER_FLAGS",
    },
    "meson": {
        "MESON_ARGS",
    },
}

# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

# Scenarios that always abort regardless of config
_ALWAYS_ABORT = {"profile_missing", "tempfile_write_failed"}

# Default behaviours if [failure_handling] is absent or incomplete
_FAILURE_DEFAULTS = {
    "pkgbuild_unparseable": "warn_and_fallback",
    "no_rule_matched": "fallback",
    "profile_missing": "abort",
    "profile_cycle": "abort",
    "tempfile_write_failed": "abort",
    "env_conflict": "warn_and_fallback",
    "abi_mismatch": "warn_and_fallback",
    "dep_unsatisfied": "warn_and_fallback",
}

_VALID_BEHAVIOURS = {"abort", "warn_and_fallback", "fallback", "error"}


def handle_failure(scenario, message, config, fallback=None):
    """
    Handle a named failure scenario according to [failure_handling] config.

    Behaviours:
      abort          — log and raise RuntimeError immediately
      error          — log as error, raise RuntimeError
      warn_and_fallback — log as warning, return fallback value
      fallback       — return fallback value silently

    profile_missing and tempfile_write_failed always abort regardless of config.
    """
    failure_cfg = config.get("failure_handling", {})
    behaviour = failure_cfg.get(scenario, _FAILURE_DEFAULTS.get(scenario, "abort"))

    if scenario in _ALWAYS_ABORT:
        behaviour = "abort"

    if behaviour not in _VALID_BEHAVIOURS:
        print(
            f"[FAILURE] Unknown behaviour {behaviour!r} for scenario {scenario!r}, defaulting to abort"
        )
        behaviour = "abort"

    if behaviour == "abort":
        print(f"[FAILURE][{scenario}] ABORT: {message}")
        raise RuntimeError(f"[{scenario}] {message}")

    elif behaviour == "error":
        print(f"[FAILURE][{scenario}] ERROR: {message}")
        raise RuntimeError(f"[{scenario}] {message}")

    elif behaviour == "warn_and_fallback":
        print(f"[FAILURE][{scenario}] WARNING: {message} — falling back")
        return fallback

    elif behaviour == "fallback":
        return fallback


def load_conflict_groups(conflict_group_paths=None):
    """
    Load append_conflict_groups.toml from user and system paths.
    Returns dict: { group_name: [flag, ...] }

    If the user file sets extends_system = true, user groups are merged onto
    system groups (user wins per key). Otherwise the first found file wins.
    Returns an empty dict if no file is found (non-fatal).
    """

    def _load(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    paths = conflict_group_paths if conflict_group_paths is not None else CONFLICT_GROUP_PATHS
    user_path = paths[0] if len(paths) > 0 else None
    system_path = paths[1] if len(paths) > 1 else None

    user_data = _load(user_path) if user_path and user_path.exists() else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        return {}

    system_groups = system_data.get("conflict_groups", {}) if system_data else {}

    if user_data is None:
        return system_groups

    user_groups = user_data.get("conflict_groups", {})

    if user_data.get("extends_system", False):
        merged = dict(system_groups)
        merged.update(user_groups)
        return merged

    return user_groups


def load_consumes_inference(paths=None):
    """
    Load consumes_inference.toml from system (and optionally user) paths.
    Returns dict: { makedep_tool: [conf_type, ...] }

    The inference map is system-defined and user-extendable. If the user file
    sets extends_system = true, user entries are merged onto system entries
    (user wins per key). Otherwise the first found file wins.
    Returns a built-in minimal default if no file is found.
    """

    def _load(p):
        with open(p, "rb") as f:
            return tomllib.load(f)

    _DEFAULT_INFERENCE = {
        "cargo":  ["makepkg", "rust", "env"],
        "meson":  ["makepkg", "meson", "env"],
        "cmake":  ["makepkg", "cmake", "env"],
        "ninja":  ["makepkg", "env"],
        "make":   ["makepkg", "env"],
        "python": ["makepkg", "env"],
        "git":    ["makepkg"],
    }

    resolved_paths = paths if paths is not None else CONSUMES_INFERENCE_PATHS
    user_path   = resolved_paths[0] if len(resolved_paths) > 0 else None
    system_path = resolved_paths[1] if len(resolved_paths) > 1 else None

    user_data   = _load(user_path)   if user_path   and user_path.exists()   else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        return _DEFAULT_INFERENCE

    system_map = system_data.get("consumes_inference", {}) if system_data else {}

    if user_data is None:
        print(f"[CONFIG] Loaded consumes_inference: {system_path}")
        return system_map

    user_map = user_data.get("consumes_inference", {})

    if user_data.get("extends_system", False):
        merged = dict(system_map)
        merged.update(user_map)
        print(f"[CONFIG] Merged consumes_inference (user onto system)")
        return merged

    print(f"[CONFIG] Loaded consumes_inference (user only): {user_path}")
    return user_map


def resolve_consumes(resolved_profile, pkgmeta, inference_map):
    """
    Determine the set of conf file types required for this build.

    Resolution order:
      1. Explicit `consumes` list on the profile → use it verbatim (overrides inference)
      2. Auto-infer: union of inference_map[dep] for each makedep that appears
         in the inference map
      3. Always include "makepkg" as baseline

    Returns a frozenset of conf type strings, e.g. frozenset({"makepkg", "rust"}).
    Logs the active set and its source under [CONF].
    """
    pkgname = pkgmeta.get("globals", {}).get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    explicit = resolved_profile.get("consumes")
    if explicit is not None:
        active = frozenset(explicit)
        print(f"[CONF][{pkgname}] consumes (explicit): {sorted(active)}")
        return active

    makedepends = set(pkgmeta.get("globals", {}).get("makedepends", []))
    inferred: set[str] = {"makepkg"}
    for dep in makedepends:
        # Strip version constraints (e.g. "cmake>=3.25" → "cmake")
        bare_dep = re.split(r"[><=!]", dep, maxsplit=1)[0].strip()
        if bare_dep in inference_map:
            inferred.update(inference_map[bare_dep])

    active = frozenset(inferred)
    matched_deps = sorted(
        bare_dep
        for dep in makedepends
        for bare_dep in [re.split(r"[><=!]", dep, maxsplit=1)[0].strip()]
        if bare_dep in inference_map
    )
    print(
        f"[CONF][{pkgname}] consumes (inferred from makedepends {matched_deps}): "
        f"{sorted(active)}"
    )
    return active


def _extract_prefix(token):
    """
    Extract the prefix used for prefix-match replacement during append merge.

    Rules (in order):
    - Flags containing '=' (e.g. '--icf=all', '-flto=thin', '-DFOO=bar'):
      prefix is everything up to and including '='
    - Flags ending in a run of digits (e.g. '-O2', '-O3', '-g2'):
      prefix is everything before those trailing digits
    - All other flags: no prefix (returns None — no replacement)
    """
    eq_idx = token.find("=")
    if eq_idx != -1:
        return token[: eq_idx + 1]

    m = re.match(r"^(.*[^\d])(\d+)$", token)
    if m:
        return m.group(1)

    return None


def _merge_append_value(parent_val, child_append_val, conflict_groups):
    """
    Merge child_append_val token list into parent_val token list.

    Algorithm (per child token):
      1. Explicit conflict group — if token is in a group, remove all group
         members from the result list, then insert the child token.
      2. Prefix match — if a token with the same prefix already exists,
         replace it in-place.
      3. Append — no match, add to end.

    Returns the merged string (space-joined).
    """
    parent_tokens = parent_val.split() if parent_val else []
    child_tokens = child_append_val.split() if child_append_val else []

    # Build reverse index: flag → frozenset of its group members
    flag_to_group: dict[str, list[str]] = {}
    for members in conflict_groups.values():
        for member in members:
            flag_to_group[member] = members

    result = list(parent_tokens)

    for child_token in child_tokens:
        # (1) Explicit conflict group
        if child_token in flag_to_group:
            group_members = set(flag_to_group[child_token])
            result = [t for t in result if t not in group_members]
            result.append(child_token)
            continue

        # (2) Prefix match
        prefix = _extract_prefix(child_token)
        matched_idx = None
        if prefix is not None:
            for i, existing in enumerate(result):
                if _extract_prefix(existing) == prefix:
                    matched_idx = i
                    break

        if matched_idx is not None:
            result[matched_idx] = child_token
            continue

        # (3) Append
        result.append(child_token)

    return " ".join(result)


def load_config(config_paths=None):
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

    paths = config_paths if config_paths is not None else CONFIG_PATHS
    user_path = paths[0] if len(paths) > 0 else None
    system_path = paths[1] if len(paths) > 1 else None

    user_config = _load(user_path) if user_path and user_path.exists() else None
    system_config = _load(system_path) if system_path and system_path.exists() else None

    if user_config is None and system_config is None:
        raise FileNotFoundError(
            f"[CONFIG] No flag_profiles.toml found. Searched:\n"
            + "\n".join(f"  {p}" for p in CONFIG_PATHS)
        )

    if user_config is None:
        _validate_rule_priorities(system_config.get("rules", []), "system")
        print(f"[CONFIG] Loaded system config: {system_path}")
        return system_config

    if system_config is None:
        _validate_rule_priorities(user_config.get("rules", []), "user")
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

        _validate_rule_priorities(system_config.get("rules", []), "system")
        _validate_rule_priorities(user_config.get("rules", []), "user")

        # Merge rules: user rules are bumped by 100 to guarantee precedence over system rules.
        # Valid priority range is 0-99 per config; effective range after bump is 100-199.
        user_rules = [
            {**r, "priority": r.get("priority", 0) + 100}
            for r in user_config.get("rules", [])
        ]
        system_rules = system_config.get("rules", [])

        merged["rules"] = system_rules + user_rules
        return merged

    _validate_rule_priorities(user_config.get("rules", []), "user")
    print(f"[CONFIG] User config overrides system config: {user_path}")
    return user_config


def _validate_rule_priorities(rules, source):
    VALID_RANGE = range(0, 100)
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


def resolve_profile(pkgmeta, matched_rules, config, conflict_groups=None):
    """
    Select winning profile from matched_rules by priority, resolve its extends
    chain, and return the flat merged profile dict.
    Highest priority wins; ties go to first occurrence. Losers are logged.
    Falls back to defaults.profile if no rules match.
    """
    profiles = config.get("profiles", {})
    defaults = config.get("defaults", {})
    default_profile = defaults.get("profile", "bare")

    pkgname = pkgmeta.get("globals", {}).get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    winner = None
    discarded = []
    for rule in matched_rules:
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
        if matched_rules:
            print(
                f"[PROFILE][{pkgname}] Rules matched but none specified a profile, "
                f"using default: {default_profile!r}"
            )
        else:
            print(
                f"[PROFILE][{pkgname}] No rules matched, using default: {default_profile!r}"
            )
        profile_name = default_profile

    return merge_extends(profile_name, profiles, conflict_groups=conflict_groups)


def merge_extends(profile_name, profiles, visited=None, conflict_groups=None):
    """
    Resolve a profile's full inheritance chain via 'extends'.
    Returns a flat dict of all keys with child values overriding parent values.

    Direct child keys fully replace the parent value (child must restate the
    complete value). Keys in the optional 'append' sub-dict are merged into the
    parent value using the token-level append algorithm (_merge_append_value),
    which handles prefix replacement and explicit conflict groups.

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
    append_overrides = profile.pop("append", {})

    if conflict_groups is None:
        conflict_groups = {}

    if parent_name is None:
        if append_overrides:
            print(
                f"[PROFILE] Warning: profile '{profile_name}' has [append] but no parent — "
                "append section ignored"
            )
        return profile

    parent = merge_extends(
        parent_name, profiles, visited + [profile_name], conflict_groups
    )

    # Parent provides the base; direct child keys win outright
    merged = {**parent, **profile}

    # Apply token-level append merges on top of direct overrides
    for key, child_append_val in append_overrides.items():
        if key in profile:
            print(
                f"[PROFILE] Warning: key '{key}' is set both directly and in [append] "
                f"on profile '{profile_name}' — direct value takes precedence, append ignored"
            )
            continue
        parent_val = parent.get(key, "")
        merged[key] = _merge_append_value(parent_val, child_append_val, conflict_groups)
        print(
            f"[PROFILE][{profile_name}] append merge {key!r}: "
            f"{parent_val!r} + {child_append_val!r} → {merged[key]!r}"
        )

    return merged


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


@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile, active_consumes=None):
    """
    Write makepkg-relevant keys from resolved_profile to a temp file.
    Yields the path to the temp file for use with MAKEPKG_CONF.
    Cleans up the temp file on exit.

    Only keys belonging to conf types in active_consumes are written.
    If active_consumes is None, falls back to writing all non-internal keys
    (backward-compatible behaviour, used when consumes resolution is unavailable).

    Keys not in any _CONF_KEY_MAP entry and not in _SYSFORGE_KEYS are silently
    skipped — they are destined for explicit env var injection (future env pass).
    """
    if active_consumes is None:
        # Fallback: write everything that isn't a sysforge-internal key
        allowed_keys = None
    else:
        # Union of key sets for all active conf types that we currently generate
        # as conf files. "env" is handled separately (future pass) — skip here.
        allowed_keys: set[str] = set()
        for conf_type in active_consumes:
            if conf_type in _CONF_KEY_MAP:
                allowed_keys.update(_CONF_KEY_MAP[conf_type])

    conf_lines = []
    skipped_for_env: list[str] = []

    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            skipped_for_env.append(key)
            continue
        conf_lines.append(f'{key}="{val}"')

    if skipped_for_env:
        print(
            f"[CONF] Skipped (env pass, not yet implemented): "
            f"{sorted(skipped_for_env)}"
        )

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


def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile):
    pkgbuild_path = Path(pkgbuild_path).resolve()
    build_dir = pkgbuild_path.parent

    env = os.environ.copy()
    env["MAKEPKG_CONF"] = str(conf_path)

    flags = resolved_profile.get("makepkg_flags", [])
    cmd = ["makepkg"] + flags

    print(
        f"[BUILD] Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}"
    )

    result = subprocess.run(cmd, cwd=build_dir, env=env)

    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "makepkg")


def _run_build(pkgbuild_path, resolved_profile, config, groups, active_consumes=None):
    """
    Emit makepkg.conf and invoke makepkg, handling build failures.
    In batch mode, aborts on failure.
    Otherwise, prompts the user to manually correct and retry.
    """
    patched_path = patch_pkgbuild_groups(pkgbuild_path, groups)
    try:
        with emit_makepkg_conf(resolved_profile, active_consumes) as conf_path:
            _invoke_with_retry(patched_path, conf_path, resolved_profile)
    except RuntimeError:
        raise
    except Exception as e:
        handle_failure("tempfile_write_failed", str(e), config)
    finally:
        if patched_path.exists():
            patched_path.unlink()
            print(f"[BUILD] Removed patched PKGBUILD: {patched_path}")


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.
    """
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile)
        except subprocess.CalledProcessError as e:
            print(f"[BUILD] Build failed in batch mode, aborting: {e}")
            raise RuntimeError(f"[build_failed] {e}")
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile)
                break
            except subprocess.CalledProcessError as e:
                print(f"[BUILD] Build failed: {e}")
                print(f"[BUILD] PKGBUILD location: {pkgbuild_path}")
                response = (
                    input(
                        "[BUILD] Manually correct the PKGBUILD and press Enter to retry, "
                        "or type 'abort' to stop: "
                    )
                    .strip()
                    .lower()
                )
                if response == "abort":
                    raise RuntimeError(
                        "[build_failed] Aborted by user after build failure"
                    )
                print("[BUILD] Retrying build...")


def run(pkgbuild_path):
    config = load_config()
    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        handle_failure("pkgbuild_unparseable", str(e), config)
        pkgmeta = {"globals": {}}

    pkgbuild_path = Path(pkgbuild_path).resolve()
    matched_rules = match_rules(pkgmeta, config.get("rules", []))
    resolved_profile = resolve_profile(pkgmeta, matched_rules, config, conflict_groups)
    active_consumes = resolve_consumes(resolved_profile, pkgmeta, inference_map)
    groups = resolve_groups(pkgmeta, matched_rules, config.get("defaults", {}))

    if resolved_profile.get("clean_builddir", False):
        build_dir = pkgbuild_path.parent
        for entry in build_dir.iterdir():
            if entry.name != "PKGBUILD" and not entry.name.endswith(".PKGBUILD"):
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        print(f"[BUILD] Cleaned build dir: {build_dir}")

    build_mode = resolved_profile.get("build_mode", None)

    if build_mode == "pgo_llvm_toolchain":
        pass  # hand off to pgo handler
    elif build_mode == "patch_linker":
        pass  # hand off to linker patcher
    else:
        _run_build(pkgbuild_path, resolved_profile, config, groups, active_consumes)


if __name__ == "__main__":
    run(sys.argv[1])
