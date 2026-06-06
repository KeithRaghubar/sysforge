"""
profile.py — profile resolution, rule matching, and consumes

Responsible for all flag profile logic: extends chain resolution, append
merging, rule evaluation, group accumulation, and consumes inference.
No file I/O; all config data is passed in as dicts.

Public API:
    match_rules(pkgmeta, rules)                              -> list[dict]
    resolve_profile(pkgmeta, matched_rules, config,
                    conflict_groups=None,
                    extracted_profile=None)                  -> dict
    merge_extends(profile_name, profiles,
                  visited=None, conflict_groups=None)        -> dict
    resolve_groups(pkgmeta, matched_rules, defaults)         -> list[str]
    resolve_consumes(resolved_profile, pkgmeta,
                     inference_map)                          -> frozenset[str]
    serialize_flags(resolved_profile)                        -> str
    get_build_mode(matched_rules, config)                    -> str | None
"""
import fnmatch
import pprint
import re
from sysforge import log

# One module, one tag. Profile resolution (extends-chain merge, append merging,
# rule matching, group accumulation, consumes inference) is a single concern;
# the historical per-facet loggers (CONF/FLAG/GROUPS) collapsed into [PROFILE]
# in P3.1. The CONF/FLAG names live on in the makepkg subsystem
# (makepkg_conf/makepkg_flags) — they are not this module's to share.
_log = log.get_logger("PROFILE")


# ---------------------------------------------------------------------------
# Key classification
# ---------------------------------------------------------------------------

# Keys that are sysforge-internal and must not be written to any conf file.
# Kept here as the authoritative source; makepkg_wrapper.py imports this.
SYSFORGE_KEYS = {
    "batch",
    "build_mode",
    "clean_builddir",
    "consumes",
    "failure_handling",
    "makepkg_flags",
    "pgo_store",
}

# Maps conf file type -> profile keys that belong in that delivery channel.
#
# All non-"env" types are written into the single temp makepkg.conf (which
# makepkg sources, making them available as shell variables in the build env).
# The "rust", "cmake", and "meson" types are separate filter buckets so that
# cargo/cmake/meson-specific keys are only included when those tools are in
# active_consumes — not written for every package unconditionally.
#
# "env" keys are injected directly onto the makepkg subprocess invocation env
# (subprocess.run env= arg) rather than written to the conf file. Use this for
# keys that need to be set before makepkg itself runs, or that wrap the compiler
# invocation (e.g. RUSTC_WRAPPER=sccache, CCACHE_DIR).
#
# Any profile key not in any type and not in SYSFORGE_KEYS also falls through
# to the env pass, logged under [ENV].
CONF_KEY_MAP: dict[str, set[str]] = {
    # Written to temp makepkg.conf. makepkg sources the conf and exports most
    # of these to the build environment, but NOT CC/CXX — those must be
    # injected via subprocess env instead (see "toolchain" below).
    "makepkg": {
        "AR", "NM", "RANLIB", "STRIP",
        "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
        "DEBUG_CFLAGS", "DEBUG_CXXFLAGS", "DEBUG_LDFLAGS",
        "MAKEFLAGS",
        "BUILDENV", "OPTIONS", "INTEGRITY_CHECK",
        "PKGEXT", "SRCEXT",
        "BUILDDIR",
    },
    # Always injected via subprocess env, regardless of active_consumes.
    # makepkg does not export CC/CXX from makepkg.conf to child processes —
    # they must be present in the env that makepkg inherits at invocation time.
    "toolchain": {
        "CC", "CXX",
    },
    "rust": {
        "RUSTFLAGS",
        "CARGO_PROFILE_RELEASE_LTO",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
        "CARGO_PROFILE_RELEASE_OPT_LEVEL",
        "CARGO_INCREMENTAL",
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
    # Keys in "env" are injected via subprocess invocation env, not conf file.
    # "env" must appear in active_consumes for these to be delivered; otherwise
    # they are logged as skipped (see resolve_env_vars).
    "env": {
        "RUSTC_WRAPPER",              # sccache/ccache Rust compiler wrapper
        "CCACHE_DIR",                 # ccache cache directory
        "SCCACHE_DIR",                # sccache cache directory
        "CARGO_HOME",                 # Cargo registry/cache root
        "CARGO_NET_GIT_FETCH_WITH_CLI",  # use git CLI for fetching (avoids SSH auth issues)
        "PKG_CONFIG_PATH",            # pkg-config search path
        "CC_LD",                      # meson: linker override for CC
        "CXX_LD",                     # meson: linker override for CXX
    },
}

# Compiler flag keys stripped from the emitted makepkg.conf for kernel builds.
# The kernel manages its own optimisation flags via Kconfig; injecting profile
# CFLAGS/CXXFLAGS/LDFLAGS causes miscompiles and build failures.
# System conf values for these keys are preserved verbatim (they pass through
# unchanged when no profile override is present).
KERNEL_CLEAN_KEYS: frozenset[str] = frozenset({
    "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
    "DEBUG_CFLAGS", "DEBUG_CXXFLAGS", "DEBUG_LDFLAGS",
})


# ---------------------------------------------------------------------------
# Variant-driven per-package env overlay
# ---------------------------------------------------------------------------
#
# Some PKGBUILDs use a build-time env var to select which LLVM source tree to
# link against (mesa's MESA_WHICH_LLVM is the canonical example). When the
# sysforge toolchain stage owns the system clang, that selector should always
# resolve to "the system LLVM" — otherwise a stray shell export can quietly
# divert the build to AUR llvm-minimal-git, missing sysforge's compiler entirely.
#
# Overlay is opt-in by package + variant; pkgbases not listed here get nothing,
# so this can't surprise unrelated builds. gcc variant is excluded — when
# sysforge isn't dictating LLVM, leave the user's shell env alone.

_MESA_PKGBASES = frozenset({"mesa", "mesa-git", "lib32-mesa", "lib32-mesa-git"})


def variant_env_overlay(pkgbase: str, variant: str | None) -> dict[str, str]:
    """Return per-package env vars driven by the active toolchain variant.

    Variants: ``"stock_llvm"`` / ``"pgo_llvm"`` activate overlays;
    ``"gcc"`` / ``"system"`` / ``None`` get an empty dict so nothing changes
    for builds where sysforge has no LLVM opinion.

    Today's only overlay: mesa-family pkgbases get ``MESA_WHICH_LLVM=4`` —
    the PKGBUILD's own selector for ``extra/llvm`` (which is the same
    package name sysforge installs when it builds LLVM, so the build links
    against sysforge's clang). Setting it here makes the build reproducible
    even if the user's shell exports a different value.
    """
    if not pkgbase or variant not in ("stock_llvm", "pgo_llvm"):
        return {}
    if pkgbase in _MESA_PKGBASES:
        return {"MESA_WHICH_LLVM": "4"}
    return {}


# ---------------------------------------------------------------------------
# Append merge internals
# ---------------------------------------------------------------------------

def _extract_prefix(token):
    """
    Extract the prefix used for prefix-match replacement during append merge.

    Rules (in order):
    - Flags containing '=' (e.g. '--icf=all', '-flto=thin'):
      prefix is everything up to and including '='
    - Flags ending in a run of digits (e.g. '-O2', '-O3', '-g2'):
      prefix is everything before those trailing digits
    - All other flags: no prefix (returns None)
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
      1. Explicit conflict group — remove all group members, insert child token.
      2. Prefix match — replace matching parent token in-place.
      3. Append — no match, add to end.

    Returns the merged string (space-joined).
    """
    parent_tokens = parent_val.split() if parent_val else []
    child_tokens = child_append_val.split() if child_append_val else []

    flag_to_group: dict[str, list[str]] = {}
    flag_to_group_name: dict[str, str] = {}
    for group_name, members in conflict_groups.items():
        for member in members:
            flag_to_group[member] = members
            flag_to_group_name[member] = group_name

    result = list(parent_tokens)

    for child_token in child_tokens:
        if child_token in flag_to_group:
            group_members = set(flag_to_group[child_token])
            removed = [t for t in result if t in group_members]
            result = [t for t in result if t not in group_members]
            if removed:
                _log.info(f"Conflict group '{flag_to_group_name[child_token]}': removed {removed}, inserted {child_token!r}")
            result.append(child_token)
            continue

        prefix = _extract_prefix(child_token)
        matched_idx = None
        if prefix is not None:
            for i, existing in enumerate(result):
                if _extract_prefix(existing) == prefix:
                    matched_idx = i
                    break

        if matched_idx is not None:
            old_token = result[matched_idx]
            result[matched_idx] = child_token
            _log.info(f"Prefix match: {old_token!r} → {child_token!r}")
            continue

        result.append(child_token)

    return " ".join(result)


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def merge_extends(profile_name, profiles, visited=None, conflict_groups=None):
    """
    Resolve a profile's full inheritance chain via 'extends'.
    Returns a flat dict of all keys with child values overriding parent values.

    Direct child keys fully replace the parent value. Keys in the optional
    'append' sub-dict are merged using the token-level algorithm in
    _merge_append_value.

    An extracted_profile dict can be injected as the implicit chain root by
    resolve_profile — it does not appear in the profiles dict and is never
    written to any TOML file.

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
            _log.warn(f"profile '{profile_name}' has [append] but no parent — append section ignored")
        return profile

    parent = merge_extends(
        parent_name, profiles, visited + [profile_name], conflict_groups
    )

    merged = {**parent, **profile}

    for key, child_append_val in append_overrides.items():
        if key in profile:
            _log.warn(f"key '{key}' set both directly and in [append] on profile '{profile_name}' — direct value wins, append ignored")
            continue
        parent_val = parent.get(key, "")
        merged[key] = _merge_append_value(parent_val, child_append_val, conflict_groups)
        _log.info(f"[{profile_name}] append merge {key!r}: {parent_val!r} + {child_append_val!r} → {merged[key]!r}")

    return merged


def resolve_profile(pkgmeta, matched_rules, config, conflict_groups=None,
                    extracted_profile=None):
    """
    Select winning profile from matched_rules by priority, resolve its extends
    chain, and return the flat merged profile dict.

    If extracted_profile is provided, it is injected as the implicit root of
    the inheritance chain — below bare — so the full profile chain always wins.
    The synthetic root is never persisted to any TOML file; that is the
    responsibility of pkgbuild_patcher.write_extracted_profile.

    Highest priority wins; ties go to first occurrence. Losers are logged.
    Falls back to defaults.profile if no rules match.
    """
    profiles = config.get("profiles", {})
    defaults = config.get("defaults", {})
    default_profile = defaults.get("profile", "bare")

    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
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
        _log.info(f"[{pkgname}] Discarded rule (priority {rule.get('priority', 0)}): profile={rule.get('profile')!r}")

    if winner:
        profile_name = winner["profile"]
        _log.info(f"[{pkgname}] Matched profile {profile_name!r} (priority {winner.get('priority', 0)})")
    else:
        if matched_rules:
            _log.info(f"[{pkgname}] Rules matched but none specified a profile, using default: {default_profile!r}")
        else:
            _log.info(f"[{pkgname}] No rules matched, using default: {default_profile!r}")
        profile_name = default_profile

    # Inject extracted_profile as implicit chain root if provided.
    # Achieved by temporarily adding it to a shallow copy of profiles under
    # the sentinel name "pkgbuild_extracted", then rebasing "bare"'s extends
    # onto it.
    if extracted_profile:
        profiles = dict(profiles)
        profiles["pkgbuild_extracted"] = {k: v for k, v in extracted_profile.items() if not k.startswith("__")}
        # Rebase bare onto extracted: if bare has no extends, give it one.
        bare = dict(profiles.get("bare", {}))
        if "extends" not in bare:
            bare["extends"] = "pkgbuild_extracted"
            profiles["bare"] = bare
        _log.info(f"[{pkgname}] Injected pkgbuild_extracted as chain root")

    result = merge_extends(profile_name, profiles, conflict_groups=conflict_groups)
    _log.debug(f"[{pkgname}] Full resolved profile ({profile_name}):\n{pprint.pformat(result, indent=2, sort_dicts=False)}")
    return result


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

def match_rules(pkgmeta, rules):
    """
    Evaluate rules against parsed PKGBUILD metadata.
    Conditions within a rule are AND'd; rules are OR'd.
    Returns list of matched rules, preserving order.
    """
    globals_ = pkgmeta.get("globals", {})

    pkgname = globals_.get("pkgname", "")
    if isinstance(pkgname, list):
        pkgnames = list(pkgname)
    else:
        pkgnames = [pkgname] if pkgname else []

    # Always include pkgbase in the names set. Split packages set pkgname to an
    # array of sub-package names (often containing unexpanded shell refs like
    # "$pkgbase") and put the canonical name in pkgbase — rules should match on
    # either. Single packages typically omit pkgbase; including it here is a no-op
    # when it equals pkgname or is absent.
    pkgbase = globals_.get("pkgbase", "")
    if pkgbase and pkgbase not in pkgnames:
        pkgnames.append(pkgbase)

    def _glob_any_match(patterns, values):
        return any(fnmatch.fnmatch(val, pat) for pat in patterns for val in values)

    def _glob_all_match(patterns, values):
        return all(any(fnmatch.fnmatch(val, pat) for val in values) for pat in patterns)

    def _glob_all_absent(patterns, values):
        return all(
            not any(fnmatch.fnmatch(val, pat) for val in values) for pat in patterns
        )

    def _exact_any(rule_key, meta_key):
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or bool(rule_vals & meta_vals)

    def _exact_all(rule_key, meta_key):
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or rule_vals.issubset(meta_vals)

    def _exact_all_absent(rule_key, meta_key):
        rule_vals = set(rule.get(rule_key, []))
        meta_vals = set(globals_.get(meta_key, []))
        return not rule_vals or not bool(rule_vals & meta_vals)

    matched = []
    for rule in rules:
        if "pkgnames" in rule:
            if not _glob_any_match(rule["pkgnames"], pkgnames):
                continue
        if "not_pkgnames" in rule:
            if not _glob_all_absent(rule["not_pkgnames"], pkgnames):
                continue
        if "groups" in rule:
            meta_groups = globals_.get("groups", [])
            if not _glob_all_match(rule["groups"], meta_groups):
                continue
        if "not_groups" in rule:
            if not _exact_all_absent("not_groups", "groups"):
                continue
        if "depends_any" in rule:
            if not _exact_any("depends_any", "depends"):
                continue
        if "depends_all" in rule:
            if not _exact_all("depends_all", "depends"):
                continue
        if "not_depends" in rule:
            if not _exact_all_absent("not_depends", "depends"):
                continue
        if "makedepends_any" in rule:
            if not _exact_any("makedepends_any", "makedepends"):
                continue
        if "makedepends_all" in rule:
            if not _exact_all("makedepends_all", "makedepends"):
                continue
        if "not_makedepends" in rule:
            if not _exact_all_absent("not_makedepends", "makedepends"):
                continue
        matched.append(rule)

    if matched:
        _log.debug(f"Matched {len(matched)} rule(s):")
        for rule in matched:
            _log.debug(pprint.pformat(rule, indent=2, sort_dicts=False))

    return matched


# ---------------------------------------------------------------------------
# Group resolution
# ---------------------------------------------------------------------------

def resolve_groups(pkgmeta, matched_rules, defaults):
    """
    Build the final groups list for a package.
    Starts with the PKGBUILD's own groups, then appends:
      - defaults.append_groups (always)
      - append_groups from each matched rule, in rule order
    Deduplicates while preserving insertion order.
    """
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    existing = list(pkgmeta.get("globals", {}).get("groups", []))
    to_append = list(defaults.get("append_groups", []))

    for rule in matched_rules:
        to_append.extend(rule.get("append_groups", []))

    seen = set(existing)
    for group in to_append:
        if group not in seen:
            existing.append(group)
            seen.add(group)

    _log.info(f"[{pkgname}] Resolved groups: {existing}")
    return existing


# ---------------------------------------------------------------------------
# Consumes resolution
# ---------------------------------------------------------------------------

def resolve_consumes(resolved_profile, pkgmeta, inference_map):
    """
    Determine the set of conf file types required for this build.

    Resolution order:
      1. Explicit `consumes` list on the profile -> use verbatim
      2. Auto-infer from makedepends via inference_map
      3. Always include "makepkg" as baseline

    Returns frozenset of conf type strings.
    """
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(pkgname, list):
        pkgname = pkgname[0]

    explicit = resolved_profile.get("consumes")
    if explicit is not None:
        active = frozenset(explicit)
        _log.info(f"[{pkgname}] consumes (explicit): {sorted(active)}")
        return active

    makedepends = set(pkgmeta.get("globals", {}).get("makedepends", []))
    inferred: set[str] = {"makepkg"}
    for dep in makedepends:
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
    _log.info(f"[{pkgname}] consumes (inferred from makedepends {matched_deps}): {sorted(active)}")
    return active


def serialize_flags(resolved_profile: dict) -> str:
    """
    Serialize a resolved profile to a stable, line-oriented string for drift detection.

    Format: sorted KEY=value lines, one per key, excluding sysforge-internal keys.
    Lists are joined with a single space. Used by build_state.toml flags_string
    and by converge to detect when a profile change affects a package's flags.
    """
    parts = []
    for key in sorted(resolved_profile):
        if key in SYSFORGE_KEYS:
            continue
        val = resolved_profile[key]
        if isinstance(val, list):
            parts.append(f"{key}={' '.join(str(v) for v in val)}")
        else:
            parts.append(f"{key}={val}")
    return "\n".join(parts)


def get_build_mode(matched_rules, config) -> str | None:
    """
    Return the build_mode from the winning rule's profile chain without
    performing a full profile resolution (and without logging).

    Walks the extends chain of the winning profile looking for a build_mode
    key. Returns None if no build_mode is found or no rules matched.
    Used by converge to determine whether extract_pkgbuild_profile is needed
    for the drift check.
    """
    profiles = config.get("profiles", {})
    defaults = config.get("defaults", {})

    winner = None
    for rule in matched_rules:
        if "profile" not in rule:
            continue
        if winner is None or rule.get("priority", 0) > winner.get("priority", 0):
            winner = rule

    profile_name = winner["profile"] if winner else defaults.get("profile", "bare")

    visited: set[str] = set()
    while profile_name and profile_name not in visited:
        visited.add(profile_name)
        p = profiles.get(profile_name, {})
        if "build_mode" in p:
            return p["build_mode"]
        profile_name = p.get("extends")

    return None
