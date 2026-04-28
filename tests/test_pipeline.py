"""
test_pipeline.py — integration tests for config loading, profile resolution,
rule matching, group resolution, and makepkg conf emission.

These tests require SYSFORGE_CONFIG_DIR to point at tests/data, which is
handled by conftest.py. All tests here use the test fixture configs under
tests/data/etc/sysforge/ and tests/data/user/.config/sysforge/.
"""
import os
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"
SYS_CONFIG = TEST_DATA / "etc/sysforge/profiles.toml"
USER_CONFIG = TEST_DATA / "user/.config/sysforge/profiles.toml"

PKGBUILDS = {
    "htop":  TEST_DATA / "PKGBUILDs/htop.PKGBUILD",
    "llvm":  TEST_DATA / "PKGBUILDs/llvm.PKGBUILD",
    "lib32": TEST_DATA / "PKGBUILDs/lib32-llvm.PKGBUILD",
}

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.config import load_config
from sysforge.primitives.profile import (
    merge_extends,
    match_rules,
    resolve_profile,
    resolve_groups,
)
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf


# ---------------------------------------------------------------------------
# merge_extends
# ---------------------------------------------------------------------------

@pytest.fixture
def test_profiles():
    import tomllib
    with open(TEST_DATA / "test_profiles.toml", "rb") as f:
        return tomllib.load(f)["profiles"]


def test_merge_extends_bare_empty(test_profiles):
    assert merge_extends("bare", test_profiles) == {}

def test_merge_extends_standard_cflags(test_profiles):
    resolved = merge_extends("standard", test_profiles)
    assert resolved.get("CFLAGS") == "-march=native -O2 -pipe"

def test_merge_extends_optimized_overrides(test_profiles):
    resolved = merge_extends("optimized", test_profiles)
    assert resolved.get("CFLAGS") == "-march=native -O3 -pipe"

def test_merge_extends_cosmic_inherits(test_profiles):
    resolved = merge_extends("cosmic", test_profiles)
    assert resolved.get("CFLAGS") == "-march=native -O3 -pipe"

def test_merge_extends_strips_extends_key(test_profiles):
    resolved = merge_extends("cosmic", test_profiles)
    assert "extends" not in resolved

def test_merge_extends_cycle_raises(test_profiles):
    cycle = {"a": {"extends": "b"}, "b": {"extends": "a"}}
    with pytest.raises(ValueError):
        merge_extends("a", cycle)

def test_merge_extends_missing_raises(test_profiles):
    with pytest.raises(ValueError):
        merge_extends("nonexistent", test_profiles)


# ---------------------------------------------------------------------------
# match_rules — pkgbase matching
# ---------------------------------------------------------------------------

def test_match_rules_pkgbase_matches_pkgnames_rule():
    """Rule with pkgnames matches a split-package PKGBUILD via pkgbase."""
    # Simulates a kernel-style PKGBUILD: pkgbase=linux-custom,
    # pkgname=("$pkgbase" "$pkgbase-headers") — unexpanded shell refs.
    meta = {"globals": {
        "pkgbase": "linux-custom",
        "pkgname": ["$pkgbase", "$pkgbase-headers"],
    }}
    rules = [{"pkgnames": ["linux-custom"], "profile": "kernel", "priority": 20}]
    matched = match_rules(meta, rules)
    assert len(matched) == 1
    assert matched[0]["profile"] == "kernel"

def test_match_rules_pkgbase_not_double_counted():
    """When pkgbase equals pkgname (single pkg), it is not duplicated."""
    meta = {"globals": {"pkgbase": "htop", "pkgname": "htop"}}
    rules = [{"pkgnames": ["htop"], "profile": "optimized", "priority": 0}]
    matched = match_rules(meta, rules)
    assert len(matched) == 1

def test_match_rules_pkgbase_higher_priority_wins():
    """pkgbase-matched kernel rule at priority 20 beats groups rule at 10."""
    meta = {"globals": {
        "pkgbase": "linux-custom",
        "pkgname": ["$pkgbase", "$pkgbase-headers"],
        "groups": ["modified"],
    }}
    rules = [
        {"groups": ["modified"], "profile": "patched", "priority": 10},
        {"pkgnames": ["linux-custom"], "profile": "kernel", "priority": 20},
    ]
    matched = match_rules(meta, rules)
    assert len(matched) == 2
    winner = max(matched, key=lambda r: r["priority"])
    assert winner["profile"] == "kernel"


# ---------------------------------------------------------------------------
# load_config — system only
# ---------------------------------------------------------------------------

@pytest.fixture
def sys_config():
    return load_config(config_paths=[SYS_CONFIG])

def test_load_config_has_profiles(sys_config):
    assert "profiles" in sys_config

def test_load_config_has_rules(sys_config):
    assert "rules" in sys_config

def test_load_config_has_defaults(sys_config):
    assert "defaults" in sys_config

def test_load_config_default_profile(sys_config):
    assert sys_config["defaults"].get("profile") == "standard"


# ---------------------------------------------------------------------------
# load_config — extends_system merge
# ---------------------------------------------------------------------------

@pytest.fixture
def merged_config():
    return load_config(config_paths=[USER_CONFIG, SYS_CONFIG])

def test_user_overrides_standard_cflags(merged_config):
    assert merged_config["profiles"]["standard"].get("CFLAGS") == \
        "-march=native -O2 -pipe -fstack-protector-strong"

def test_user_profile_custom_present(merged_config):
    assert "custom" in merged_config["profiles"]

def test_system_profile_optimized_preserved(merged_config):
    assert "optimized" in merged_config["profiles"]

def test_user_rule_priority_bumped(merged_config):
    rule = next(
        (r for r in merged_config["rules"]
         if r.get("pkgnames") == ["htop"] and r.get("profile") == "custom"),
        None,
    )
    assert rule is not None
    assert rule["priority"] == 105  # 5 + 100 user bump

def test_system_rule_priority_unchanged(merged_config):
    rule = next(
        (r for r in merged_config["rules"]
         if r.get("pkgnames") == ["htop"] and r.get("profile") == "optimized"),
        None,
    )
    assert rule is not None
    assert rule["priority"] == 0

def test_extends_system_stripped(merged_config):
    assert "extends_system" not in merged_config

def test_user_rule_wins_for_htop(merged_config):
    meta = parse_pkgbuild(PKGBUILDS["htop"])
    matched = match_rules(meta, merged_config["rules"])
    profile = resolve_profile(meta, matched, merged_config)
    assert profile.get("RUSTFLAGS") == "-C opt-level=3"


# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------

@pytest.fixture
def htop_profile(sys_config):
    meta = parse_pkgbuild(PKGBUILDS["htop"])
    matched = match_rules(meta, sys_config["rules"])
    return resolve_profile(meta, matched, sys_config)

@pytest.fixture
def llvm_profile(sys_config):
    meta = parse_pkgbuild(PKGBUILDS["llvm"])
    matched = match_rules(meta, sys_config["rules"])
    return resolve_profile(meta, matched, sys_config)

def test_htop_resolves_optimized_cflags(htop_profile):
    assert htop_profile.get("CFLAGS") == "-march=native -O3 -pipe -fno-plt"

def test_llvm_resolves_pgo_build_mode(llvm_profile):
    assert llvm_profile.get("build_mode") == "pgo_llvm_toolchain"

def test_unknown_pkg_fallback_to_default(sys_config):
    meta = {"globals": {"pkgname": "totally-unknown-pkg"}}
    matched = match_rules(meta, sys_config["rules"])
    profile = resolve_profile(meta, matched, sys_config)
    assert profile.get("CFLAGS") == "-march=native -O2 -pipe"


# ---------------------------------------------------------------------------
# resolve_groups
# ---------------------------------------------------------------------------

@pytest.fixture
def sys_metas_matched(sys_config):
    metas = {k: parse_pkgbuild(v) for k, v in PKGBUILDS.items()}
    matched = {k: match_rules(metas[k], sys_config["rules"]) for k in metas}
    defaults = sys_config.get("defaults", {})
    return metas, matched, defaults

def test_htop_gets_sf_build(sys_metas_matched):
    metas, matched, defaults = sys_metas_matched
    groups = resolve_groups(metas["htop"], matched["htop"], defaults)
    assert "sf-build" in groups

def test_llvm_gets_sf_build_and_pgo(sys_metas_matched):
    metas, matched, defaults = sys_metas_matched
    groups = resolve_groups(metas["llvm"], matched["llvm"], defaults)
    assert "sf-build" in groups
    assert "pgo" in groups

def test_lib32_retains_modified_group(sys_metas_matched):
    metas, matched, defaults = sys_metas_matched
    groups = resolve_groups(metas["lib32"], matched["lib32"], defaults)
    assert "modified" in groups

def test_no_duplicate_groups(sys_metas_matched):
    metas, matched, defaults = sys_metas_matched
    groups = resolve_groups(metas["lib32"], matched["lib32"], defaults)
    assert len(groups) == len(set(groups))


# ---------------------------------------------------------------------------
# emit_makepkg_conf
# ---------------------------------------------------------------------------

def test_conf_contains_cflags(htop_profile):
    with emit_makepkg_conf(htop_profile) as conf_path:
        content = open(conf_path).read()
    assert "CFLAGS=" in content

def test_conf_excludes_build_mode(htop_profile):
    with emit_makepkg_conf(htop_profile) as conf_path:
        content = open(conf_path).read()
    assert "build_mode" not in content

def test_conf_excludes_makepkg_flags(htop_profile):
    with emit_makepkg_conf(htop_profile) as conf_path:
        content = open(conf_path).read()
    assert "makepkg_flags" not in content

def test_conf_tempfile_cleaned_up(htop_profile):
    with emit_makepkg_conf(htop_profile) as conf_path:
        p = Path(conf_path)
        assert p.exists()
    assert not p.exists()
