"""
test_prompt.py — unit tests for sysforge.primitives.prompt.

Covers `is_interactive`, `prompt_text`, and `prompt_choice`. All input is
driven through `monkeypatch.setattr("builtins.input", ...)` so tests never
read real stdin.
"""
from unittest.mock import patch

from sysforge.primitives.prompt import (
    is_interactive,
    prompt_choice,
    prompt_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scripted_input(monkeypatch, responses):
    """Replace builtins.input with a function that returns the next scripted
    response per call. Raise StopIteration if the prompt asks more than
    expected — that surfaces test bugs."""
    it = iter(responses)

    def fake_input(prompt: str = "") -> str:
        return next(it)

    monkeypatch.setattr("builtins.input", fake_input)


def _eof_input(monkeypatch, exc: type[BaseException] = EOFError):
    def fake_input(prompt: str = "") -> str:
        raise exc

    monkeypatch.setattr("builtins.input", fake_input)


# ---------------------------------------------------------------------------
# is_interactive
# ---------------------------------------------------------------------------

def test_is_interactive_returns_true_when_tty():
    with patch("sysforge.primitives.prompt.sys.stdin.isatty", return_value=True):
        assert is_interactive() is True


def test_is_interactive_returns_false_when_not_tty():
    with patch("sysforge.primitives.prompt.sys.stdin.isatty", return_value=False):
        assert is_interactive() is False


# ---------------------------------------------------------------------------
# prompt_text
# ---------------------------------------------------------------------------

def test_prompt_text_returns_user_input(monkeypatch):
    _scripted_input(monkeypatch, ["hello"])
    assert prompt_text("Q: ") == "hello"


def test_prompt_text_strips_whitespace(monkeypatch):
    _scripted_input(monkeypatch, ["  spaced  "])
    assert prompt_text("Q: ") == "spaced"


def test_prompt_text_empty_returns_default(monkeypatch):
    _scripted_input(monkeypatch, [""])
    assert prompt_text("Q: ", default="fallback") == "fallback"


def test_prompt_text_eof_returns_default(monkeypatch):
    _eof_input(monkeypatch)
    assert prompt_text("Q: ", default="fallback") == "fallback"


def test_prompt_text_eof_default_overrides_default(monkeypatch):
    _eof_input(monkeypatch)
    assert prompt_text("Q: ", default="d", eof_default="e") == "e"


def test_prompt_text_oserror_treated_as_eof(monkeypatch):
    """Pytest's captured stdin raises OSError, not EOFError."""
    _eof_input(monkeypatch, exc=OSError)
    assert prompt_text("Q: ", default="d", eof_default="e") == "e"


def test_prompt_text_tag_prefix(monkeypatch):
    """When tag is given, the prompt prefix is prepended."""
    captured = []

    def fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        return "x"

    monkeypatch.setattr("builtins.input", fake_input)
    prompt_text("Q: ", tag="MYTAG", level="WARN")
    assert captured == ["[SYSFORGE][WARN][MYTAG] Q: "]


# ---------------------------------------------------------------------------
# prompt_choice
# ---------------------------------------------------------------------------

def test_prompt_choice_returns_valid_choice(monkeypatch):
    _scripted_input(monkeypatch, ["y"])
    assert prompt_choice("Q? ", choices=("y", "n")) == "y"


def test_prompt_choice_lowercases_input(monkeypatch):
    _scripted_input(monkeypatch, ["Y"])
    assert prompt_choice("Q? ", choices=("y", "n")) == "y"


def test_prompt_choice_empty_returns_default(monkeypatch):
    _scripted_input(monkeypatch, [""])
    assert prompt_choice("Q? ", choices=("y", "n"), default="n") == "n"


def test_prompt_choice_eof_returns_eof_default(monkeypatch):
    _eof_input(monkeypatch)
    assert prompt_choice(
        "Q? ", choices=("y", "n"), default="n", eof_default="y"
    ) == "y"


def test_prompt_choice_eof_falls_back_to_default(monkeypatch):
    """When eof_default is None, EOF returns `default`."""
    _eof_input(monkeypatch)
    assert prompt_choice("Q? ", choices=("y", "n"), default="n") == "n"


def test_prompt_choice_oserror_treated_as_eof(monkeypatch):
    _eof_input(monkeypatch, exc=OSError)
    assert prompt_choice(
        "Q? ", choices=("y", "n"), default="n", eof_default="y"
    ) == "y"


def test_prompt_choice_invalid_input_reprompts(monkeypatch):
    """Jibberish must trigger a re-prompt with a warning, not silently default."""
    _scripted_input(monkeypatch, ["xyz", "asdf", "y"])
    warnings = []
    fake_log = type("F", (), {"warn": lambda self, msg: warnings.append(msg)})()
    with patch("sysforge.primitives.prompt._log", fake_log):
        result = prompt_choice("Q? ", choices=("y", "n"))
    assert result == "y"
    assert len(warnings) == 2
    assert "xyz" in warnings[0]
    assert "asdf" in warnings[1]


def test_prompt_choice_retry_on_invalid_false_returns_default(monkeypatch):
    """Destructive prompts: any non-confirming input must fall through to
    the default, no re-prompt, no warning."""
    _scripted_input(monkeypatch, ["maybe"])
    warnings = []
    fake_log = type("F", (), {"warn": lambda self, msg: warnings.append(msg)})()
    with patch("sysforge.primitives.prompt._log", fake_log):
        result = prompt_choice(
            "Type 'yes' to proceed: ",
            choices=("yes",),
            default="",
            retry_on_invalid=False,
        )
    assert result == ""
    assert warnings == []


def test_prompt_choice_retry_on_invalid_false_accepts_valid(monkeypatch):
    _scripted_input(monkeypatch, ["yes"])
    assert prompt_choice(
        "Confirm: ",
        choices=("yes",),
        default="",
        retry_on_invalid=False,
    ) == "yes"


def test_prompt_choice_full_word_choices(monkeypatch):
    """Multi-letter choices like 'abort' or 'plain' must be accepted as-is."""
    _scripted_input(monkeypatch, ["abort"])
    assert prompt_choice(
        "Q? ", choices=("s", "abort"), default=""
    ) == "abort"


def test_prompt_choice_tag_prefix(monkeypatch):
    captured = []

    def fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    prompt_choice("Q? ", choices=("y", "n"), tag="STAGE", level="WARN")
    assert captured == ["[SYSFORGE][WARN][STAGE] Q? "]
