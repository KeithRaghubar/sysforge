"""
prompt.py — shared interactive-prompt helpers.

Every stage that needs user input must go through these so the behaviour on
empty input, unrecognized input, and EOF is consistent across the codebase.
The prompt prefix follows the standard ``[SYSFORGE][LEVEL][TAG]`` format
produced by :func:`sysforge.log.prompt_prefix`.

Two functions are provided:

* :func:`prompt_text`   — free-form single-line input with a default.
* :func:`prompt_choice` — single-token choice from a fixed lowercase set,
  re-prompts on unrecognized input by default.

Plus :func:`is_interactive` for stages that need to gate prompts on a TTY.
"""
from __future__ import annotations

import sys
from typing import Iterable

from sysforge import log

_log = log.get_logger("PROMPT")


def is_interactive() -> bool:
    """True when stdin is attached to a TTY."""
    return sys.stdin.isatty()


def _format_prefix(tag: str | None, level: str) -> str:
    if tag is None:
        return ""
    return log.prompt_prefix(level, f"[{tag}]")


def prompt_text(
    msg: str,
    *,
    default: str = "",
    eof_default: str | None = None,
    tag: str | None = None,
    level: str = "UI",
) -> str:
    """Prompt for free-form single-line input.

    Returns the stripped user input, or ``default`` if the user presses Enter
    on an empty line, or ``eof_default`` (defaulting to ``default``) on EOF.

    ``OSError`` from ``input()`` is also treated as EOF — pytest's captured
    stdin raises ``OSError("reading from stdin while output is captured")``,
    and any other unreadable-stdin scenario should fall back gracefully too.
    """
    full = _format_prefix(tag, level) + msg
    try:
        raw = input(full).strip()
    except (EOFError, OSError):
        return default if eof_default is None else eof_default
    return raw or default


def prompt_choice(
    msg: str,
    choices: Iterable[str],
    *,
    default: str = "",
    eof_default: str | None = None,
    retry_on_invalid: bool = True,
    tag: str | None = None,
    level: str = "UI",
) -> str:
    """Prompt for a single token from a fixed set of choices.

    Comparison is case-insensitive (input is lowercased; ``choices`` should
    already be lowercase).

    * ``default`` is returned on empty input (Enter).
    * ``eof_default`` is returned on EOF; falls back to ``default`` when
      ``None``.
    * ``retry_on_invalid`` controls behaviour for unrecognized input:

      - ``True`` (the common case) — re-prompt with a visible warning so
        typos and jibberish never silently become the default.
      - ``False`` — return ``default`` immediately. Use this for destructive
        prompts where any non-confirming input should fall through to abort.
    """
    choices_t = tuple(c.lower() for c in choices)
    full = _format_prefix(tag, level) + msg
    while True:
        try:
            raw = input(full).strip().lower()
        except (EOFError, OSError):
            # OSError covers pytest's captured-stdin and any other unreadable
            # stdin scenario; treat it the same as EOF.
            return default if eof_default is None else eof_default
        if not raw:
            return default
        if raw in choices_t:
            return raw
        if not retry_on_invalid:
            return default
        _log.warn(
            f"Unrecognized input {raw!r}. "
            f"Valid: {'/'.join(choices_t)} (or ↵ for default)."
        )
