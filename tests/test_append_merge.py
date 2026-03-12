#!/usr/bin/env python3
"""
Unit tests for [profiles.x.append] merge machinery.

Covers:
  - _extract_prefix
  - _merge_append_value  (plain append, prefix replace, conflict group)
  - merge_extends with append sub-dict
  - merge_extends direct key takes precedence over append key (same key in both)
  - merge_extends: append on root profile (no parent) emits warning and ignores
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.makepkg_wrapper import (
    _extract_prefix,
    _merge_append_value,
    merge_extends,
)

CONFLICT_GROUPS = {
    "pic":   ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"],
    "lto":   ["-flto", "-flto=thin", "-flto=full", "-fno-lto"],
    "stack": ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"],
}

# ---------------------------------------------------------------------------
# _extract_prefix
# ---------------------------------------------------------------------------

def test_prefix_opt_level():
    assert _extract_prefix("-O2") == "-O"
    assert _extract_prefix("-O3") == "-O"
    assert _extract_prefix("-O0") == "-O"

def test_prefix_eq():
    assert _extract_prefix("--icf=all") == "--icf="
    assert _extract_prefix("-flto=thin") == "-flto="
    assert _extract_prefix("-DFOO=bar") == "-DFOO="

def test_prefix_no_match():
    assert _extract_prefix("-pipe") is None
    assert _extract_prefix("-march=native") == "-march="  # has =

def test_prefix_trailing_digits_only():
    assert _extract_prefix("-g2") == "-g"

# ---------------------------------------------------------------------------
# _merge_append_value
# ---------------------------------------------------------------------------

def test_plain_append():
    result = _merge_append_value("-march=native -O2 -pipe", "--icf=all", {})
    assert result == "-march=native -O2 -pipe --icf=all"

def test_prefix_replace_opt():
    result = _merge_append_value("-march=native -O2 -pipe", "-O3", {})
    assert result == "-march=native -O3 -pipe"

def test_prefix_replace_icf():
    # --icf= prefix replacement works when LDFLAGS are space-separated tokens
    parent = "--as-needed --icf=safe -z relro"
    result = _merge_append_value(parent, "--icf=all", {})
    assert result == "--as-needed --icf=all -z relro"

def test_prefix_no_replace_packed_wl():
    # Packed -Wl,... is a single token — --icf=all has no prefix match, so it appends
    parent = "-Wl,-O1,--sort-common,--icf=safe"
    result = _merge_append_value(parent, "--icf=all", {})
    assert result == "-Wl,-O1,--sort-common,--icf=safe --icf=all"

def test_conflict_group_stack():
    parent = "-march=native -O2 -pipe -fstack-protector"
    result = _merge_append_value(parent, "-fno-stack-protector", CONFLICT_GROUPS)
    assert result == "-march=native -O2 -pipe -fno-stack-protector"

def test_conflict_group_replaces_multiple():
    # parent has -fstack-protector-strong; child brings -fno-stack-protector
    parent = "-O2 -fstack-protector-strong -pipe"
    result = _merge_append_value(parent, "-fno-stack-protector", CONFLICT_GROUPS)
    assert result == "-O2 -pipe -fno-stack-protector"

def test_conflict_group_lto():
    parent = "-O3 -flto -pipe"
    result = _merge_append_value(parent, "-flto=thin", CONFLICT_GROUPS)
    assert result == "-O3 -pipe -flto=thin"

def test_conflict_group_takes_precedence_over_prefix():
    # -flto=thin would also match prefix "-flto=", but conflict group should fire first
    parent = "-O3 -flto=full -pipe"
    result = _merge_append_value(parent, "-flto=thin", CONFLICT_GROUPS)
    # -flto=thin is in lto group → removes -flto=full, appends -flto=thin
    assert result == "-O3 -pipe -flto=thin"

def test_full_example_from_design_doc():
    parent = "-march=native -O2 -pipe -fstack-protector"
    child_append = "-O3 -fno-stack-protector --icf=all"
    result = _merge_append_value(parent, child_append, CONFLICT_GROUPS)
    assert result == "-march=native -O3 -pipe -fno-stack-protector --icf=all"

def test_empty_parent():
    result = _merge_append_value("", "-O2 -pipe", {})
    assert result == "-O2 -pipe"

def test_empty_append():
    result = _merge_append_value("-O2 -pipe", "", {})
    assert result == "-O2 -pipe"

# ---------------------------------------------------------------------------
# merge_extends with append sub-dict
# ---------------------------------------------------------------------------

PROFILES_BASIC = {
    "bare": {},
    "standard": {
        "extends": "bare",
        "CFLAGS": "-march=native -O2 -pipe",
        "LDFLAGS": "-Wl,--as-needed",
    },
    "optimized": {
        "extends": "standard",
        "append": {
            "CFLAGS": "-O3 --icf=all",
        },
    },
}

def test_merge_extends_append_basic():
    result = merge_extends("optimized", PROFILES_BASIC, conflict_groups={})
    # -O3 replaces -O2 (prefix); --icf=all appends
    assert result["CFLAGS"] == "-march=native -O3 -pipe --icf=all"
    # LDFLAGS inherited unchanged
    assert result["LDFLAGS"] == "-Wl,--as-needed"

def test_merge_extends_direct_wins_over_append():
    """A key set both directly and in append: direct takes precedence."""
    profiles = {
        "bare": {},
        "child": {
            "extends": "bare",
            "CFLAGS": "-O2",
            "append": {"CFLAGS": "-O3"},
        },
    }
    result = merge_extends("child", profiles, conflict_groups={})
    assert result["CFLAGS"] == "-O2"

def test_merge_extends_append_on_root_ignored(capsys=None):
    """append on a root profile (no extends) is ignored without crashing."""
    profiles = {
        "root": {
            "CFLAGS": "-O2",
            "append": {"CFLAGS": "-O3"},
        },
    }
    result = merge_extends("root", profiles, conflict_groups={})
    assert result["CFLAGS"] == "-O2"

def test_merge_extends_conflict_group_via_append():
    profiles = {
        "bare": {},
        "standard": {
            "extends": "bare",
            "CFLAGS": "-O2 -fstack-protector -pipe",
        },
        "hardened": {
            "extends": "standard",
            "append": {"CFLAGS": "-fstack-protector-strong"},
        },
    }
    result = merge_extends("hardened", profiles, conflict_groups=CONFLICT_GROUPS)
    assert "-fstack-protector-strong" in result["CFLAGS"]
    assert "-fstack-protector" not in result["CFLAGS"].replace("-fstack-protector-strong", "")

def test_merge_extends_multi_level_append():
    """Append across a three-level chain: bare → standard → optimized."""
    profiles = {
        "bare": {},
        "standard": {
            "extends": "bare",
            "CFLAGS": "-O2 -pipe",
        },
        "optimized": {
            "extends": "standard",
            "append": {"CFLAGS": "-O3 -fno-plt"},
        },
    }
    result = merge_extends("optimized", profiles, conflict_groups={})
    assert result["CFLAGS"] == "-O3 -pipe -fno-plt"

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
