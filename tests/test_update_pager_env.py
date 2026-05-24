"""
test_update_pager_env.py — $PAGER scrub at the top of cmd_update and in
the per-build makepkg env.

cmd_update calls _suppress_pagers_in_env(interactive) so no subprocess
(pacman post-install hooks, makepkg subshells, git invocations in
PKGBUILDs, meson configure, systemd tools called by hooks, etc.)
inherits a $PAGER that would put the terminal into alt-screen mode
mid-update. The user-facing symptom of a regression is a less(1) UI
appearing during update and broken shell scrollback after CTRL+C.

The scrub overrides unconditionally (not setdefault) — an exported
PAGER=less from .zshrc is a preference for interactive shells, not
consent to be paged inside a batch update. The only opt-in for paging
is `sysforge update --interactive`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update import _suppress_pagers_in_env


_KEYS = ("PAGER", "GIT_PAGER", "SYSTEMD_PAGER", "LESS")


def _clear_pager_env(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


def test_unset_env_gets_no_pager_defaults(monkeypatch):
    """Clean env: the scrub fills in cat for the three pager keys and a
    sensible LESS for any tool that bypasses $PAGER and invokes less directly."""
    _clear_pager_env(monkeypatch)

    _suppress_pagers_in_env(interactive=False)

    assert os.environ["PAGER"] == "cat"
    assert os.environ["GIT_PAGER"] == "cat"
    assert os.environ["SYSTEMD_PAGER"] == "cat"
    assert os.environ["LESS"] == "-RFX"


def test_inherited_pager_less_is_overridden(monkeypatch):
    """Regression for the libinput-git pager-hang bug: a user with
    `export PAGER=less` in .zshrc still gets PAGER=cat inside a
    non-interactive `sysforge update`. setdefault semantics here were
    the original bug — the scrub must override."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("GIT_PAGER", "less")
    monkeypatch.setenv("SYSTEMD_PAGER", "less")
    monkeypatch.setenv("LESS", "-R")

    _suppress_pagers_in_env(interactive=False)

    assert os.environ["PAGER"] == "cat"
    assert os.environ["GIT_PAGER"] == "cat"
    assert os.environ["SYSTEMD_PAGER"] == "cat"
    assert os.environ["LESS"] == "-RFX"


def test_interactive_run_leaves_env_untouched(monkeypatch):
    """``sysforge update --interactive`` is the single documented opt-in
    for paging. With --interactive set, the user's exported PAGER wins."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "less")

    _suppress_pagers_in_env(interactive=True)

    assert os.environ["PAGER"] == "less"
    assert "GIT_PAGER" not in os.environ
    assert "SYSTEMD_PAGER" not in os.environ
    assert "LESS" not in os.environ


def test_unusual_pager_choice_still_overridden(monkeypatch):
    """`PAGER=most sysforge update` (no --interactive) was previously
    preserved by setdefault. New contract: the scrub overrides regardless
    of value; the only way to keep a custom pager active during update is
    `--interactive`. Documents the behavior change from commit dd77c54."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "most")

    _suppress_pagers_in_env(interactive=False)

    assert os.environ["PAGER"] == "cat"
    assert os.environ["GIT_PAGER"] == "cat"
    assert os.environ["SYSTEMD_PAGER"] == "cat"
    assert os.environ["LESS"] == "-RFX"
