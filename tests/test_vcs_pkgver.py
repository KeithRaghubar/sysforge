"""
test_vcs_pkgver.py — unit tests for sysforge.primitives.vcs_pkgver

All makepkg invocations are mocked; no real subprocess is executed.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.vcs_pkgver import evaluate_vcs_pkgver


def _make_pkgbuild(tmp_path: Path) -> Path:
    pkg_dir = tmp_path / "foo-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=foo-git\n")
    return pkg_dir


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_evaluate_returns_resolved_pkgver(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    listing_stdout = "/var/cache/pkgdest/foo-git-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"

    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "0.1.r5.gabcdef-1"

    # Ensure both makepkg passes ran in the right cwd
    cwds = [call.kwargs.get("cwd") for call in run.call_args_list]
    assert all(cwd == pkg_dir for cwd in cwds)


def test_evaluate_returns_resolved_pkgver_with_epoch(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    listing_stdout = "foo-git-2:0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"

    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "2:0.1.r5.gabcdef-1"


def test_evaluate_returns_first_parseable_line_for_split_pkgs(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    # Split package: two pkgnames, same pkgver/pkgrel — either line is fine.
    listing_stdout = (
        "foo-git-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"
        "foo-git-docs-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "0.1.r5.gabcdef-1"


def test_evaluate_returns_none_on_makepkg_resolve_failure(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(1, stderr="==> ERROR: pkgver() failed")]
        assert evaluate_vcs_pkgver(pkg_dir) is None
        # --packagelist must not be invoked after resolve fails
        assert run.call_count == 1


def test_evaluate_returns_none_on_packagelist_failure(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(2, stderr="boom")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_resolve_timeout(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd="makepkg", timeout=300)
        assert evaluate_vcs_pkgver(pkg_dir, timeout=300) is None


def test_evaluate_returns_none_on_packagelist_timeout(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), subprocess.TimeoutExpired(cmd="makepkg", timeout=30)]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_unparseable_packagelist(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout="not-a-pkg-filename\nstill-garbage\n")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_empty_packagelist(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout="")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_when_pkgbuild_missing(tmp_path):
    pkg_dir = tmp_path / "no-pkgbuild"
    pkg_dir.mkdir()
    # No subprocess call should be attempted.
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert evaluate_vcs_pkgver(pkg_dir) is None
        run.assert_not_called()


def test_evaluate_returns_none_when_makepkg_missing(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = FileNotFoundError("makepkg")
        assert evaluate_vcs_pkgver(pkg_dir) is None
