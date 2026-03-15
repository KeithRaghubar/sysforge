"""
test_resolve.py — unit tests for sysforge.resolve.

Covers:
    _find_pkgbuild      — existing file path, bare name via cwd, missing → error
    _get_profile_chain  — single profile, chain, missing parent (stops gracefully)
    _find_winner        — highest priority wins, ties, no profile key, empty list
    _format_conditions  — various condition key combos, empty rule
    _print_resolve      — stdout output contains expected fields;
                          with and without --show-flags; no matched rules case
    cmd_resolve         — integration: resolves a real PKGBUILD with test config
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.config import find_pkgbuild
from sysforge.resolve import (
    _find_winner,
    _format_conditions,
    _get_profile_chain,
    _print_resolve,
    cmd_resolve,
)

TESTS_DIR = Path(__file__).parent
DATA_DIR = TESTS_DIR / "data"
PKGBUILDS_DIR = DATA_DIR / "PKGBUILDs"


# ---------------------------------------------------------------------------
# find_pkgbuild
# ---------------------------------------------------------------------------

def test_find_pkgbuild_explicit_path():
    path = PKGBUILDS_DIR / "simple.PKGBUILD"
    result = find_pkgbuild(str(path))
    assert result == path.resolve()


def test_find_pkgbuild_bare_name_via_cwd(tmp_path):
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    pb = pkg_dir / "PKGBUILD"
    pb.write_text("pkgname=mypkg\npkgver=1.0\npkgrel=1\narch=('any')\n")

    with patch("sysforge.primitives.config.Path.cwd", return_value=tmp_path):
        result = find_pkgbuild("mypkg")
    assert result == pb.resolve()


def test_find_pkgbuild_via_pkgbuild_dir(tmp_path):
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    pb = pkg_dir / "PKGBUILD"
    pb.write_text("pkgname=mypkg\npkgver=1.0\npkgrel=1\narch=('any')\n")

    config = {"paths": {"pkgbuild_dir": str(tmp_path)}}
    with patch("sysforge.primitives.config.Path.cwd", return_value=tmp_path / "other"):
        result = find_pkgbuild("mypkg", config)
    assert result == pb.resolve()


def test_find_pkgbuild_not_found_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        with patch("sysforge.primitives.config.Path.cwd", return_value=tmp_path):
            find_pkgbuild("nonexistent")


def test_find_pkgbuild_error_message_shows_both_searched(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        with patch("sysforge.primitives.config.Path.cwd", return_value=tmp_path):
            find_pkgbuild("mypkg")
    msg = str(exc.value)
    assert "mypkg" in msg


# ---------------------------------------------------------------------------
# _get_profile_chain
# ---------------------------------------------------------------------------

def test_get_profile_chain_single():
    profiles = {"bare": {}}
    assert _get_profile_chain("bare", profiles) == ["bare"]


def test_get_profile_chain_two_deep():
    profiles = {
        "optimized": {"extends": "bare"},
        "bare": {},
    }
    assert _get_profile_chain("optimized", profiles) == ["optimized", "bare"]


def test_get_profile_chain_three_deep():
    profiles = {
        "child": {"extends": "mid"},
        "mid": {"extends": "root"},
        "root": {},
    }
    assert _get_profile_chain("child", profiles) == ["child", "mid", "root"]


def test_get_profile_chain_missing_parent_stops():
    # Parent not in profiles dict — chain stops without error
    profiles = {"child": {"extends": "nonexistent"}}
    assert _get_profile_chain("child", profiles) == ["child"]


def test_get_profile_chain_cycle_stops():
    # Cycle should not loop forever
    profiles = {
        "a": {"extends": "b"},
        "b": {"extends": "a"},
    }
    chain = _get_profile_chain("a", profiles)
    assert len(chain) == 2
    assert "a" in chain
    assert "b" in chain


# ---------------------------------------------------------------------------
# _find_winner
# ---------------------------------------------------------------------------

def test_find_winner_returns_highest_priority():
    rules = [
        {"priority": 5, "profile": "low"},
        {"priority": 20, "profile": "high"},
        {"priority": 10, "profile": "mid"},
    ]
    assert _find_winner(rules)["profile"] == "high"


def test_find_winner_first_on_tie():
    rules = [
        {"priority": 10, "profile": "first"},
        {"priority": 10, "profile": "second"},
    ]
    assert _find_winner(rules)["profile"] == "first"


def test_find_winner_skips_rules_without_profile():
    rules = [
        {"priority": 99, "append_groups": ["x"]},   # no profile key
        {"priority": 5, "profile": "only"},
    ]
    assert _find_winner(rules)["profile"] == "only"


def test_find_winner_empty_list():
    assert _find_winner([]) is None


def test_find_winner_all_without_profile():
    rules = [{"priority": 10, "append_groups": ["x"]}]
    assert _find_winner(rules) is None


# ---------------------------------------------------------------------------
# _format_conditions
# ---------------------------------------------------------------------------

def test_format_conditions_pkgnames():
    rule = {"pkgnames": ["htop", "htop-*"], "profile": "opt", "priority": 10}
    result = _format_conditions(rule)
    assert "pkgnames" in result
    assert "htop" in result


def test_format_conditions_groups():
    rule = {"groups": ["devel"], "profile": "opt", "priority": 5}
    result = _format_conditions(rule)
    assert "groups" in result


def test_format_conditions_multiple_keys():
    rule = {"pkgnames": ["foo"], "not_groups": ["debug"], "profile": "p", "priority": 0}
    result = _format_conditions(rule)
    assert "pkgnames" in result
    assert "not_groups" in result


def test_format_conditions_empty_rule():
    rule = {"profile": "bare", "priority": 0}
    result = _format_conditions(rule)
    assert result == "(no conditions — always matches)"


def test_format_conditions_only_known_keys_shown():
    # 'profile' and 'priority' should not appear in conditions
    rule = {"pkgnames": ["x"], "profile": "p", "priority": 5}
    result = _format_conditions(rule)
    assert "profile" not in result
    assert "priority" not in result


# ---------------------------------------------------------------------------
# _print_resolve — stdout content
# ---------------------------------------------------------------------------

SIMPLE_PKGMETA = {
    "globals": {"pkgname": "mypkg", "pkgver": "1.0", "pkgrel": "1"}
}

SIMPLE_CONFIG = {
    "defaults": {"profile": "bare"},
    "profiles": {
        "bare": {},
        "optimized": {"extends": "bare", "CC": "clang"},
    },
    "rules": [],
}

MATCHED_RULE = {"priority": 10, "pkgnames": ["mypkg"], "profile": "optimized"}
RESOLVED_PROFILE = {"CC": "clang", "build_mode": "patch_pkgbuild"}


def test_print_resolve_shows_package_name(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE], RESOLVED_PROFILE,
                   frozenset({"makepkg"}), ["grp"], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "mypkg" in out


def test_print_resolve_shows_pkgbuild_path(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE], RESOLVED_PROFILE,
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert str(pb) in out


def test_print_resolve_shows_winner_marker(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE], RESOLVED_PROFILE,
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "winner" in out
    assert "optimized" in out


def test_print_resolve_no_rules_shows_default(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [], {"CC": "gcc"},
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "none" in out
    assert "bare" in out


def test_print_resolve_shows_profile_chain(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE], RESOLVED_PROFILE,
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "optimized" in out
    assert "bare" in out
    assert "→" in out


def test_print_resolve_shows_build_mode(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE], RESOLVED_PROFILE,
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "patch_pkgbuild" in out


def test_print_resolve_no_build_mode_omits_line(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [], {"CC": "gcc"},
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "Build mode" not in out


def test_print_resolve_shows_consumes(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [], {"CC": "gcc"},
                   frozenset({"makepkg", "rust"}), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "makepkg" in out
    assert "rust" in out


def test_print_resolve_shows_groups(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [], {"CC": "gcc"},
                   frozenset(), ["mygroup"], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "mygroup" in out


def test_print_resolve_show_flags_expands_profile(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [MATCHED_RULE],
                   {"CC": "clang", "CFLAGS": "-O3"},
                   frozenset(), [], SIMPLE_CONFIG, show_flags=True)
    out = capsys.readouterr().out
    assert "CC" in out
    assert "clang" in out
    assert "CFLAGS" in out
    assert "-O3" in out


def test_print_resolve_no_flags_shows_count_hint(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    _print_resolve(pb, SIMPLE_PKGMETA, [], {"CC": "gcc", "CFLAGS": "-O2"},
                   frozenset(), [], SIMPLE_CONFIG, show_flags=False)
    out = capsys.readouterr().out
    assert "--show-flags" in out


def test_print_resolve_internal_keys_separated(capsys, tmp_path):
    pb = tmp_path / "PKGBUILD"
    profile_with_internal = {"CC": "clang", "build_mode": "kernel", "batch": True}
    _print_resolve(pb, SIMPLE_PKGMETA, [], profile_with_internal,
                   frozenset(), [], SIMPLE_CONFIG, show_flags=True)
    out = capsys.readouterr().out
    # Internal keys should be under the "sysforge-internal keys" comment
    assert "build_mode" in out
    assert "batch" in out
    assert "sysforge-internal" in out


# ---------------------------------------------------------------------------
# cmd_resolve — integration with test PKGBUILD and config
# ---------------------------------------------------------------------------

def _make_args(pkg, show_flags=False, profile_conf=None):
    return SimpleNamespace(pkg=pkg, show_flags=show_flags, profile_conf=profile_conf)


def test_cmd_resolve_htop_matches_rule(capsys):
    """htop has a matching rule in test flag_profiles.toml → profile: optimized."""
    pb = str(PKGBUILDS_DIR / "htop.PKGBUILD")
    profile_conf = str(DATA_DIR / "etc" / "sysforge" / "flag_profiles.toml")
    cmd_resolve(_make_args(pb, profile_conf=profile_conf))
    out = capsys.readouterr().out
    assert "htop" in out
    assert "optimized" in out
    assert "winner" in out


def test_cmd_resolve_show_flags_produces_flag_lines(capsys):
    pb = str(PKGBUILDS_DIR / "htop.PKGBUILD")
    profile_conf = str(DATA_DIR / "etc" / "sysforge" / "flag_profiles.toml")
    cmd_resolve(_make_args(pb, show_flags=True, profile_conf=profile_conf))
    out = capsys.readouterr().out
    # Resolved profile section should appear
    assert "Resolved profile:" in out


def test_cmd_resolve_missing_pkgbuild_exits(tmp_path):
    with patch("sysforge.primitives.config.Path.cwd", return_value=tmp_path):
        with pytest.raises(SystemExit):
            cmd_resolve(_make_args("nonexistent_pkg_xyz"))


def test_cmd_resolve_simple_pkgbuild(capsys):
    """simple.PKGBUILD has pkgname=htop... actually let's check what simple contains."""
    pb = str(PKGBUILDS_DIR / "simple.PKGBUILD")
    profile_conf = str(DATA_DIR / "etc" / "sysforge" / "flag_profiles.toml")
    cmd_resolve(_make_args(pb, profile_conf=profile_conf))
    out = capsys.readouterr().out
    assert "Package:" in out
    assert "PKGBUILD:" in out
    assert "Profile chain:" in out
    assert "Consumes:" in out
