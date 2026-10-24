# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Every command is run by an end-to-end scenario or listed with a reason (3.5.0-DEV23).

A new command fails here until a scenario under tools/e2e/scenarios/ runs it or
tools/e2e/uncovered.toml says why none can; a listing a scenario has since
made redundant fails too.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _tool():
    spec = importlib.util.spec_from_file_location(
        "e2e_coverage", REPO / "tools" / "e2e_coverage.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cov = _tool()


def test_every_command_is_run_or_listed():
    known = cov.commands()
    assert cov.problems(known, cov.covered(known), cov.uncovered()) == []


def _scenario(tmp_path, text):
    d = tmp_path / "scenarios"
    d.mkdir()
    (d / "10-x.sh").write_text(text)
    return d


def test_a_two_word_command_wins_over_its_first_word(tmp_path):
    d = _scenario(tmp_path, "sysforge state list --json </dev/null\n")
    assert cov.covered({"state list", "state", "build"}, d) == {"state list": ["10-x"]}


def test_flags_before_the_command_are_skipped(tmp_path):
    d = _scenario(tmp_path, "sysforge -vv run kernel --dry-run\n")
    assert cov.covered({"run kernel"}, d) == {"run kernel": ["10-x"]}


def test_paths_and_comments_do_not_count(tmp_path):
    d = _scenario(tmp_path, "# sysforge build foo\ngrep x /etc/sysforge/build\n")
    assert cov.covered({"build"}, d) == {}


def test_wrapped_invocations_count(tmp_path):
    d = _scenario(tmp_path, "output_has x sysforge packages list </dev/null\n"
                            "answer 'p' y -- sysforge build foo\n")
    assert set(cov.covered({"packages list", "build"}, d)) == {"packages list", "build"}


def test_problems_name_each_kind():
    known = {"build", "update", "log"}
    runs = {"build": ["10-x"], "log": ["20-y"]}
    listed = {"log": "reason", "gone": "reason", "update": " "}
    out = cov.problems(known, runs, listed)
    assert any("`sysforge log` is listed" in p for p in out)
    assert any("lists `gone`" in p for p in out)
    assert any("no reason for `update`" in p for p in out)
    assert not any("run by no scenario" in p for p in out)
    assert cov.problems({"build", "x"}, {"build": ["a"]}, {}) == [
        "`sysforge x` is run by no scenario and not listed in uncovered.toml"]
