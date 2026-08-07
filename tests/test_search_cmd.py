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


def test_render_aur_colourizes_prefix_name_and_version(monkeypatch):
    """F2: with colour forced, the AUR line carries ANSI so it visually
    matches the pacman-rendered sections instead of reading as plain text."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    out = search_cmd.render_aur([
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ])
    assert "\033[" in out                       # ANSI present
    assert "\033[1mcosmic-ext-foo\033[0m" in out  # bold name
    assert "\033[32m1.0-1\033[0m" in out          # green version


def test_render_aur_plain_under_no_color(monkeypatch):
    """F2: NO_COLOR degrades cleanly to the original plain rendering."""
    monkeypatch.setenv("NO_COLOR", "1")
    out = search_cmd.render_aur([
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ])
    assert "\033[" not in out
    assert "aur/cosmic-ext-foo 1.0-1" in out


def test_blank_line_separates_consecutive_sections(monkeypatch, capsys):
    """F2: a blank line delimits the three sources so they read distinctly."""
    monkeypatch.setattr(search_cmd.pacman, "search_local", lambda t: "")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "extra/nano 7.2-1\n")
    monkeypatch.setattr(search_cmd.aur, "aur_search",
                        lambda t: [{"Name": "nano-git", "Version": "r1-1", "Description": "d"}])
    verb = search_cmd.SearchVerb()
    verb.execute(SimpleNamespace(term="nano"), PreCheckResult(ctx={}))
    out = capsys.readouterr().err
    assert "\n\n== AUR ==" in out


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
    # helper already swallows errors
    monkeypatch.setattr(search_cmd.aur, "aur_search", lambda t: [])

    verb = search_cmd.SearchVerb()
    res = verb.execute(SimpleNamespace(term="foo"), PreCheckResult(ctx={}))
    assert res.exit_code == 0
    assert "local/foo" in capsys.readouterr().err
