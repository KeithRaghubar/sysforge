# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the pacman primitives backing `search` / `uninstall`."""
import subprocess
from types import SimpleNamespace

from sysforge.primitives import pacman


def test_uninstall_pkgs_builds_Rnsu_interactive(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0))
    pacman.uninstall_pkgs(["mesa-sysforge"])
    (argv,), kwargs = calls[0]
    assert argv == ["sudo", "pacman", "-Rnsu", "--", "mesa-sysforge"]
    assert kwargs.get("check") is True
    assert "--noconfirm" not in argv  # interactive: pacman prompts


def test_uninstall_pkgs_forwards_extra_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    pacman.uninstall_pkgs(["foo"], extra_flags=["-c"])
    assert calls[0][0] == ["sudo", "pacman", "-Rnsu", "-c", "--", "foo"]


def test_uninstall_pkgs_empty_is_noop(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    pacman.uninstall_pkgs([])  # must not call subprocess.run


def test_search_repo_returns_stdout_on_match(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="extra/nano 7.2-1\n"))
    assert pacman.search_repo("nano") == "extra/nano 7.2-1\n"


def test_search_local_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert pacman.search_local("no-such-pkg") == ""
