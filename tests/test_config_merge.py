# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for `sysforge config merge` (.sfnew/.pacnew adoption) and the shared
editor/merge-tool resolution in primitives/editor.py."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from sysforge import config_cmd
from sysforge.primitives import editor


def _args(config_dir: Path, **kw) -> argparse.Namespace:
    base = dict(config_dir=str(config_dir), no_pager=True, list=False, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _scripted(answers):
    """Return a fake prompt that yields each answer in turn."""
    it = iter(answers)

    def fake(*a, **k):
        return next(it)

    return fake


def _make_pair(config_dir: Path, suffix: str = ".sfnew") -> tuple[Path, Path]:
    """Create a live file + a companion; return (target, companion)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "sysforge.toml"
    target.write_text('key = "live"\n', encoding="utf-8")
    companion = config_dir / f"sysforge.toml{suffix}"
    companion.write_text("# doc comment\nkey = \"shipped\"\n", encoding="utf-8")
    return target, companion


# --------------------------------------------------------------------------- #
# candidate discovery
# --------------------------------------------------------------------------- #

def test_candidates_finds_all_companion_suffixes(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "a.toml.sfnew").write_text("", encoding="utf-8")
    (config_dir / "b.toml.pacnew").write_text("", encoding="utf-8")
    (config_dir / "c.toml.pacsave").write_text("", encoding="utf-8")
    (config_dir / "d.toml").write_text("", encoding="utf-8")  # not a companion

    pairs = config_cmd._candidates(config_dir)
    names = {p[0].name for p in pairs}
    assert names == {"a.toml.sfnew", "b.toml.pacnew", "c.toml.pacsave"}
    # target is the companion with its final extension stripped
    by_new = {p[0].name: p[1].name for p in pairs}
    assert by_new["a.toml.sfnew"] == "a.toml"
    assert by_new["b.toml.pacnew"] == "b.toml"


# --------------------------------------------------------------------------- #
# cmd_config_merge — top-level flows
# --------------------------------------------------------------------------- #

def test_no_companions_returns_zero(tmp_path, capsys):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    # _log.ui writes diagnostics to stderr (stdout/stderr separation contract).
    assert "nothing to merge" in capsys.readouterr().err


def test_missing_config_dir_returns_one(tmp_path):
    assert config_cmd.cmd_config_merge(_args(tmp_path / "absent")) == 1


def test_list_mode_reports_without_prompting(tmp_path, capsys, monkeypatch):
    config_dir = tmp_path / "cfg"
    _make_pair(config_dir)
    # prompt_key must never be called in --list mode.
    def _boom(*a, **k):
        pytest.fail("prompt_key called in --list mode")
    monkeypatch.setattr(config_cmd, "prompt_key", _boom)
    assert config_cmd.cmd_config_merge(_args(config_dir, list=True)) == 0
    out = capsys.readouterr().out
    assert "sysforge.toml.sfnew" in out and "live present" in out


# --------------------------------------------------------------------------- #
# interactive loop outcomes
# --------------------------------------------------------------------------- #

def test_remove_unlinks_companion_and_keeps_live(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    target, companion = _make_pair(config_dir)
    monkeypatch.setattr(config_cmd, "prompt_key", _scripted(["r"]))
    monkeypatch.setattr(config_cmd, "prompt_choice", lambda *a, **k: "y")

    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    assert not companion.exists()
    assert target.read_text(encoding="utf-8") == 'key = "live"\n'


def test_skip_leaves_companion(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    target, companion = _make_pair(config_dir)
    monkeypatch.setattr(config_cmd, "prompt_key", _scripted(["s"]))

    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    assert companion.exists()


def test_merge_launches_tool_with_live_and_companion(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    target, companion = _make_pair(config_dir)
    # First 'm' launches the tool; then 's' to end the loop.
    monkeypatch.setattr(config_cmd, "prompt_key", _scripted(["m", "s"]))
    monkeypatch.setattr(config_cmd, "resolve_merge_tool",
                        lambda: (["vimdiff"], "vimdiff"))
    captured: list[list[str]] = []
    monkeypatch.setattr(config_cmd, "run_tty_argv",
                        lambda argv: captured.append(argv) or 0)

    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    assert captured == [["vimdiff", str(target), str(companion)]]
    assert companion.exists()  # merge alone doesn't remove it


def test_overwrite_replaces_live_after_confirm(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    target, companion = _make_pair(config_dir)
    monkeypatch.setattr(config_cmd, "prompt_key", _scripted(["o"]))
    monkeypatch.setattr(config_cmd, "prompt_choice", lambda *a, **k: "y")

    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    assert not companion.exists()
    assert target.read_text(encoding="utf-8") == "# doc comment\nkey = \"shipped\"\n"


def test_abort_stops_the_run(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    _make_pair(config_dir, ".sfnew")
    # A second companion in the same dir; abort on the first leaves both.
    (config_dir / "other.toml").write_text("x=1\n", encoding="utf-8")
    (config_dir / "other.toml.sfnew").write_text("# c\nx=2\n", encoding="utf-8")
    monkeypatch.setattr(config_cmd, "prompt_key", _scripted(["b"]))

    assert config_cmd.cmd_config_merge(_args(config_dir)) == 0
    assert (config_dir / "sysforge.toml.sfnew").exists()
    assert (config_dir / "other.toml.sfnew").exists()


# --------------------------------------------------------------------------- #
# resolve_merge_tool precedence
# --------------------------------------------------------------------------- #

@pytest.fixture
def _all_tools_present(monkeypatch):
    monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(editor, "load_sysforge_toml", lambda: {})


def test_merge_tool_env_wins(monkeypatch, _all_tools_present):
    monkeypatch.setenv("SYSFORGE_MERGE", "kdiff3")
    monkeypatch.setenv("DIFFPROG", "meld")
    argv, source = editor.resolve_merge_tool()
    assert argv == ["kdiff3"] and source == "SYSFORGE_MERGE"


def test_merge_tool_config_over_diffprog(monkeypatch, _all_tools_present):
    monkeypatch.delenv("SYSFORGE_MERGE", raising=False)
    monkeypatch.setattr(editor, "load_sysforge_toml", lambda: {"ui": {"merge": "meld"}})
    monkeypatch.setenv("DIFFPROG", "kdiff3")
    argv, source = editor.resolve_merge_tool()
    assert argv == ["meld"] and source == "sysforge.toml"


def test_merge_tool_diffprog_multi_token_split(monkeypatch, _all_tools_present):
    monkeypatch.delenv("SYSFORGE_MERGE", raising=False)
    monkeypatch.setenv("DIFFPROG", "nvim -d")
    argv, source = editor.resolve_merge_tool()
    assert argv == ["nvim", "-d"] and source == "$DIFFPROG"


def test_merge_tool_falls_back_to_vimdiff(monkeypatch):
    monkeypatch.delenv("SYSFORGE_MERGE", raising=False)
    monkeypatch.delenv("DIFFPROG", raising=False)
    monkeypatch.setattr(editor, "load_sysforge_toml", lambda: {})
    # Only vimdiff resolves on PATH.
    monkeypatch.setattr(editor.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name == "vimdiff" else None)
    argv, source = editor.resolve_merge_tool()
    assert argv == ["vimdiff"] and source == "vimdiff"


def test_merge_tool_none_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("SYSFORGE_MERGE", raising=False)
    monkeypatch.delenv("DIFFPROG", raising=False)
    monkeypatch.setattr(editor, "load_sysforge_toml", lambda: {})
    monkeypatch.setattr(editor.shutil, "which", lambda name: None)
    argv, source = editor.resolve_merge_tool()
    assert argv == [] and source == "none"
