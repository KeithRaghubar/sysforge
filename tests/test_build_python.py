# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Unit tests for the configurable build python (Fix for the pyenv-shim leak that
broke `cable`'s `python -m build`).

Covers:
  resolve_build_python:
    - unset cfg / "system" → the system python (/usr/bin/python)
    - bare version "3.12" → /usr/bin/python3.12
    - absolute path used verbatim
    - unusable explicit setting warns and falls back to the system python
    - None when no system python is available
  invoke_makepkg:
    - the resolved python's directory is prepended to PATH so a bare `python`
      in a PKGBUILD resolves to it ahead of any pyenv/asdf/conda shim
"""
import os
import stat
from pathlib import Path

from sysforge.primitives import makepkg_env, makepkg_invoke
from sysforge.primitives.makepkg_env import resolve_build_python


def _make_exec(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------------------
# resolve_build_python
# ---------------------------------------------------------------------------

def test_default_resolves_system_python(monkeypatch, tmp_path):
    syspy = _make_exec(tmp_path / "python")
    monkeypatch.setattr(makepkg_env, "_SYSTEM_PYTHON", syspy)
    assert resolve_build_python({}) == syspy


def test_system_keyword_resolves_system_python(monkeypatch, tmp_path):
    syspy = _make_exec(tmp_path / "python")
    monkeypatch.setattr(makepkg_env, "_SYSTEM_PYTHON", syspy)
    assert resolve_build_python({"build": {"python": "system"}}) == syspy


def test_version_form_constructs_usr_bin_path(monkeypatch):
    # Hermetic: assert the version→/usr/bin/pythonX.Y construction without
    # depending on which minor version the host actually ships.
    monkeypatch.setattr(
        makepkg_env, "_is_exec", lambda p: str(p) == "/usr/bin/python3.12"
    )
    assert resolve_build_python({"build": {"python": "3.12"}}) == Path(
        "/usr/bin/python3.12"
    )


def test_absolute_path_used_verbatim(tmp_path):
    custom = _make_exec(tmp_path / "opt" / "mypython")
    assert resolve_build_python({"build": {"python": str(custom)}}) == custom


def test_unusable_setting_falls_back_to_system(monkeypatch, tmp_path):
    syspy = _make_exec(tmp_path / "python")
    monkeypatch.setattr(makepkg_env, "_SYSTEM_PYTHON", syspy)
    result = resolve_build_python({"build": {"python": str(tmp_path / "nope")}})
    assert result == syspy


def test_returns_none_when_no_system_python(monkeypatch):
    monkeypatch.setattr(makepkg_env, "_is_exec", lambda p: False)
    assert resolve_build_python({}) is None


# ---------------------------------------------------------------------------
# invoke_makepkg PATH prepend
# ---------------------------------------------------------------------------

def test_invoke_makepkg_prepends_build_python_dir_to_path(monkeypatch, tmp_path):
    pkgbuild = tmp_path / "src" / "PKGBUILD"
    pkgbuild.parent.mkdir(parents=True)
    pkgbuild.write_text("pkgname=foo\n")
    conf = tmp_path / "makepkg.conf"
    conf.write_text("")
    fake_py = _make_exec(tmp_path / "pybin" / "python")

    monkeypatch.setattr(
        makepkg_invoke, "resolve_build_python", lambda *a, **k: fake_py
    )

    captured: dict = {}

    class _FakeProc:
        def wait(self):
            return 0

    def _fake_popen(cmd, cwd=None, env=None, **kw):
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(makepkg_invoke.subprocess, "Popen", _fake_popen)

    # interactive=True takes the Popen path (no pty), which is the simplest
    # seam to observe the env the child would inherit.
    makepkg_invoke.invoke_makepkg(
        pkgbuild, conf, {"makepkg_flags": []}, interactive=True
    )

    path_entries = captured["env"]["PATH"].split(os.pathsep)
    assert path_entries[0] == str(fake_py.parent)
