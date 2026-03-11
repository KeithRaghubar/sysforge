#!/usr/bin/env python3
import sys
import os
import tomllib

sys.path.insert(0, os.path.expanduser("~/src/sysforge"))

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.makepkg_wrapper import match_rules

PKGBUILD_ALIASES = {
    "htop": f"{sys.path[0]}/tests/TEST_PKGBUILD_HTOP",
    "lib32": f"{sys.path[0]}/tests/TEST_PKGBUILD_LIB32",
    "llvm": f"{sys.path[0]}/tests/TEST_PKGBUILD_LLVM",
}
EXPECT_KEYS = {
    "htop": "_expect_htop",
    "lib32": "_expect_lib32",
    "llvm": "_expect_llvm",
}

alias = sys.argv[1] if len(sys.argv) > 1 else "lib32"
PKGBUILD = PKGBUILD_ALIASES.get(alias, alias)
expect_key = EXPECT_KEYS.get(alias, None)

PROFILES = (
    sys.argv[2] if len(sys.argv) > 2 else f"{sys.path[0]}/tests/test_flag_profiles.toml"
)

pkgmeta = parse_pkgbuild(PKGBUILD)
with open(PROFILES, "rb") as f:
    config = tomllib.load(f)

rules = config.get("rules", [])
clean_rules = [
    {k: v for k, v in r.items() if k.startswith("_") is False} for r in rules
]

print(f"=== Parsing: {PKGBUILD} ===")
print(f"pkgname:      {pkgmeta['globals'].get('pkgname')}")
print(f"groups:       {pkgmeta['globals'].get('groups', [])}")
print(f"makedepends:  {pkgmeta['globals'].get('makedepends', [])}")
print(f"depends:      {pkgmeta['globals'].get('depends', [])}")
print()

matched = match_rules(pkgmeta, clean_rules)
matched_ids = {id(r) for r in matched}

print(f"=== {len(rules)} rules in {PROFILES} ===\n")

passed = 0
failed = 0
no_expect = 0

for i, (rule, clean) in enumerate(zip(rules, clean_rules)):
    did_match = id(clean) in matched_ids
    expected = rule.get(expect_key) if expect_key else None
    status = "MATCH" if did_match else "SKIP "

    if expected is None:
        verdict = ""
        no_expect += 1
    elif did_match == expected:
        verdict = "✓"
        passed += 1
    else:
        verdict = "✗ UNEXPECTED"
        failed += 1

    print(f"  [{i}] {status} {verdict}")
    for k, v in clean.items():
        print(f"         {k}: {v!r}")
    print()

print("=== Summary ===")
print(f"  Passed:  {passed}")
print(f"  Failed:  {failed}")
if no_expect:
    print(f"  No expectation: {no_expect}")
print()
if failed:
    print("FAILURES detected.")
else:
    print("All rules matched expectations.")
