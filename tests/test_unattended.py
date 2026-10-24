# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
Unattended runs (3.5.0-F21): one switch that makes every prompt behave as if
stdin were closed, even at a terminal.

Resolution is ``--unattended`` > ``SYSFORGE_UNATTENDED`` > ``[ui] unattended``
> off, applied once in ``cli`` through ``prompt.set_unattended``. The prompt
helpers then take their EOF path without reading, so a call site that forgot
to check ``is_interactive()`` still cannot block.
"""
import builtins
import re
from pathlib import Path

import pytest

from sysforge.primitives import prompt


@pytest.fixture
def at_a_terminal(monkeypatch):
    monkeypatch.setattr(prompt.sys.stdin, "isatty", lambda: True, raising=False)


@pytest.fixture
def no_reads(monkeypatch):
    def _fail(*_a, **_k):
        raise AssertionError("an unattended prompt read stdin")
    monkeypatch.setattr(builtins, "input", _fail)


# ---------------------------------------------------------------------------
# prompt.py behaviour
# ---------------------------------------------------------------------------

def test_is_interactive_is_false_when_unattended_even_at_a_terminal(at_a_terminal):
    assert prompt.is_interactive() is True
    prompt.set_unattended(True)
    assert prompt.is_interactive() is False
    assert prompt.unattended() is True


def test_prompt_choice_returns_its_eof_answer_without_reading(no_reads):
    prompt.set_unattended(True)
    assert prompt.prompt_choice("Go? [y/N] ", ("y", "n"), default="y",
                                eof_default="n") == "n"
    assert prompt.prompt_choice("Go? [Y/n] ", ("y", "n"), default="y") == "y"


def test_prompt_text_returns_its_eof_answer_without_reading(no_reads):
    prompt.set_unattended(True)
    assert prompt.prompt_text("Name: ", default="a", eof_default="b") == "b"
    assert prompt.prompt_text("Name: ", default="a") == "a"


def test_prompt_key_raises_eof_without_reading(no_reads):
    """prompt_key's no-answer contract is EOFError, so callers hit their abort path."""
    prompt.set_unattended(True)
    with pytest.raises(EOFError):
        prompt.prompt_key("[r]etry [a]bort? ")


def test_no_prompt_reason_names_the_cause(at_a_terminal):
    assert prompt.no_prompt_reason() == "no terminal"
    prompt.set_unattended(True)
    assert prompt.no_prompt_reason() == "unattended run"


# ---------------------------------------------------------------------------
# cli resolution
# ---------------------------------------------------------------------------

def _config(monkeypatch, value):
    monkeypatch.setattr(
        "sysforge.primitives.config.load_sysforge_toml",
        lambda: {"ui": {} if value is None else {"unattended": value}},
    )


def test_resolve_unattended_defaults_off(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.delenv("SYSFORGE_UNATTENDED", raising=False)
    _config(monkeypatch, None)
    assert cli._resolve_unattended(False) is False


def test_resolve_unattended_reads_the_config(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.delenv("SYSFORGE_UNATTENDED", raising=False)
    _config(monkeypatch, True)
    assert cli._resolve_unattended(False) is True


def test_resolve_unattended_ignores_a_non_bool_config_value(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.delenv("SYSFORGE_UNATTENDED", raising=False)
    _config(monkeypatch, "yes")
    assert cli._resolve_unattended(False) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_resolve_unattended_env_turns_it_on(monkeypatch, value):
    import sysforge.cli as cli
    monkeypatch.setenv("SYSFORGE_UNATTENDED", value)
    _config(monkeypatch, False)
    assert cli._resolve_unattended(False) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_resolve_unattended_env_turns_a_configured_on_off(monkeypatch, value):
    """The env var is the way to get prompts back for one run."""
    import sysforge.cli as cli
    monkeypatch.setenv("SYSFORGE_UNATTENDED", value)
    _config(monkeypatch, True)
    assert cli._resolve_unattended(False) is False


def test_resolve_unattended_flag_wins_over_env_and_config(monkeypatch):
    import sysforge.cli as cli
    monkeypatch.setenv("SYSFORGE_UNATTENDED", "0")
    _config(monkeypatch, False)
    assert cli._resolve_unattended(True) is True


def test_unattended_is_a_hoisted_global_flag():
    from sysforge.cli import _build_parser, _hoist_global_flags
    assert _hoist_global_flags(["update", "--unattended"]) == ["--unattended", "update"]
    assert _build_parser().parse_args(["--unattended", "doctor"]).unattended is True
    assert _build_parser().parse_args(["doctor"]).unattended is False


# ---------------------------------------------------------------------------
# One home: no stdin TTY check outside prompt.py
# ---------------------------------------------------------------------------

def test_no_stdin_isatty_outside_prompt():
    """A direct check would ignore the unattended switch (3.5.0-F21)."""
    root = Path(__file__).resolve().parent.parent / "sysforge"
    pattern = re.compile(r"stdin\.isatty\(")
    offenders = [
        f"{p.relative_to(root.parent)}:{n}"
        for p in root.rglob("*.py")
        if p.name != "prompt.py"
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == [], f"use prompt.is_interactive(): {offenders}"
