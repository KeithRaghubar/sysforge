# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for pacman remove_pkgs and reinstall_repo_pkgs helpers."""

from unittest.mock import patch

import pytest

from sysforge.primitives import pacman


@pytest.fixture(autouse=True)
def _force_non_root(monkeypatch):
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 1000)


def test_remove_pkgs_argv():
    with patch("sysforge.primitives.pacman.subprocess.run") as run:
        run.return_value.returncode = 0
        pacman.remove_pkgs(["mesa-sysforge"])
    argv = run.call_args[0][0]
    assert argv[:3] == ["sudo", "pacman", "-R"]
    assert "mesa-sysforge" in argv
    assert "--needed" not in argv


def test_reinstall_repo_pkgs_argv_has_no_needed():
    with patch("sysforge.primitives.pacman.subprocess.run") as run:
        run.return_value.returncode = 0
        pacman.reinstall_repo_pkgs(["mesa"])
    argv = run.call_args[0][0]
    assert argv[:3] == ["sudo", "pacman", "-S"]
    assert "--needed" not in argv
    assert "mesa" in argv


def test_remove_pkgs_empty_is_noop():
    with patch("sysforge.primitives.pacman.subprocess.run") as run:
        pacman.remove_pkgs([])
    run.assert_not_called()


def test_reinstall_repo_pkgs_replace_auto_confirms_conflict():
    """3.3.0-B6: `--noconfirm` answers pacman's conflict prompt N, so a
    conflict-mode revert (`pacman -S mesa` over `mesa-sysforge`) aborted.
    `replace=True` passes `--ask=4` (ALPM_QUESTION_CONFLICT_PKG), as the `-U`
    install path already does for an intended drop-in (B6 of that path)."""
    with patch("sysforge.primitives.pacman.subprocess.run") as run:
        run.return_value.returncode = 0
        pacman.reinstall_repo_pkgs(["mesa"], replace=True)
    argv = run.call_args[0][0]
    assert "--ask=4" in argv
    assert argv.index("--ask=4") < argv.index("--")


def test_reinstall_repo_pkgs_plain_keeps_safe_conflict_default():
    with patch("sysforge.primitives.pacman.subprocess.run") as run:
        run.return_value.returncode = 0
        pacman.reinstall_repo_pkgs(["mesa"])
    assert "--ask=4" not in run.call_args[0][0]
