# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for makepkg_wrapper.install_built_packages — the split build/install
path that installs .pkg.tar* artifacts via ``pacman -U`` through the F10
privilege seam (2.3.0-F10)."""
from pathlib import Path

import pytest

from sysforge.primitives import makepkg_wrapper


@pytest.fixture(autouse=True)
def _force_non_root(monkeypatch):
    """Pin euid non-root so the seam's ``sudo`` prefix is deterministic
    regardless of the test runner's uid."""
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 1000)


@pytest.fixture
def _capture_run(monkeypatch):
    """Replace the module's subprocess.run with a capturing stub.

    Returns a dict the test reads: ``cmd`` (the argv passed) and a settable
    ``rc`` (the returncode the stub reports, default 0)."""
    box = {"cmd": None, "rc": 0, "calls": 0}

    class _Result:
        def __init__(self, rc):
            self.returncode = rc

    def _fake_run(cmd, *a, **kw):
        box["cmd"] = cmd
        box["calls"] += 1
        return _Result(box["rc"])

    monkeypatch.setattr(makepkg_wrapper.subprocess, "run", _fake_run)
    return box


def _fake_artifacts(monkeypatch, pkgs):
    monkeypatch.setattr(
        makepkg_wrapper, "_artifacts_for_pkgbuild", lambda _d: list(pkgs)
    )


def test_install_built_packages_escalates_via_seam(monkeypatch, _capture_run, tmp_path):
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    result = makepkg_wrapper.install_built_packages(tmp_path)

    assert result == [pkg]
    # non-root → sudo-prefixed; noconfirm default True → --noconfirm present
    assert _capture_run["cmd"] == [
        "sudo", "pacman", "-U", "--noconfirm", str(pkg),
    ]


def test_install_built_packages_no_noconfirm(monkeypatch, _capture_run, tmp_path):
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path, noconfirm=False)

    assert _capture_run["cmd"] == ["sudo", "pacman", "-U", str(pkg)]


def test_install_built_packages_bare_when_root(monkeypatch, _capture_run, tmp_path):
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 0)
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path)

    # already root → no sudo prefix
    assert _capture_run["cmd"] == ["pacman", "-U", "--noconfirm", str(pkg)]


def test_install_built_packages_no_artifacts_raises(monkeypatch, _capture_run, tmp_path):
    _fake_artifacts(monkeypatch, [])

    with pytest.raises(RuntimeError, match="nothing to install"):
        makepkg_wrapper.install_built_packages(tmp_path)

    # nothing was executed
    assert _capture_run["calls"] == 0


def test_install_built_packages_pacman_failure_raises(monkeypatch, _capture_run, tmp_path):
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="pacman -U failed"):
        makepkg_wrapper.install_built_packages(tmp_path)


def test_install_built_packages_drops_manifest_on_success(
    monkeypatch, _capture_run, tmp_path
):
    # a stale build-time manifest must be removed after a successful install so
    # a later unrelated build in the same dir can't read it (B9).
    manifest = tmp_path / makepkg_wrapper._BUILT_MANIFEST_NAME
    manifest.write_text("foo-1-1-x86_64.pkg.tar.zst\n", encoding="utf-8")
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    makepkg_wrapper.install_built_packages(tmp_path)

    assert not manifest.exists()


def test_install_built_packages_failure_names_artifacts(
        monkeypatch, _capture_run, tmp_path):
    """B7: the failure names what it tried to install — the exit code alone
    is undiagnosable after the fact."""
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="foo-1-1-x86_64"):
        makepkg_wrapper.install_built_packages(tmp_path)


def test_install_built_packages_interactive_failure_notes_tty(
        monkeypatch, _capture_run, tmp_path):
    """B7: interactive installs (noconfirm=False) inherit stdio, so pacman's
    output was never captured — the error must say where it went and that a
    declined prompt also exits 1."""
    _capture_run["rc"] = 1
    pkg = Path("/pkgdest/foo-1-1-x86_64.pkg.tar.zst")
    _fake_artifacts(monkeypatch, [pkg])

    with pytest.raises(RuntimeError, match="terminal"):
        makepkg_wrapper.install_built_packages(tmp_path, noconfirm=False)
