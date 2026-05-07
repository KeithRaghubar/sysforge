"""
test_pacman_pyalpm.py — pyalpm read-path wrapper in primitives/pacman.

The wrapper has two cases:
  (1) pyalpm absent — module imports cleanly, every query falls through to
      subprocess. Verified via importlib.reload with sys.modules['pyalpm']
      set to a sentinel that fails on import.
  (2) pyalpm present — query returns libalpm-derived result. Verified by
      stubbing the libalpm handle.

Setting SYSFORGE_PACMAN_NO_PYALPM=1 forces the subprocess path even when
pyalpm is installed; this is the parity-test escape hatch and is asserted
in test_force_subprocess_via_env.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib

from sysforge.primitives import pacman as pacman_mod


def _mock_subproc_run(stdout="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# (1) Module import is robust to pyalpm being absent
# ---------------------------------------------------------------------------

def test_module_imports_without_pyalpm():
    """When pyalpm import fails, the module should still load and expose
    the public API. The reload simulates a fresh interpreter where pyalpm
    is not installed."""
    saved = sys.modules.pop("pyalpm", None)
    try:
        sys.modules["pyalpm"] = None  # type: ignore[assignment]
        reloaded = importlib.reload(pacman_mod)
        assert reloaded._HAS_PYALPM is False
        # Public API still present
        assert callable(reloaded.get_all_installed_packages)
        assert callable(reloaded.get_installed_version)
    finally:
        sys.modules.pop("pyalpm", None)
        if saved is not None:
            sys.modules["pyalpm"] = saved
        importlib.reload(pacman_mod)


# ---------------------------------------------------------------------------
# (2) Subprocess fallback unchanged when pyalpm path is suppressed
# ---------------------------------------------------------------------------

def test_force_subprocess_via_env(monkeypatch):
    """SYSFORGE_PACMAN_NO_PYALPM=1 must force the subprocess path even when
    pyalpm is present. Verifies the env hook used for parity testing."""
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    assert pacman_mod._use_pyalpm() is False


def test_get_all_installed_packages_subprocess_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    output = "htop 3.4.1-1\nneovim 0.9.5-1\n"
    with patch("subprocess.run", return_value=_mock_subproc_run(output)):
        result = pacman_mod.get_all_installed_packages()
    assert result == {"htop": "3.4.1-1", "neovim": "0.9.5-1"}


def test_get_foreign_packages_subprocess_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    output = "yay 12.3.3-1\nneovim-git r1234.gabcdef-1\n"
    with patch("subprocess.run", return_value=_mock_subproc_run(output)):
        result = pacman_mod.get_foreign_packages()
    assert result == {"yay": "12.3.3-1", "neovim-git": "r1234.gabcdef-1"}


def test_get_installed_version_subprocess_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    with patch("subprocess.run", return_value=_mock_subproc_run("htop 3.4.1-1\n")):
        result = pacman_mod.get_installed_version("htop")
    assert result == "3.4.1-1"


def test_get_pacman_sync_version_subprocess_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    output = "Repository      : extra\nName            : htop\nVersion         : 3.4.1-1\n"
    with patch("subprocess.run", return_value=_mock_subproc_run(output)):
        result = pacman_mod.get_pacman_sync_version("htop")
    assert result == "3.4.1-1"


def test_filter_missing_deps_subprocess_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    with patch("subprocess.run",
               return_value=_mock_subproc_run("missing-pkg\n", returncode=127)):
        result = pacman_mod.filter_missing_deps(["missing-pkg", "htop"])
    assert result == ["missing-pkg"]


# ---------------------------------------------------------------------------
# (3) pyalpm fast path returns libalpm-derived data
# ---------------------------------------------------------------------------

def _make_fake_pkg(name, version):
    p = MagicMock()
    p.name = name
    p.version = version
    return p


def _stub_alpm_handle(local_pkgs=(), sync_pkgs_by_db=None):
    """Build a MagicMock that quacks like a pyalpm.Handle."""
    sync_pkgs_by_db = sync_pkgs_by_db or {}
    handle = MagicMock()

    localdb = MagicMock()
    localdb.pkgcache = list(local_pkgs)
    localdb.get_pkg.side_effect = lambda n: next(
        (p for p in local_pkgs if p.name == n), None
    )
    handle.get_localdb.return_value = localdb

    syncdbs = []
    for repo_name, pkgs in sync_pkgs_by_db.items():
        db = MagicMock()
        db.name = repo_name
        db.pkgcache = pkgs
        db.get_pkg.side_effect = lambda n, _pkgs=pkgs: next(
            (p for p in _pkgs if p.name == n), None
        )
        syncdbs.append(db)
    handle.get_syncdbs.return_value = syncdbs
    return handle


def test_get_all_installed_packages_pyalpm_path(monkeypatch):
    monkeypatch.delenv("SYSFORGE_PACMAN_NO_PYALPM", raising=False)
    pkgs = [_make_fake_pkg("htop", "3.4.1-1"), _make_fake_pkg("neovim", "0.9.5-1")]
    handle = _stub_alpm_handle(local_pkgs=pkgs)
    with patch.object(pacman_mod, "_HAS_PYALPM", True), \
         patch.object(pacman_mod, "_get_alpm_handle", return_value=handle):
        result = pacman_mod.get_all_installed_packages()
    assert result == {"htop": "3.4.1-1", "neovim": "0.9.5-1"}


def test_get_foreign_packages_pyalpm_path(monkeypatch):
    """Foreign = installed minus any name found in any sync DB."""
    monkeypatch.delenv("SYSFORGE_PACMAN_NO_PYALPM", raising=False)
    local = [
        _make_fake_pkg("htop", "3.4.1-1"),
        _make_fake_pkg("neovim-git", "r1234.gabc-1"),
    ]
    sync = {"extra": [_make_fake_pkg("htop", "3.4.1-1")]}
    handle = _stub_alpm_handle(local_pkgs=local, sync_pkgs_by_db=sync)
    with patch.object(pacman_mod, "_HAS_PYALPM", True), \
         patch.object(pacman_mod, "_get_alpm_handle", return_value=handle):
        result = pacman_mod.get_foreign_packages()
    assert result == {"neovim-git": "r1234.gabc-1"}
