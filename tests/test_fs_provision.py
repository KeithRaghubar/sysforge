# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/fs_provision.py — the one home for sysforge runtime
directory provisioning (root:sysforge 2775; usermod + per-run repair)."""

from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives import fs_provision
from sysforge.primitives.fs_provision import (
    SYSFORGE_DIR_MODE,
    SYSFORGE_GROUP,
    FsProvisionError,
    build_user,
    empty_dir_contents,
    ensure_writable_dir,
)


# ---------------------------------------------------------------------------
# build_user
# ---------------------------------------------------------------------------


def test_build_user_prefers_sudo_user(monkeypatch):
    monkeypatch.setenv("SUDO_USER", "invoker")
    monkeypatch.setenv("USER", "root")
    assert build_user() == "invoker"


def test_build_user_falls_back_to_user(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("USER", "keith")
    assert build_user() == "keith"


# ---------------------------------------------------------------------------
# ensure_writable_dir — fast path (no sudo)
# ---------------------------------------------------------------------------


def test_ensure_writable_dir_user_path_no_sudo(tmp_path):
    """A user-writable target is created directly and never shells out."""
    target = tmp_path / "cache" / "llvm-pgo"
    with patch("subprocess.run") as mock_run:
        out = ensure_writable_dir(target)
    assert out == target
    assert target.is_dir()
    mock_run.assert_not_called()


def test_ensure_writable_dir_dry_run_noop(tmp_path):
    target = tmp_path / "cache" / "llvm-pgo"
    with patch("subprocess.run") as mock_run:
        ensure_writable_dir(target, dry_run=True)
    assert not target.exists()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_writable_dir — slow path (sudo provisioning)
# ---------------------------------------------------------------------------


def test_ensure_writable_dir_root_owned_provisions_group(tmp_path, monkeypatch):
    """A root-owned ancestor (mkdir raises EACCES) provisions the group,
    adds the user, and installs/repairs the dir as root:sysforge 2775."""
    target = tmp_path / "var" / "cache" / "sysforge" / "llvm-pgo"
    monkeypatch.setenv("SUDO_USER", "buildbot")

    def raise_eacces(*a, **k):
        raise PermissionError(13, "Permission denied")

    with patch("pathlib.Path.mkdir", side_effect=raise_eacces), \
         patch.object(fs_provision, "_group_exists", return_value=False), \
         patch.object(fs_provision, "_user_in_group", return_value=False), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        ensure_writable_dir(target)

    calls = [c.args[0] for c in mock_run.call_args_list]
    mode = f"{SYSFORGE_DIR_MODE:o}"
    assert ["sudo", "groupadd", "-f", SYSFORGE_GROUP] in calls
    assert ["sudo", "usermod", "-aG", SYSFORGE_GROUP, "buildbot"] in calls
    assert ["sudo", "install", "-d", "-m", mode, "-g", SYSFORGE_GROUP, str(target)] in calls
    assert ["sudo", "chgrp", SYSFORGE_GROUP, str(target)] in calls
    assert ["sudo", "chmod", mode, str(target)] in calls


def test_ensure_writable_dir_existing_group_skips_usermod(tmp_path, monkeypatch):
    """When the group exists and the user is already a member, neither groupadd
    nor usermod runs — only the install/chgrp/chmod repair."""
    target = tmp_path / "var" / "lib" / "sysforge"
    monkeypatch.setenv("SUDO_USER", "keith")

    def raise_eacces(*a, **k):
        raise PermissionError(13, "Permission denied")

    with patch("pathlib.Path.mkdir", side_effect=raise_eacces), \
         patch.object(fs_provision, "_group_exists", return_value=True), \
         patch.object(fs_provision, "_user_in_group", return_value=True), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        ensure_writable_dir(target)

    flat = [c.args[0] for c in mock_run.call_args_list]
    assert not any(c[:2] == ["sudo", "groupadd"] for c in flat)
    assert not any(c[:2] == ["sudo", "usermod"] for c in flat)
    assert ["sudo", "chgrp", SYSFORGE_GROUP, str(target)] in flat


def test_ensure_writable_dir_no_sudo_allowed_raises(tmp_path):
    """allow_sudo=False over a non-writable target raises FsProvisionError so
    callers (e.g. state-dir resolution) can fall back to an XDG location."""
    target = tmp_path / "var" / "lib" / "sysforge"
    with patch("pathlib.Path.mkdir", side_effect=PermissionError(13, "nope")):
        with pytest.raises(FsProvisionError):
            ensure_writable_dir(target, allow_sudo=False)


def test_ensure_writable_dir_sudo_missing_raises(tmp_path):
    """sudo not installed surfaces as FsProvisionError, not a raw FileNotFoundError."""
    target = tmp_path / "var" / "cache" / "sysforge"
    with patch("pathlib.Path.mkdir", side_effect=PermissionError(13, "nope")), \
         patch.object(fs_provision, "_group_exists", return_value=True), \
         patch.object(fs_provision, "_user_in_group", return_value=True), \
         patch("subprocess.run", side_effect=FileNotFoundError("sudo")):
        with pytest.raises(FsProvisionError):
            ensure_writable_dir(target)


# ---------------------------------------------------------------------------
# empty_dir_contents
# ---------------------------------------------------------------------------


def test_empty_dir_contents_keeps_node_clears_tree(tmp_path):
    """Contents (files + nested dirs) are removed; the node itself survives —
    this is what avoids the rmtree-EACCES on a root-owned parent."""
    pgo_store = tmp_path / "llvm-pgo"
    pgo_store.mkdir()
    (pgo_store / "clang.profdata").write_text("x")
    nested = pgo_store / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "run.profraw").write_text("y")

    empty_dir_contents(pgo_store)

    assert pgo_store.is_dir()
    assert list(pgo_store.iterdir()) == []


def test_empty_dir_contents_dry_run_and_missing(tmp_path):
    pgo_store = tmp_path / "llvm-pgo"
    pgo_store.mkdir()
    (pgo_store / "f").write_text("x")
    empty_dir_contents(pgo_store, dry_run=True)
    assert (pgo_store / "f").exists()
    # Missing path is a silent no-op.
    empty_dir_contents(tmp_path / "does-not-exist")


def test_empty_dir_contents_removes_symlink_without_following(tmp_path):
    """A symlinked child is unlinked, never recursed into."""
    pgo_store = tmp_path / "llvm-pgo"
    pgo_store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (pgo_store / "link").symlink_to(outside)

    empty_dir_contents(pgo_store)

    assert list(pgo_store.iterdir()) == []
    assert (outside / "keep").exists()  # link target untouched
