#!/usr/bin/env python3
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/src/sysforge"))

# Point SYSFORGE_CONFIG_DIR at test data so load_config finds the system config
TEST_ROOT = f"{sys.path[0]}/tests/data"
os.environ["SYSFORGE_CONFIG_DIR"] = TEST_ROOT

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.makepkg_wrapper import (
    load_config,
    merge_extends,
    match_rules,
    resolve_profile,
    resolve_groups,
    emit_makepkg_conf,
)

PKGBUILDS = {
    "htop":  f"{sys.path[0]}/tests/data/PKGBUILDs/htop.PKGBUILD",
    "llvm":  f"{sys.path[0]}/tests/data/PKGBUILDs/llvm.PKGBUILD",
    "lib32": f"{sys.path[0]}/tests/data/PKGBUILDs/lib32-llvm.PKGBUILD",
}

USER_CONFIG  = Path(f"{TEST_ROOT}/user/.config/sysforge/flag_profiles.toml")
SYS_CONFIG   = Path(f"{TEST_ROOT}/etc/sysforge/flag_profiles.toml")

passed = 0
failed = 0


def check(label, actual, expected):
    global passed, failed
    if actual == expected:
        print(f"  ✓ {label}")
        passed += 1
    else:
        print(f"  ✗ {label}")
        print(f"      expected: {expected!r}")
        print(f"      actual:   {actual!r}")
        failed += 1


def check_raises(label, fn, exc_type):
    global passed, failed
    try:
        fn()
        print(f"  ✗ {label} (no exception raised)")
        failed += 1
    except exc_type:
        print(f"  ✓ {label}")
        passed += 1
    except Exception as e:
        print(f"  ✗ {label} (wrong exception: {type(e).__name__}: {e})")
        failed += 1


# ---------------------------------------------------------------------------
print("=== merge_extends ===")
# ---------------------------------------------------------------------------

with open(f"{TEST_ROOT}/test_flag_profiles.toml", "rb") as f:
    test_config = tomllib.load(f)
profiles = test_config["profiles"]

# bare has no keys
check("bare resolves to empty", merge_extends("bare", profiles), {})

# standard inherits bare, adds CFLAGS
resolved = merge_extends("standard", profiles)
check("standard.CFLAGS", resolved.get("CFLAGS"), "-march=native -O2 -pipe")

# optimized overrides CFLAGS
resolved = merge_extends("optimized", profiles)
check(
    "optimized.CFLAGS overrides standard",
    resolved.get("CFLAGS"),
    "-march=native -O3 -pipe",
)

# cosmic inherits optimized, adds nothing new — CFLAGS should still be optimized's
resolved = merge_extends("cosmic", profiles)
check(
    "cosmic inherits optimized.CFLAGS",
    resolved.get("CFLAGS"),
    "-march=native -O3 -pipe",
)

# extends key should not appear in resolved output
check("extends stripped from resolved", "extends" not in resolved, True)

# cycle detection
cycle_profiles = {
    "a": {"extends": "b"},
    "b": {"extends": "a"},
}
check_raises(
    "cycle detection raises ValueError",
    lambda: merge_extends("a", cycle_profiles),
    ValueError,
)

# missing profile
check_raises(
    "missing profile raises ValueError",
    lambda: merge_extends("nonexistent", profiles),
    ValueError,
)


# ---------------------------------------------------------------------------
print("\n=== load_config (system only) ===")
# ---------------------------------------------------------------------------

config = load_config(config_paths=[SYS_CONFIG])
check("config has profiles", "profiles" in config, True)
check("config has rules",    "rules" in config, True)
check("config has defaults", "defaults" in config, True)
check("default profile is standard", config["defaults"].get("profile"), "standard")


# ---------------------------------------------------------------------------
print("\n=== load_config (extends_system merge) ===")
# ---------------------------------------------------------------------------

merged = load_config(config_paths=[USER_CONFIG, SYS_CONFIG])

# User overrides existing system profile key
check(
    "user overrides standard.CFLAGS",
    merged["profiles"]["standard"].get("CFLAGS"),
    "-march=native -O2 -pipe -fstack-protector-strong",
)

# New user profile present
check("user profile 'custom' present", "custom" in merged["profiles"], True)

# System profiles still present
check("system profile 'optimized' still present", "optimized" in merged["profiles"], True)

# User rule bumped to priority 105 (5 + 100)
user_rule = next(
    (r for r in merged["rules"]
     if r.get("pkgnames") == ["htop"] and r.get("profile") == "custom"),
    None,
)
check("user rule exists after merge", user_rule is not None, True)
check(
    "user rule priority bumped by 100",
    user_rule.get("priority") if user_rule else None,
    105,
)

# System rules still present and untouched
system_rule = next(
    (r for r in merged["rules"]
     if r.get("pkgnames") == ["htop"] and r.get("profile") == "optimized"),
    None,
)
check("system rule still present", system_rule is not None, True)
check(
    "system rule priority unchanged",
    system_rule.get("priority") if system_rule else None,
    0,
)

# extends_system key stripped from merged config
check("extends_system stripped from merged config", "extends_system" not in merged, True)

# User rule wins over system rule for htop (priority 105 vs 0)
htop_meta = parse_pkgbuild(PKGBUILDS["htop"])
htop_matched = match_rules(htop_meta, merged["rules"])
htop_profile = resolve_profile(htop_meta, htop_matched, merged)
check(
    "user rule wins for htop — resolves custom profile",
    htop_profile.get("RUSTFLAGS"),
    "-C opt-level=3",
)


# ---------------------------------------------------------------------------
print("\n=== resolve_profile ===")
# ---------------------------------------------------------------------------

llvm_meta  = parse_pkgbuild(PKGBUILDS["llvm"])
lib32_meta = parse_pkgbuild(PKGBUILDS["lib32"])

# Use system-only config for the remaining tests
htop_matched = match_rules(htop_meta, config["rules"])
htop_profile = resolve_profile(htop_meta, htop_matched, config)
check(
    "htop resolves to optimized CFLAGS",
    htop_profile.get("CFLAGS"),
    "-march=native -O3 -pipe -fno-plt",
)

# llvm matches rule with profile=pgo_llvm_toolchain
llvm_matched = match_rules(llvm_meta, config["rules"])
llvm_profile = resolve_profile(llvm_meta, llvm_matched, config)
check(
    "llvm resolves build_mode=pgo_llvm_toolchain",
    llvm_profile.get("build_mode"),
    "pgo_llvm_toolchain",
)

# no-match fallback: use a fresh meta with a pkgname that matches nothing
no_match_meta = {"globals": {"pkgname": "totally-unknown-pkg"}}
no_match_rules = match_rules(no_match_meta, config["rules"])
no_match_profile = resolve_profile(no_match_meta, no_match_rules, config)
check(
    "unknown pkg falls back to default profile CFLAGS",
    no_match_profile.get("CFLAGS"),
    "-march=native -O2 -pipe",
)


# ---------------------------------------------------------------------------
print("\n=== resolve_groups ===")
# ---------------------------------------------------------------------------

defaults = config.get("defaults", {})

# htop: no existing groups, gets defaults.append_groups
htop_groups = resolve_groups(htop_meta, htop_matched, defaults)
check("htop gets sf-build from defaults", "sf-build" in htop_groups, True)

# llvm: matched pgo rule which has append_groups=["pgo"]
llvm_groups = resolve_groups(llvm_meta, llvm_matched, defaults)
check("llvm gets sf-build from defaults", "sf-build" in llvm_groups, True)
check("llvm gets pgo from rule",          "pgo" in llvm_groups, True)

# lib32-llvm has groups=["modified"] in the PKGBUILD — should be preserved
lib32_matched = match_rules(lib32_meta, config["rules"])
lib32_groups = resolve_groups(lib32_meta, lib32_matched, defaults)
check("lib32 retains existing group modified", "modified" in lib32_groups, True)

# no duplicates
check("no duplicate groups", len(lib32_groups) == len(set(lib32_groups)), True)


# ---------------------------------------------------------------------------
print("\n=== emit_makepkg_conf ===")
# ---------------------------------------------------------------------------

import os as _os

with emit_makepkg_conf(htop_profile) as conf_path:
    check("temp file exists during context", _os.path.exists(conf_path), True)
    with open(conf_path) as f:
        contents = f.read()
    check("CFLAGS written to conf",              "CFLAGS=" in contents,       True)
    check("build_mode not written to conf",      "build_mode" in contents,    False)
    check("makepkg_flags not written to conf",   "makepkg_flags" in contents, False)

check("temp file removed after context", _os.path.exists(conf_path), False)


# ---------------------------------------------------------------------------
print(f"\n=== Summary ===")
print(f"  Passed:  {passed}")
print(f"  Failed:  {failed}")
print()
if failed:
    print("FAILURES detected.")
else:
    print("All checks passed.")
