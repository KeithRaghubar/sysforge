"""
test_cli.py — unit tests for sysforge.cli helper functions.

Tests the pure, stateless preprocessing functions that have non-trivial
logic. Does not test main() or the _cmd_* handlers (those require the
full build pipeline and subprocess invocation).

Covers:
    _hoist_verbosity_flags   — hoists -v/-vv/-vvv/--verbose before subcommand,
                               leaves non-verbose flags in place, handles mixed
                               order, deduplicates multiple -v flags correctly
    _patch_makepkg_argv      — rewrites -m <value-starting-with-dash> to
                               --makepkg=<value>, passes through other tokens
    expand_makepkg_flags     — expands combined short flags, passes long flags,
                               handles empty/None input
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.cli import (
    _extract_implicit_makepkg_flags,
    _hoist_verbosity_flags,
    _patch_makepkg_argv,
)
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags


# ---------------------------------------------------------------------------
# _hoist_verbosity_flags
# ---------------------------------------------------------------------------

def test_hoist_v_before_subcommand():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-v"])
    assert result == ["-v", "build", "PKGBUILD"]


def test_hoist_vv_before_subcommand():
    result = _hoist_verbosity_flags(["pipeline", "-vv"])
    assert result == ["-vv", "pipeline"]


def test_hoist_vvv_before_subcommand():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-vvv", "--interactive"])
    assert result == ["-vvv", "build", "PKGBUILD", "--interactive"]


def test_hoist_verbose_long_form():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--verbose"])
    assert result == ["--verbose", "build", "PKGBUILD"]


def test_hoist_multiple_v_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "-v", "-v"])
    assert result == ["-v", "-v", "build", "PKGBUILD"]


def test_hoist_already_at_front():
    result = _hoist_verbosity_flags(["-vv", "build", "PKGBUILD"])
    assert result == ["-vv", "build", "PKGBUILD"]


def test_hoist_no_verbose_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--interactive"])
    assert result == ["build", "PKGBUILD", "--interactive"]


def test_hoist_empty_argv():
    assert _hoist_verbosity_flags([]) == []


def test_hoist_only_verbose():
    result = _hoist_verbosity_flags(["-v"])
    assert result == ["-v"]


def test_hoist_mixed_verbose_and_other_flags():
    result = _hoist_verbosity_flags(["build", "PKGBUILD", "--cc", "clang", "-vv", "--persist-log"])
    # -vv hoisted, rest preserved in original relative order
    assert result[0] == "-vv"
    assert "build" in result
    assert "--cc" in result
    assert "clang" in result
    assert "--persist-log" in result


# ---------------------------------------------------------------------------
# _patch_makepkg_argv
# ---------------------------------------------------------------------------

def test_patch_makepkg_long_form_dash_value():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "--makepkg", "-sfci"])
    assert "--makepkg=-sfci" in result
    assert "--makepkg" not in result or result.index("--makepkg") == result.index("--makepkg=-sfci")


def test_patch_makepkg_short_form_dash_value():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "-m", "-sfci"])
    assert "--makepkg=-sfci" in result


def test_patch_makepkg_value_not_starting_with_dash():
    # Values that don't start with '-' should be left alone
    result = _patch_makepkg_argv(["build", "PKGBUILD", "-m", "sfci"])
    assert result == ["build", "PKGBUILD", "-m", "sfci"]


def test_patch_makepkg_no_makepkg_flag():
    argv = ["build", "PKGBUILD", "--interactive"]
    result = _patch_makepkg_argv(argv)
    assert result == argv


def test_patch_makepkg_empty():
    assert _patch_makepkg_argv([]) == []


def test_patch_makepkg_m_at_end_no_value():
    # -m at end with no following value — should pass through unchanged
    result = _patch_makepkg_argv(["build", "-m"])
    assert result == ["build", "-m"]


def test_patch_makepkg_double_dash_value():
    result = _patch_makepkg_argv(["-m", "--noconfirm"])
    assert "--makepkg=--noconfirm" in result


def test_patch_makepkg_preserves_other_flags():
    result = _patch_makepkg_argv(["build", "PKGBUILD", "--cc", "clang", "-m", "-sf"])
    assert "--cc" in result
    assert "clang" in result
    assert "--makepkg=-sf" in result


# ---------------------------------------------------------------------------
# expand_makepkg_flags
# ---------------------------------------------------------------------------

def test_expand_combined_short_flags():
    assert expand_makepkg_flags("-sfci") == ["-s", "-f", "-c", "-i"]


def test_expand_single_short_flag():
    assert expand_makepkg_flags("-s") == ["-s"]


def test_expand_long_flag_unchanged():
    assert expand_makepkg_flags("--noconfirm") == ["--noconfirm"]


def test_expand_multiple_tokens():
    result = expand_makepkg_flags("-sf --noconfirm -i")
    assert "-s" in result
    assert "-f" in result
    assert "--noconfirm" in result
    assert "-i" in result


def test_expand_none_returns_empty():
    assert expand_makepkg_flags(None) == []


def test_expand_empty_string_returns_empty():
    assert expand_makepkg_flags("") == []


def test_expand_already_separated():
    result = expand_makepkg_flags("-s -f -c")
    assert result == ["-s", "-f", "-c"]


def test_expand_long_flag_with_value():
    # Long flags with = should pass through intact
    result = expand_makepkg_flags("--key=value")
    assert result == ["--key=value"]


def test_expand_mixed_short_and_long():
    result = expand_makepkg_flags("-sf --noconfirm")
    assert result == ["-s", "-f", "--noconfirm"]


# ---------------------------------------------------------------------------
# _extract_implicit_makepkg_flags
# ---------------------------------------------------------------------------

def test_implicit_basic_passthrough():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sfCci"])
    assert result == ["build", "ventoy", "-m", "-sfCci"]


def test_implicit_multiple_flag_groups():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sf", "-Cci"])
    assert result == ["build", "ventoy", "-m", "-sfCci"]


def test_implicit_no_subcommand():
    argv = ["--verbose", "resolve", "pkg"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_no_flags():
    argv = ["build", "ventoy", "--interactive"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_excluded_flags_not_extracted():
    """Flags in _PASSTHROUGH_EXCLUDE (-h, -V, -p, -m, -D) stay in argv."""
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-h"])
    assert "-m" not in result
    assert "-h" in result


def test_implicit_mixed_excluded_and_valid():
    """A token with any excluded char is left alone (not partially extracted)."""
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "-sh"])
    # -sh contains h (excluded), so the whole token stays.
    assert "-m" not in result
    assert "-sh" in result


def test_implicit_update_subcommand():
    result = _extract_implicit_makepkg_flags(["update", "-sf"])
    assert result == ["update", "-m", "-sf"]


def test_implicit_converge_subcommand():
    result = _extract_implicit_makepkg_flags(["converge", "-f"])
    assert result == ["converge", "-m", "-f"]


def test_implicit_merge_with_explicit_m():
    result = _extract_implicit_makepkg_flags(["build", "pkg", "-m", "--noconfirm", "-sf"])
    assert result == ["build", "pkg", "-m", "--noconfirm -sf", ]


def test_implicit_merge_with_explicit_makepkg_eq():
    result = _extract_implicit_makepkg_flags(["build", "pkg", "--makepkg=--noconfirm", "-sf"])
    assert result == ["build", "pkg", "--makepkg=--noconfirm -sf"]


def test_implicit_preserves_positional_args():
    result = _extract_implicit_makepkg_flags(["build", "ventoy", "cosmic-osd-git", "-sfCci"])
    assert result == ["build", "ventoy", "cosmic-osd-git", "-m", "-sfCci"]


def test_implicit_long_flags_not_extracted():
    """--noconfirm and other long flags should not be extracted."""
    argv = ["build", "pkg", "--interactive"]
    assert _extract_implicit_makepkg_flags(argv) is argv


def test_implicit_preserves_tokens_before_subcommand():
    result = _extract_implicit_makepkg_flags(["-vv", "build", "pkg", "-sf"])
    assert result == ["-vv", "build", "pkg", "-m", "-sf"]
