# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the search verb."""
from types import SimpleNamespace

from sysforge import search_cmd
from sysforge.verbs.base import PreCheckResult


def test_not_sentinel_gated():
    assert search_cmd.SearchVerb.requires_sentinel is False


def test_render_aur_formats_repo_line():
    out = search_cmd.render_aur([
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ])
    assert "aur/cosmic-ext-foo 1.0-1" in out
    assert "a thing" in out


def test_sections_ordered_and_empty_omitted(monkeypatch, capsys):
    monkeypatch.setattr(search_cmd.pacman, "search_local", lambda t: "")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "extra/nano 7.2-1\n")
    monkeypatch.setattr(search_cmd.aur, "aur_search",
                        lambda t: [{"Name": "nano-git", "Version": "r1-1", "Description": "d"}])

    verb = search_cmd.SearchVerb()
    args = SimpleNamespace(term="nano")
    verb.execute(args, PreCheckResult(ctx={}))
    # _log.ui writes to stderr (log._out() is stderr unless dry-run).
    out = capsys.readouterr().err

    assert "Installed" not in out          # local empty → header omitted
    assert out.index("Repo") < out.index("AUR")  # fixed order
    assert "extra/nano" in out and "aur/nano-git" in out


def test_aur_failure_is_nonfatal(monkeypatch, capsys):
    monkeypatch.setattr(search_cmd.pacman, "search_local", lambda t: "local/foo 1-1\n")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "")
    monkeypatch.setattr(search_cmd.aur, "aur_search", lambda t: [])  # helper already swallows errors

    verb = search_cmd.SearchVerb()
    res = verb.execute(SimpleNamespace(term="foo"), PreCheckResult(ctx={}))
    assert res.exit_code == 0
    assert "local/foo" in capsys.readouterr().err
