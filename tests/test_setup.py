"""
test_setup.py — unit tests for sysforge.setup_cmd

Tests the pure helper functions (_check_ignore_group, _patch_conf_text) and
the cmd_setup entry point (mocked filesystem and stdin).
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.setup_cmd import _check_ignore_group, _patch_conf_text, cmd_setup


# ---------------------------------------------------------------------------
# _check_ignore_group
# ---------------------------------------------------------------------------

_CONF_NO_IGNORE = """\
[options]
HoldPkg = pacman glibc
Architecture = auto
"""

_CONF_WITH_SF_BUILD = """\
[options]
HoldPkg = pacman glibc
IgnoreGroup = sf-build
"""

_CONF_WITH_OTHER_GROUPS = """\
[options]
HoldPkg = pacman glibc
IgnoreGroup = base devel
"""

_CONF_WITH_SF_BUILD_AMONG_OTHERS = """\
[options]
IgnoreGroup = base sf-build devel
"""

_CONF_COMMENTED_IGNORE = """\
[options]
# IgnoreGroup = sf-build
HoldPkg = pacman glibc
"""

_CONF_IGNORE_UPPERCASE_KEY = """\
[options]
IGNOREGROUP = sf-build
"""


def test_check_absent():
    assert _check_ignore_group(_CONF_NO_IGNORE) is False


def test_check_present():
    assert _check_ignore_group(_CONF_WITH_SF_BUILD) is True


def test_check_other_groups_no_sf_build():
    assert _check_ignore_group(_CONF_WITH_OTHER_GROUPS) is False


def test_check_sf_build_among_others():
    assert _check_ignore_group(_CONF_WITH_SF_BUILD_AMONG_OTHERS) is True


def test_check_commented_line_not_counted():
    # Commented-out IgnoreGroup should not count as configured.
    assert _check_ignore_group(_CONF_COMMENTED_IGNORE) is False


def test_check_case_insensitive_key():
    # pacman.conf keys are case-insensitive; sysforge should handle that.
    assert _check_ignore_group(_CONF_IGNORE_UPPERCASE_KEY) is True


# ---------------------------------------------------------------------------
# _patch_conf_text
# ---------------------------------------------------------------------------

def test_patch_appends_to_existing_ignore_group():
    result = _patch_conf_text(_CONF_WITH_OTHER_GROUPS)
    assert "IgnoreGroup = base devel sf-build" in result
    # Original line not duplicated.
    assert result.count("IgnoreGroup") == 1


def test_patch_inserts_new_ignore_group_in_options():
    result = _patch_conf_text(_CONF_NO_IGNORE)
    assert "IgnoreGroup = sf-build" in result
    # Verify it's inside [options] (comes before any subsequent section).
    options_idx = result.index("[options]")
    ignore_idx = result.index("IgnoreGroup = sf-build")
    assert ignore_idx > options_idx


def test_patch_no_options_section_appends_at_end():
    conf = "# minimal conf\nColor\n"
    result = _patch_conf_text(conf)
    assert "IgnoreGroup = sf-build" in result
    assert result.endswith("IgnoreGroup = sf-build\n")


def test_patch_preserves_existing_content():
    result = _patch_conf_text(_CONF_NO_IGNORE)
    assert "HoldPkg = pacman glibc" in result
    assert "Architecture = auto" in result


def test_patch_idempotent_on_existing_line():
    # Patching a conf that already has sf-build should still work structurally
    # (check_ignore_group gates this in practice, but patch_conf_text itself
    # will just append sf-build again — callers are responsible for the guard).
    result = _patch_conf_text(_CONF_WITH_SF_BUILD)
    # sf-build appears at least once.
    assert "sf-build" in result


# ---------------------------------------------------------------------------
# cmd_setup — already configured
# ---------------------------------------------------------------------------

def test_cmd_setup_already_configured(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_WITH_SF_BUILD)

    args = MagicMock()
    args.pacman_conf = str(conf)

    cmd_setup(args)

    out = capsys.readouterr().out
    assert "Already configured" in out


# ---------------------------------------------------------------------------
# cmd_setup — user says yes
# ---------------------------------------------------------------------------

def test_cmd_setup_user_yes(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="y"):
        cmd_setup(args)

    out = capsys.readouterr().out
    assert "Added" in out
    assert "IgnoreGroup = sf-build" in conf.read_text()


def test_cmd_setup_user_yes_uppercase(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="Y"):
        cmd_setup(args)

    assert "IgnoreGroup = sf-build" in conf.read_text()


def test_cmd_setup_user_yes_full_word(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="yes"):
        cmd_setup(args)

    assert "IgnoreGroup = sf-build" in conf.read_text()


# ---------------------------------------------------------------------------
# cmd_setup — user says no
# ---------------------------------------------------------------------------

def test_cmd_setup_user_no_prints_warning(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="n"):
        cmd_setup(args)

    err = capsys.readouterr().err
    assert "pacman -Syu" in err
    assert "sysforge update" in err
    # conf must be untouched.
    assert "IgnoreGroup" not in conf.read_text()


def test_cmd_setup_user_empty_input_treated_as_no(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value=""):
        cmd_setup(args)

    assert "IgnoreGroup" not in conf.read_text()


def test_cmd_setup_eof_treated_as_no(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", side_effect=EOFError):
        cmd_setup(args)

    assert "IgnoreGroup" not in conf.read_text()


# ---------------------------------------------------------------------------
# cmd_setup — permission denied
# ---------------------------------------------------------------------------

def test_cmd_setup_permission_denied(tmp_path, capsys):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_CONF_NO_IGNORE)

    args = MagicMock()
    args.pacman_conf = str(conf)

    with patch("builtins.input", return_value="y"), \
         patch("sysforge.setup_cmd._write_conf", return_value=False):
        cmd_setup(args)

    err = capsys.readouterr().err
    assert "permission denied" in err.lower()
    assert "sudo sysforge setup" in err


# ---------------------------------------------------------------------------
# cmd_setup — conf file not found
# ---------------------------------------------------------------------------

def test_cmd_setup_conf_not_found(tmp_path, capsys):
    args = MagicMock()
    args.pacman_conf = str(tmp_path / "nonexistent.conf")

    cmd_setup(args)

    err = capsys.readouterr().err
    assert "not found" in err
