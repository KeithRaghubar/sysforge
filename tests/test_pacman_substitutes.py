# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""3.2.0-F16: the drop-in rule and its local-DB reader."""
from types import SimpleNamespace

from sysforge.primitives import pacman


def test_supersedes_names_replaces_or_conflicts_and_provides():
    assert pacman.supersedes_names(["mesa"], [], []) == {"mesa"}
    assert pacman.supersedes_names([], ["wayland"], ["wayland=1.26"]) == {"wayland"}


def test_supersedes_names_ignores_provides_without_conflicts():
    """A virtual provide (``xdg-desktop-portal-impl``) is not a drop-in."""
    assert pacman.supersedes_names([], ["a"], ["a", "xdg-desktop-portal-impl"]) == {"a"}
    assert pacman.supersedes_names([], [], ["libgl"]) == set()


_QI = """\
Name            : cosmic-comp-git
Version         : 1.9.0.r5-1
Provides        : cosmic-comp
Conflicts With  : cosmic-comp
Replaces        : None

Name            : vulkan-intel-sysforge
Version         : 1:26.2.3-1
Provides        : vulkan-intel=1:26.2.3  vulkan-driver
Conflicts With  : vulkan-intel
Replaces        : vulkan-intel

Name            : cosmic-comp-git-self
Version         : 1-1
Provides        : cosmic-comp-git-self
Conflicts With  : cosmic-comp-git-self
Replaces        : None
"""


def test_get_installed_substitutes_qi_fallback(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    monkeypatch.setattr(pacman.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_QI, stderr=""))
    assert pacman.get_installed_substitutes() == {
        "cosmic-comp": [("cosmic-comp-git", "1.9.0.r5-1")],
        "vulkan-intel": [("vulkan-intel-sysforge", "1:26.2.3-1")],
    }


def test_get_installed_substitutes_empty_on_read_failure(monkeypatch):
    monkeypatch.setenv("SYSFORGE_PACMAN_NO_PYALPM", "1")
    monkeypatch.setattr(pacman.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="x"))
    assert pacman.get_installed_substitutes() == {}
