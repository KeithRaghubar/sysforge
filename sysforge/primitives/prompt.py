"""
prompt.py — shared interactive-prompt helpers.

Every stage that needs user input must go through these so the behaviour on
empty input, unrecognized input, and EOF is consistent across the codebase.
The prompt prefix follows the standard ``[SYSFORGE][LEVEL][TAG]`` format
produced by :func:`sysforge.log.prompt_prefix`.

Three functions are provided:

* :func:`prompt_text`   — free-form single-line input with a default.
* :func:`prompt_choice` — single-token choice from a fixed lowercase set,
  re-prompts on unrecognized input by default.
* :func:`prompt_key`    — single keypress (no Enter) read in cbreak mode,
  falling back to line input when stdin is not a real terminal.

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


def prompt_key(
    msg: str,
    *,
    tag: str | None = None,
    level: str = "UI",
) -> str:
    """Prompt for a single keypress — no Enter required.

    Reads one character from stdin in cbreak mode, echoes it followed by a
    newline (so the transcript still shows what was answered), and returns
    it lowercased. The caller owns choice validation and any re-prompt loop,
    matching :func:`prompt_choice`'s contract.

    Fallback: when stdin is not a real terminal (pipes, captured stdin in
    tests) or raw-mode setup fails, degrades to line-based ``input()`` and
    returns the first character of the stripped line.

    Control keys: Ctrl-C raises ``KeyboardInterrupt`` (cbreak mode delivers
    it as ``\\x03`` rather than a signal); Ctrl-D / EOF / unreadable stdin
    raise ``EOFError`` so callers hit the same abort path as line prompts.
    A bare Enter (or empty line in fallback mode) returns ``""`` — "no
    answer", distinct from EOF — so callers can re-prompt.
    """
    full = _format_prefix(tag, level) + msg

    def _fallback(prompt: str) -> str:
        try:
            raw = input(prompt).strip().lower()
        except OSError as e:
            # pytest's captured stdin and other unreadable-stdin scenarios.
            raise EOFError from e
        return raw[:1]

    if not sys.stdin.isatty():
        return _fallback(full)
    try:
        import termios
        import tty
    except ImportError:
        return _fallback(full)
    sys.stdout.write(full)
    sys.stdout.flush()
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError):
        # Covers pytest's captured stdin (fileno() raises
        # io.UnsupportedOperation) and any tty that refuses raw mode.
        return _fallback("")
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise KeyboardInterrupt
    if ch in ("", "\x04"):
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise EOFError
    if ch in ("\r", "\n"):
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ""
    echo = ch if ch.isprintable() else ""
    sys.stdout.write(f"{echo}\n")
    sys.stdout.flush()
    return ch.lower()
