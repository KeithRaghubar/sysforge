# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the search verb."""
from types import SimpleNamespace

import pytest

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


# ---------------------------------------------------------------------------
# 3.2.0-F16: in-line installed markers (AUR) + -sysforge variant markers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_installed(monkeypatch):
    """Keep every test hermetic: no live local-DB read unless a test opts in."""
    monkeypatch.setattr(search_cmd.pacman, "get_installed_facts", lambda: {})
    monkeypatch.setattr(search_cmd.pacman, "get_installed_substitutes", lambda: {})


def _aur(name, ver="1.0-1"):
    return {"Name": name, "Version": ver, "Description": "d"}


def test_installed_markers_maps_stock_and_variant():
    markers = search_cmd.installed_markers({
        "nano": ("7.2-1", 1),
        "mesa-sysforge": ("25.2.1-1", 1),
        "vulkan-radeon-sysforge": ("25.2.1-1", 1),
    })
    # stock is every installed name (a real AUR ``x-sysforge`` still matches itself)
    assert markers.stock["nano"] == "7.2-1"
    assert markers.stock["mesa-sysforge"] == "25.2.1-1"
    assert markers.variants == {
        "mesa": [("mesa-sysforge", "25.2.1-1")],
        "vulkan-radeon": [("vulkan-radeon-sysforge", "25.2.1-1")],
    }


def test_render_aur_marks_installed_same_version(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"foo-git": ("1.0-1", 1)})
    out = search_cmd.render_aur([_aur("foo-git", "1.0-1")], m)
    assert "aur/foo-git 1.0-1 [installed]" in out


def test_render_aur_marks_installed_different_version(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"foo-git": ("0.9-1", 1)})
    out = search_cmd.render_aur([_aur("foo-git", "1.0-1")], m)
    assert "aur/foo-git 1.0-1 [installed: 0.9-1]" in out


def test_render_aur_uninstalled_carries_no_marker(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = search_cmd.render_aur([_aur("foo-git")], search_cmd.installed_markers({}))
    assert "[installed" not in out


def test_render_aur_marks_sysforge_variant(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"foo-git-sysforge": ("1.0-1", 1)})
    out = search_cmd.render_aur([_aur("foo-git", "1.0-1")], m)
    assert "aur/foo-git 1.0-1 [installed as foo-git-sysforge]" in out


def test_render_aur_marks_sysforge_variant_version_skew(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"foo-git-sysforge": ("0.9-1", 1)})
    out = search_cmd.render_aur([_aur("foo-git", "1.0-1")], m)
    assert "[installed as foo-git-sysforge: 0.9-1]" in out


def test_render_aur_marker_coloured(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    m = search_cmd.installed_markers({"foo-git": ("1.0-1", 1)})
    out = search_cmd.render_aur([_aur("foo-git", "1.0-1")], m)
    assert "\033[36m[installed]\033[0m" in out


def test_mark_repo_variants_appends_to_header_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"mesa-sysforge": ("25.2.1-1", 1)})
    body = ("extra/mesa 1:25.2.1-1\n"
            "    Open-source OpenGL drivers\n"
            "extra/nano 7.2-1\n"
            "    Pico editor clone\n")
    out = search_cmd.mark_repo_variants(body, m)
    lines = out.splitlines()
    assert lines[0] == "extra/mesa 1:25.2.1-1 [installed as mesa-sysforge: 25.2.1-1]"
    assert lines[1] == "    Open-source OpenGL drivers"
    assert lines[2] == "extra/nano 7.2-1"


def test_mark_repo_variants_parses_through_pacman_colour(monkeypatch):
    """pacman's forced-colour header wraps repo/name/version in SGR codes; the
    name must still be recovered and the marker appended after the colour reset."""
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({"mesa-sysforge": ("25.2.1-1", 1)})
    head = ("\033[1m\033[35mextra/\033[0m\033[1mmesa \033[0m\033[1m\033[32m"
            "25.2.1-1\033[0m\033[1m \033[36m[installed]\033[0m")
    out = search_cmd.mark_repo_variants(head + "\n    desc\n", m)
    assert out.splitlines()[0] == head + " [installed as mesa-sysforge]"


def test_mark_repo_variants_noop_without_variants():
    body = "extra/mesa 1:25.2.1-1\n    desc\n"
    assert search_cmd.mark_repo_variants(body, search_cmd.installed_markers({})) == body


def test_execute_threads_markers_into_repo_and_aur(monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(search_cmd.pacman, "get_installed_facts",
                        lambda: {"mesa-sysforge": ("25.2.1-1", 1), "mesa-git": ("r1-1", 1)})
    monkeypatch.setattr(search_cmd.pacman, "search_local",
                        lambda t: "local/mesa-sysforge 25.2.1-1\n")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "extra/mesa 25.2.1-1\n    d\n")
    monkeypatch.setattr(search_cmd.aur, "aur_search", lambda t: [_aur("mesa-git", "r1-1")])
    search_cmd.SearchVerb().execute(SimpleNamespace(term="mesa"), PreCheckResult(ctx={}))
    out = capsys.readouterr().err
    assert "== Installed ==" in out                      # section kept
    assert "extra/mesa 25.2.1-1 [installed as mesa-sysforge]" in out
    assert "aur/mesa-git r1-1 [installed]" in out


def test_installed_markers_merges_dropin_substitutes():
    """A -git drop-in (conflicts+provides the stock name) marks the stock name."""
    markers = search_cmd.installed_markers(
        {"cosmic-comp-git": ("1.9.0.r5-1", 1)},
        {"cosmic-comp": [("cosmic-comp-git", "1.9.0.r5-1")]},
    )
    assert markers.variants == {"cosmic-comp": [("cosmic-comp-git", "1.9.0.r5-1")]}


def test_installed_markers_dedups_sysforge_suffix_and_replaces():
    """A conflict-mode -sysforge build matches both by suffix and by replaces:
    one entry, not two."""
    markers = search_cmd.installed_markers(
        {"mesa-sysforge": ("25.2.1-1", 1)},
        {"mesa": [("mesa-sysforge", "25.2.1-1")]},
    )
    assert markers.variants == {"mesa": [("mesa-sysforge", "25.2.1-1")]}


def test_mark_repo_variants_marks_git_dropin(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers(
        {"cosmic-comp-git": ("1.9.0.r5-1", 1)},
        {"cosmic-comp": [("cosmic-comp-git", "1.9.0.r5-1")]},
    )
    out = search_cmd.mark_repo_variants("extra/cosmic-comp 1:1.9.0-1\n    d\n", m)
    assert out.splitlines()[0] == (
        "extra/cosmic-comp 1:1.9.0-1 [installed as cosmic-comp-git: 1.9.0.r5-1]")


def test_variant_marker_lists_multiple_standins(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    m = search_cmd.installed_markers({}, {"foo": [("foo-git", "2-1"), ("foo-sysforge", "1-1")]})
    out = search_cmd.mark_repo_variants("extra/foo 1-1\n", m)
    assert out.splitlines()[0] == "extra/foo 1-1 [installed as foo-git: 2-1, foo-sysforge]"
