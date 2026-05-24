"""
test_update_pager_env.py — $PAGER scrub at the top of cmd_update.

cmd_update calls _suppress_pagers_in_env(interactive) so no subprocess
(pacman post-install hooks, makepkg subshells, git invocations in
PKGBUILDs, systemd tools called by hooks, etc.) inherits a $PAGER that
would put the terminal into alt-screen mode mid-update. The user-facing
symptom of a regression is a less(1) UI appearing during update and
broken shell scrollback after CTRL+C.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update import _suppress_pagers_in_env


_KEYS = ("PAGER", "GIT_PAGER", "SYSTEMD_PAGER", "LESS")


def _clear_pager_env(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


def test_default_run_sets_no_pager_defaults(monkeypatch):
    """Bare ``sysforge update`` (no --interactive) wipes out any inherited
    $PAGER so no nested subprocess opens less(1) on the user's shell."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("GIT_PAGER", "less")
    monkeypatch.setenv("SYSTEMD_PAGER", "less")

    _suppress_pagers_in_env(interactive=False)

    # PAGER=less was the user's outer shell value; the scrub does not
    # override an explicit value (setdefault semantics) — only fills in
    # missing keys. So the assertion is on the keys we cleared.
    # Re-run with a clean baseline:
    _clear_pager_env(monkeypatch)
    _suppress_pagers_in_env(interactive=False)
    assert os.environ["PAGER"] == "cat"
    assert os.environ["GIT_PAGER"] == "cat"
    assert os.environ["SYSTEMD_PAGER"] == "cat"
    assert os.environ["LESS"] == "-RFX"


def test_interactive_run_leaves_env_untouched(monkeypatch):
    """``sysforge update --interactive`` is explicit consent to pause and
    let downstream tools page output, so the scrub is skipped."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "less")

    _suppress_pagers_in_env(interactive=True)

    assert os.environ["PAGER"] == "less"
    assert "GIT_PAGER" not in os.environ
    assert "SYSTEMD_PAGER" not in os.environ
    assert "LESS" not in os.environ


def test_user_set_pager_is_preserved(monkeypatch):
    """``PAGER=most sysforge update`` is an explicit ask from the operator;
    the scrub must use setdefault so an inherited value wins over the
    cat-by-default."""
    _clear_pager_env(monkeypatch)
    monkeypatch.setenv("PAGER", "most")

    _suppress_pagers_in_env(interactive=False)

    assert os.environ["PAGER"] == "most"
    # The unspecified keys still get filled in — only PAGER was the user's
    # explicit choice.
    assert os.environ["GIT_PAGER"] == "cat"
    assert os.environ["SYSTEMD_PAGER"] == "cat"
    assert os.environ["LESS"] == "-RFX"
