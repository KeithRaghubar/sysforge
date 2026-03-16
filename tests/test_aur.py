"""
test_aur.py — unit tests for sysforge.primitives.aur

Covers:
    aur_info    — successful batch query, empty result, network error, bad JSON
    aur_clone   — successful clone, git failure
"""
import json
import subprocess
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.aur import aur_clone, aur_info, is_repo_package, pkgctl_checkout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUR_RESPONSE_FOUND = {
    "version": 5,
    "type": "multiinfo",
    "resultcount": 2,
    "results": [
        {"ID": 1, "Name": "mesa-git",       "Version": "24.0-1"},
        {"ID": 2, "Name": "cosmic-comp-git","Version": "0.1-1"},
    ],
}

AUR_RESPONSE_EMPTY = {
    "version": 5,
    "type": "multiinfo",
    "resultcount": 0,
    "results": [],
}


def _mock_urlopen(response_dict):
    """Return a context-manager mock that yields a readable HTTP response."""
    body = json.dumps(response_dict).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# aur_info
# ---------------------------------------------------------------------------

def test_aur_info_returns_found_packages():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_FOUND)):
        result = aur_info(["mesa-git", "cosmic-comp-git", "nonexistent"])
    assert "mesa-git" in result
    assert "cosmic-comp-git" in result
    assert "nonexistent" not in result


def test_aur_info_empty_names_returns_empty():
    result = aur_info([])
    assert result == {}


def test_aur_info_empty_results():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_EMPTY)):
        result = aur_info(["totally-fake-pkg"])
    assert result == {}


def test_aur_info_network_error_returns_empty():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        result = aur_info(["mesa-git"])
    assert result == {}


def test_aur_info_bad_json_returns_empty():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json {"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = aur_info(["mesa-git"])
    assert result == {}


def test_aur_info_result_contains_version():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_FOUND)):
        result = aur_info(["mesa-git"])
    assert result["mesa-git"]["Version"] == "24.0-1"


def test_aur_info_url_contains_arg_brackets():
    """Verify the query string uses literal arg[] keys (not percent-encoded)."""
    captured_url = []

    def fake_urlopen(url, timeout=None):
        captured_url.append(url)
        return _mock_urlopen(AUR_RESPONSE_EMPTY)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        aur_info(["htop"])

    assert "arg[]=" in captured_url[0]


# ---------------------------------------------------------------------------
# aur_clone
# ---------------------------------------------------------------------------

def test_aur_clone_success(tmp_path):
    dest = tmp_path / "mesa-git"

    def fake_run(cmd, **kwargs):
        # Simulate git creating the directory with a PKGBUILD
        dest.mkdir()
        (dest / "PKGBUILD").write_text("pkgname=mesa-git\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("mesa-git", dest)

    assert (dest / "PKGBUILD").exists()


def test_aur_clone_command_uses_aur_url():
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("mesa-git", Path("/tmp/mesa-git"))

    assert captured[0][0] == "git"
    assert "aur.archlinux.org" in captured[0][2]
    assert "mesa-git" in captured[0][2]


def test_aur_clone_failure_raises():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="repository not found")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="AUR clone failed"):
            aur_clone("nonexistent-pkg-xyz", Path("/tmp/nonexistent"))


# ---------------------------------------------------------------------------
# is_repo_package
# ---------------------------------------------------------------------------

def test_is_repo_package_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
        assert is_repo_package("htop") is True


def test_is_repo_package_not_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
        assert is_repo_package("totally-fake-aur-only-pkg") is False


def test_is_repo_package_calls_pacman_si():
    captured = []
    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_run):
        is_repo_package("htop")

    assert captured[0][:3] == ["pacman", "-Si", "htop"]


# ---------------------------------------------------------------------------
# pkgctl_checkout
# ---------------------------------------------------------------------------

def test_pkgctl_checkout_success(tmp_path):
    pkg_dir = tmp_path / "htop"

    def fake_run(cmd, **kwargs):
        # Simulate pkgctl creating the directory with a PKGBUILD
        pkg_dir.mkdir()
        (pkg_dir / "PKGBUILD").write_text("pkgname=htop\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        pkgctl_checkout("htop", pkg_dir)

    assert (pkg_dir / "PKGBUILD").exists()


def test_pkgctl_checkout_runs_in_parent(tmp_path):
    captured = []
    pkg_dir = tmp_path / "htop"

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        pkgctl_checkout("htop", pkg_dir)

    cmd, kwargs = captured[0]
    assert cmd[:3] == ["pkgctl", "repo", "clone"]
    assert "--protocol=https" in cmd
    assert "htop" in cmd
    assert kwargs.get("cwd") == str(tmp_path)


def test_pkgctl_checkout_failure_raises(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: package not found")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="pkgctl checkout failed"):
            pkgctl_checkout("nonexistent", tmp_path / "nonexistent")
