# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Tests for sysforge.primitives.env_persist."""
from pathlib import Path


from sysforge.primitives.env_persist import (
    EnvTarget,
    format_assignment,
    plan_write,
    system_target,
    user_target,
)

VARS = {"EDITOR": "nvim", "VISUAL": "nvim"}


def _bare_target(tmp_path: Path) -> EnvTarget:
    return EnvTarget(
        key="system",
        label="system-wide",
        path=tmp_path / "environment",
        syntax="bare",
        scope_note="all users, next login",
        needs_root=True,
    )


def _export_target(tmp_path: Path) -> EnvTarget:
    return EnvTarget(
        key="user",
        label="this user",
        path=tmp_path / ".zshenv",
        syntax="export",
        scope_note="this user, next shell",
        needs_root=False,
    )


def test_format_assignment_bare():
    assert format_assignment("EDITOR", "nvim", "bare") == "EDITOR=nvim"


def test_format_assignment_export():
    assert format_assignment("EDITOR", "nvim", "export") == "export EDITOR=nvim"


def test_format_assignment_quotes_value_with_spaces():
    assert format_assignment("EDITOR", "code -w", "export") == "export EDITOR='code -w'"


def test_plan_create_when_file_absent(tmp_path):
    plan = plan_write(_export_target(tmp_path), VARS, None)
    assert plan.action == "create"
    assert plan.new_text == "export EDITOR=nvim\nexport VISUAL=nvim\n"
    assert [(c.name, c.current, c.new) for c in plan.changes] == [
        ("EDITOR", None, "nvim"),
        ("VISUAL", None, "nvim"),
    ]


def test_plan_append_keeps_existing_content(tmp_path):
    existing = "# my zshenv\nexport PATH=/usr/bin\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert plan.action == "append"
    assert plan.new_text == existing + "export EDITOR=nvim\nexport VISUAL=nvim\n"


def test_plan_append_adds_missing_trailing_newline(tmp_path):
    plan = plan_write(_export_target(tmp_path), VARS, "export PATH=/usr/bin")
    assert plan.new_text.startswith("export PATH=/usr/bin\nexport EDITOR=")


def test_plan_replace_rewrites_in_place(tmp_path):
    existing = "export EDITOR=vim\nexport PATH=/usr/bin\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert plan.action == "replace"
    assert plan.new_text == (
        "export EDITOR=nvim\nexport PATH=/usr/bin\nexport VISUAL=nvim\n"
    )
    editor_change = plan.changes[0]
    assert (editor_change.current, editor_change.new) == ("vim", "nvim")


def test_plan_replace_drops_duplicate_assignments(tmp_path):
    existing = "export EDITOR=vim\nexport PATH=/usr/bin\nexport EDITOR=emacs\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert plan.new_text.count("EDITOR=") == 1
    # Last assignment is what the shell actually applies, so it is the
    # "current" value reported to the user.
    assert plan.changes[0].current == "emacs"


def test_plan_nochange_when_both_already_correct(tmp_path):
    existing = "export EDITOR=nvim\nexport VISUAL=nvim\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert plan.action == "nochange"
    assert plan.new_text == existing
    assert all(not c.is_change for c in plan.changes)


def test_plan_bare_syntax_ignores_export_lines_it_cannot_write(tmp_path):
    # /etc/environment is not sourced by a shell; an `export` line there is
    # not an assignment pam_env understands, so it is left alone and a bare
    # assignment is appended.
    plan = plan_write(_bare_target(tmp_path), VARS, "export EDITOR=vim\n")
    assert plan.action == "append"
    assert "export EDITOR=vim" in plan.new_text
    assert "EDITOR=nvim" in plan.new_text


def test_plan_strips_quotes_when_reporting_current(tmp_path):
    plan = plan_write(_export_target(tmp_path), VARS, 'export EDITOR="vim"\n')
    assert plan.changes[0].current == "vim"


def test_plan_ignores_commented_assignment(tmp_path):
    plan = plan_write(_export_target(tmp_path), VARS, "# export EDITOR=vim\n")
    assert plan.action == "append"
    assert plan.changes[0].current is None


def test_system_target_is_etc_environment():
    t = system_target()
    assert t.path == Path("/etc/environment")
    assert t.syntax == "bare"
    assert t.needs_root is True


def test_user_target_follows_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    t = user_target()
    assert t.path == tmp_path / ".zshenv"
    assert t.syntax == "export"
    assert t.needs_root is False


from unittest.mock import patch

import pytest

from sysforge.primitives.env_chain import _parse_shell_init_file
from sysforge.primitives.env_persist import apply_write


def test_apply_write_creates_file(tmp_path):
    target = _export_target(tmp_path)
    apply_write(plan_write(target, VARS, None))
    assert target.path.read_text() == "export EDITOR=nvim\nexport VISUAL=nvim\n"


def test_apply_write_nochange_is_a_noop(tmp_path):
    target = _export_target(tmp_path)
    target.path.write_text("export EDITOR=nvim\nexport VISUAL=nvim\n")
    before = target.path.stat().st_mtime_ns
    apply_write(plan_write(target, VARS, target.path.read_text()))
    assert target.path.stat().st_mtime_ns == before


def test_apply_write_preserves_unrelated_lines(tmp_path):
    target = _export_target(tmp_path)
    target.path.write_text("export PATH=/usr/bin\nexport EDITOR=vim\n")
    apply_write(plan_write(target, VARS, target.path.read_text()))
    text = target.path.read_text()
    assert "export PATH=/usr/bin" in text
    assert "export EDITOR=nvim" in text
    assert "export EDITOR=vim" not in text


def test_apply_write_escalates_when_unwritable(tmp_path):
    """A root-owned target stages to a temp file and copies via the
    privilege seam — never a hand-rolled sudo argv."""
    target = _export_target(tmp_path)
    plan = plan_write(target, VARS, None)
    with patch(
        "sysforge.primitives.env_persist.Path.write_text",
        side_effect=PermissionError,
    ), patch("sysforge.primitives.env_persist.subprocess.run") as run:
        run.return_value.returncode = 0
        apply_write(plan)
    argv = run.call_args[0][0]
    assert argv[0] == "sudo"
    assert argv[1] == "cp"
    assert argv[-1] == str(target.path)


def test_apply_write_raises_when_escalation_fails(tmp_path):
    target = _export_target(tmp_path)
    plan = plan_write(target, VARS, None)
    with patch(
        "sysforge.primitives.env_persist.Path.write_text",
        side_effect=PermissionError,
    ), patch("sysforge.primitives.env_persist.subprocess.run") as run:
        run.return_value.returncode = 1
        with pytest.raises(OSError, match="exited 1"):
            apply_write(plan)


@pytest.mark.parametrize("factory", [_bare_target, _export_target])
def test_round_trip_env_chain_reads_back_what_we_wrote(factory, tmp_path):
    """The load-bearing guard: env_persist must write the syntax
    env_chain._parse_shell_init_file accepts for that file, or a value
    sysforge just wrote is invisible to `sysforge env`."""
    target = factory(tmp_path)
    apply_write(plan_write(target, VARS, None))
    allow_bare = target.syntax == "bare"
    kv, _ = _parse_shell_init_file(target.path, allow_bare=allow_bare)
    assert kv["EDITOR"] == "nvim"
    assert kv["VISUAL"] == "nvim"
