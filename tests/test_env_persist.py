# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Tests for sysforge.primitives.env_persist."""
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives.env_chain import _parse_shell_init_file
from sysforge.primitives.env_persist import (
    EnvTarget,
    apply_write,
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


@pytest.mark.parametrize(
    "value",
    [
        "a'b",             # shlex.quote renders 'a'"'"'b' — no reader parses it
        'a"b',
        "vim\nrm -rf /",   # a newline becomes a second, bogus assignment line
        "vim\r",
        "",                # no meaningful assignment
        " vim",            # leading space is lost by the reader's strip()
        "vim ",
    ],
)
@pytest.mark.parametrize("syntax", ["bare", "export"])
def test_format_assignment_rejects_values_it_cannot_round_trip(value, syntax):
    """The primitive is safe on its own terms, not by caller discipline.

    Today's only caller pre-filters through ``shutil.which``; a second caller
    must not be able to write an unparseable ``/etc/environment`` line.
    """
    with pytest.raises(ValueError, match="cannot be persisted"):
        format_assignment("EDITOR", value, syntax)


def test_plan_write_rejects_an_unpersistable_value(tmp_path):
    """Rejection lands at plan time, before anything is written."""
    with pytest.raises(ValueError, match="cannot be persisted"):
        plan_write(_export_target(tmp_path), {"EDITOR": "a'b"}, None)


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


def test_plan_reads_current_from_the_split_export_form(tmp_path):
    """``KEY=value; export KEY`` is a form ``env_chain`` parses, so the
    planner must not report ``currently unset`` beneath a chain display
    showing the real old value."""
    existing = "EDITOR=vim; export EDITOR\nVISUAL=vim; export VISUAL\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert [c.current for c in plan.changes] == ["vim", "vim"]


def test_plan_replaces_the_split_export_form_in_place(tmp_path):
    """It is replaced, not appended beneath — otherwise the file grows a
    duplicate assignment on every run."""
    existing = "EDITOR=vim; export EDITOR\n"
    plan = plan_write(_export_target(tmp_path), {"EDITOR": "nvim"}, existing)
    assert plan.action == "replace"
    assert plan.new_text == "export EDITOR=nvim\n"


def test_plan_nochange_when_split_export_form_already_correct(tmp_path):
    existing = "EDITOR=nvim; export EDITOR\nVISUAL=nvim; export VISUAL\n"
    plan = plan_write(_export_target(tmp_path), VARS, existing)
    assert plan.action == "nochange"


@pytest.mark.parametrize("factory", [_bare_target, _export_target])
def test_plan_current_agrees_with_env_chain_on_the_split_form(factory, tmp_path):
    """The planner's ``current`` must be the value ``env_chain`` reports.

    ``_parse_shell_init_file`` applies the split-export pattern before its
    ``allow_bare`` fallback, so it reads this form on *both* targets. A
    planner that disagreed would print ``currently unset`` — or the whole
    line as the value — beneath a chain display showing the real one.
    """
    target = factory(tmp_path)
    existing = "EDITOR=vim; export EDITOR\n"
    target.path.write_text(existing)
    kv, _ = _parse_shell_init_file(target.path, allow_bare=target.syntax == "bare")

    plan = plan_write(target, {"EDITOR": "nvim"}, existing)
    assert plan.changes[0].current == kv["EDITOR"] == "vim"


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
    ), patch(
        "sysforge.primitives.env_persist.privileged_argv",
        return_value=["<escalated>", "cp"],
    ) as priv, patch("sysforge.primitives.env_persist.subprocess.run") as run:
        run.return_value.returncode = 0
        apply_write(plan)

    # Assert on the seam, not on the literal it abstracts: an "sudo" assertion
    # here would pin the very implementation detail §22 exists to hide.
    inner = priv.call_args[0][0]
    assert inner[0] == "cp"
    assert inner[-1] == str(target.path)
    assert run.call_args[0][0] == ["<escalated>", "cp"]


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


@pytest.mark.parametrize("factory", [_bare_target, _export_target])
@pytest.mark.parametrize(
    "value",
    [
        "nvim",                    # safe: written bare
        "code -w",                 # space: shlex.quote, the one form all readers agree on
        "/usr/bin/env nvim",
        "emacsclient -a=",         # '=' is inside _SAFE_VALUE
        "nvim#1",                  # '#' must not be taken as an inline comment
    ],
)
def test_round_trip_holds_for_every_value_the_writer_accepts(value, factory, tmp_path):
    """The counterpart to the rejection matrix: anything
    ``_reject_unpersistable`` lets through must survive the round trip, or the
    rejection rule is drawn in the wrong place."""
    target = factory(tmp_path)
    apply_write(plan_write(target, {"EDITOR": value}, None))
    kv, _ = _parse_shell_init_file(
        target.path, allow_bare=target.syntax == "bare"
    )
    assert kv["EDITOR"] == value
