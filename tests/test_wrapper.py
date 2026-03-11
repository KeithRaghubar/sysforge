#!/usr/bin/env python3
import sys
import pprint

sys.path.insert(0, "$HOME/src/sysforge")

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.makepkg_wrapper import match_rules

PKGBUILD = sys.argv[1] if len(sys.argv) > 1 else "TEST_PKGBUILD"

pkgmeta = parse_pkgbuild(PKGBUILD)

rules = [
    # should match htop (exact pkgname)
    {
        "pkgname": "htop",
        "flags": {"CFLAGS": "-O3"},
    },
    # should match anything with cmake in makedepends
    {
        "makedepends": ["cmake"],
        "flags": {"CFLAGS": "-O2"},
    },
    # should match lib32-llvm (regex)
    {
        "pkgname_regex": r"lib32-llvm.*",
        "flags": {"CFLAGS": "-m32"},
    },
    # should match anything in group 'modified'
    {
        "groups": ["modified"],
        "flags": {"CFLAGS": "-march=native"},
    },
    # should NOT match htop (not_makedepends excludes cmake-free packages... inverted: excludes if cmake present)
    # actually tests not_pkgname
    {
        "not_pkgname": "htop",
        "flags": {"CFLAGS": "-pipe"},
    },
    # should match nothing — requires both git and nonexistent-dep
    {
        "makedepends": ["git", "nonexistent-dep"],
        "flags": {"CFLAGS": "-funroll-loops"},
    },
]

print(f"=== Parsing: {PKGBUILD} ===")
print(f"pkgname:      {pkgmeta['globals'].get('pkgname')}")
print(f"groups:       {pkgmeta['globals'].get('groups', [])}")
print(f"makedepends:  {pkgmeta['globals'].get('makedepends', [])}")
print(f"depends:      {pkgmeta['globals'].get('depends', [])}")
print()

matched = match_rules(pkgmeta, rules)

print(f"=== Matched {len(matched)}/{len(rules)} rules ===")
pprint.pprint(matched)
