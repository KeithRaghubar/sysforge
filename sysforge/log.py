# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
log.py — SysForge structured logging

All output goes to stderr. Verbosity controls which levels are shown on stderr:
  0 (default) — ERROR only
  1 (-v)       — ERROR + WARN
  2 (-vv)      — ERROR + WARN + INFO
  3 (-vvv)     — ERROR + WARN + INFO + DEBUG (full config/profile/conf dumps)

File logging (always full verbosity, all levels):
  Unified log — one file for the entire run, managed by the pipeline runner.
                Default: <state_dir>/sysforge.log
                Persists across runs until a successful pipeline completion,
                then truncated. --purge-log truncates before the run starts.
                --persist-log suppresses truncation on success.
  Per-package log — one file per package build, written alongside the PKGBUILD.
                    Path: <pkgbuild_dir>/<pkgname>/sysforge_<pkgname>.log
                    Same lifecycle as the unified log.

Format: [SYSFORGE][LEVEL][TAG] message

Colour: when the output stream is a TTY and ``NO_COLOR`` is unset, the LEVEL
token is coloured by severity (red for ERROR, yellow for WARN, dim for DEBUG)
and the TAG is coloured cyan. File logs are never coloured.

Usage:
    from sysforge import log

    log.info("[CONF]", f"Wrote temp conf: {path}")
    log.warn("[DEP]", "soname mismatch — continuing")
    log.error("[FAILURE]", "aborting build")

    # Set once at CLI entry point:
    log.set_verbosity(args.verbose)  # 0, 1, 2, or 3

    # Pipeline runner manages the unified log:
    log.open_unified_log(path, purge=False)
    log.close_unified_log(success=True, persist=False)

    # makepkg_wrapper manages per-package logs:
    log.open_pkg_log(path)
    log.close_pkg_log(success=True, persist=False)
"""
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn

_VERBOSITY = 0
_DRY_RUN = False
_COLOR_MODE = "auto"  # one of {"auto", "always", "never"}; see use_color()
_UNICODE_MODE = "auto"  # one of {"auto", "always", "never"}; see use_unicode()
_unified_log_fh = None
_pkg_log_fh = None

# ---------------------------------------------------------------------------
# ANSI colour support — the single colour authority for all sysforge output.
#
# Every output site (structured logging here, ui/headers, ui/progress, doctor
# findings, state tables, build summaries, the review diff) gates on use_color()
# and wraps text with the helpers below — no site hand-writes escape codes.
# ---------------------------------------------------------------------------

_ANSI_RESET  = "\033[0m"
_ANSI_BOLD   = "\033[1m"
_ANSI_DIM    = "\033[2m"
_ANSI_RED    = "\033[31m"
_ANSI_GREEN  = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_CYAN   = "\033[36m"

_LEVEL_SGR = {
    "ERROR": _ANSI_BOLD + _ANSI_RED,
    "WARN":  _ANSI_YELLOW,
    "INFO":  "",
    "DEBUG": _ANSI_DIM,
}

_CLEARED_MARKER = "# log cleared after successful run\n"
_SEP = "# " + "─" * 60 + "\n"


def _session_header(label: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{_SEP}# {label} — {ts}\n{_SEP}"


def set_verbosity(level: int) -> None:
    global _VERBOSITY
    _VERBOSITY = max(0, int(level))


def set_dry_run_mode() -> None:
    """Force max verbosity and redirect all output to stdout for dry-run mode."""
    global _VERBOSITY, _DRY_RUN
    _VERBOSITY = 3
    _DRY_RUN = True


def set_color_mode(mode: str) -> None:
    """Set the global colour mode consulted by :func:`use_color`.

    ``mode`` is one of ``"auto"`` (TTY + NO_COLOR/FORCE_COLOR gated), ``"always"``
    (force colour on, even when piped — e.g. into a pager), or ``"never"`` (force
    plain). An unrecognised value degrades to ``"auto"`` rather than raising, so a
    bad config/CLI value never aborts startup.
    """
    global _COLOR_MODE
    _COLOR_MODE = mode if mode in ("auto", "always", "never") else "auto"


def get_verbosity() -> int:
    return _VERBOSITY


def _out():
    return sys.stdout if _DRY_RUN else sys.stderr


def use_color() -> bool:
    """Return True iff the active output stream should receive ANSI colour.

    Precedence (single source of truth for the whole codebase):
      * colour mode ``"never"`` → False; ``"always"`` → True. An explicit mode
        wins over the environment, so ``--color=always`` colours output even when
        it is piped (e.g. into a pager or a colour-aware tool).
      * mode ``"auto"`` (the default): NO_COLOR (any non-empty value) disables;
        FORCE_COLOR (any non-empty value) forces on; otherwise colour follows
        whether the destination is a TTY.

    Checked per-call so redirecting output mid-run is respected and test captures
    (pytest's capsys) stay plain.
    """
    if _COLOR_MODE == "never":
        return False
    if _COLOR_MODE == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = _out()
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


# Shared colour helpers — wrap *text* in an SGR sequence when use_color() is on,
# otherwise return it unchanged. The whole codebase routes through these so the
# gating stays in one place; no caller hand-writes escape codes.
def _wrap(text: str, sgr: str) -> str:
    return f"{sgr}{text}{_ANSI_RESET}" if use_color() else text


def bold(text: str) -> str:
    return _wrap(text, _ANSI_BOLD)


def dim(text: str) -> str:
    return _wrap(text, _ANSI_DIM)


def red(text: str) -> str:
    return _wrap(text, _ANSI_RED)


def green(text: str) -> str:
    return _wrap(text, _ANSI_GREEN)


def yellow(text: str) -> str:
    return _wrap(text, _ANSI_YELLOW)


def cyan(text: str) -> str:
    return _wrap(text, _ANSI_CYAN)


# ---------------------------------------------------------------------------
# Unicode glyph gating — single source of truth, parallel to use_color().
#
# Terminal output may include decorative non-ASCII glyphs (status marks like
# ✓/✗, arrows, box-drawing rules, ellipses). The Linux framebuffer/VT console
# (TERM=linux, what a bare-metal or QEMU-graphical install sees) loads a
# console font that maps only a subset of code points, so those glyphs render
# as a missing-glyph box. A non-UTF-8 stream (C/POSIX locale) can't encode them
# at all. use_unicode() decides whether to keep the glyphs; downgrade_glyphs()
# rewrites them to ASCII when not. File logs are always UTF-8 and bypass this.
# ---------------------------------------------------------------------------

def set_unicode_mode(mode: str) -> None:
    """Set the global glyph mode consulted by :func:`use_unicode`.

    ``mode`` is ``"auto"`` (TERM/encoding/SYSFORGE_ASCII gated), ``"always"``
    (force Unicode glyphs), or ``"never"`` (force ASCII). Unknown values degrade
    to ``"auto"`` rather than raising, mirroring :func:`set_color_mode`.
    """
    global _UNICODE_MODE
    _UNICODE_MODE = mode if mode in ("auto", "always", "never") else "auto"


def use_unicode() -> bool:
    """Return True iff the active output stream can render decorative glyphs.

    Precedence (single source of truth for the whole codebase):
      * mode ``"never"`` → False; ``"always"`` → True.
      * mode ``"auto"`` (the default): ``SYSFORGE_ASCII`` (any non-empty value)
        forces ASCII; a *known* non-UTF stream encoding forces ASCII; the Linux
        VT console (``TERM=linux``) forces ASCII; otherwise glyphs are kept.

    An unknown/``None`` stream encoding does *not* downgrade — capture sinks and
    odd streams stay Unicode rather than being needlessly stripped. Checked
    per-call so a mid-run redirect is respected.
    """
    if _UNICODE_MODE == "never":
        return False
    if _UNICODE_MODE == "always":
        return True
    if os.environ.get("SYSFORGE_ASCII"):
        return False
    enc = (getattr(_out(), "encoding", None) or "").lower()
    if enc and "utf" not in enc:
        return False
    return os.environ.get("TERM") != "linux"


# Decorative glyph → ASCII fallback. Applied only to terminal-bound text when
# use_unicode() is False. Keep replacements width-frugal and unambiguous.
_GLYPH_FALLBACKS = {
    # arrows
    "→": "->", "←": "<-", "↔": "<->", "↳": "->", "↷": ">>", "▸": ">",
    # status marks
    "✓": "[OK]", "✗": "[X]", "⚠": "(!)", "•": "*", "·": "-",
    # punctuation
    "…": "...", "—": "--", "–": "-", "−": "-", "×": "x", "≥": ">=", "≈": "~=",
    # box-drawing rules / junctions
    "─": "-", "═": "=", "│": "|", "┤": "+", "├": "+", "┬": "+", "┴": "+",
    "┼": "+", "┌": "+", "┐": "+", "└": "+", "┘": "+",
    # block elements (progress bars)
    "█": "#", "▏": "#", "▎": "#", "▍": "#", "▌": "#", "▋": "#", "▊": "#",
    "▉": "#",
}
_GLYPH_TABLE = {ord(k): v for k, v in _GLYPH_FALLBACKS.items()}


def downgrade_glyphs(text: str) -> str:
    """Return *text* with decorative glyphs rewritten to ASCII when the active
    terminal can't render them (``use_unicode()`` is False); otherwise unchanged.

    The single chokepoint for terminal glyph safety — call sites keep writing the
    pretty Unicode and this strips it only where it would corrupt the display.
    """
    if use_unicode():
        return text
    return text.translate(_GLYPH_TABLE)


def _format_line(level: str, tag: str, message: str) -> str:
    """Return a ``[SYSFORGE][LEVEL]<tag> <message>\\n`` line, with ANSI colour
    applied when the output stream is a colour-capable TTY."""
    # Terminal-only path: downgrade decorative glyphs the console can't render.
    # The UTF-8 file logs are written separately from the caller's own `plain`.
    message = downgrade_glyphs(message)
    plain = f"[SYSFORGE][{level}]{tag} {message}\n"
    if not use_color():
        return plain
    r = _ANSI_RESET
    sgr = _LEVEL_SGR.get(level, "")
    lvl_fmt = f"{sgr}{level}{r}" if sgr else level
    tag_fmt = f"{_ANSI_CYAN}{tag}{r}" if tag else ""
    return f"[SYSFORGE][{lvl_fmt}]{tag_fmt} {message}\n"


# ---------------------------------------------------------------------------
# File log management
# ---------------------------------------------------------------------------

def open_unified_log(path, purge: bool = False) -> None:
    """
    Open (or create) the unified log file.
    purge=True truncates the file before writing, regardless of prior content.
    """
    global _unified_log_fh
    path = Path(path)
    # The unified log lives in the state dir; create its parent group-consistently
    # (root:sysforge) when creatable. Lazy import avoids a log<->fs_provision cycle;
    # allow_sudo=False keeps logging from ever prompting — fall back to a plain
    # mkdir, matching the prior best-effort behaviour.
    try:
        from sysforge.primitives import fs_provision

        fs_provision.ensure_writable_dir(path.parent, allow_sudo=False)
    except Exception:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if purge else "a"
    _unified_log_fh = path.open(mode, buffering=1)  # noqa: SIM115 — line-buffered handle held open across calls, closed by close_unified_log
    # Group-writable so other sysforge-group members (e.g. post-reboot primary
    # user) can append on subsequent invocations. Best-effort: silently skip if
    # we don't own the file (e.g. appending an existing root-owned log).
    with contextlib.suppress(OSError):
        path.chmod(0o664)
    _write_to_files(_session_header("sysforge pipeline"), raw=True)


def close_unified_log(success: bool = True, persist: bool = False) -> None:
    """
    Close the unified log. Truncates on success unless persist=True.
    """
    global _unified_log_fh
    with contextlib.suppress(Exception):
        from sysforge.ui import progress as _progress
        _progress.shutdown()
    if _unified_log_fh is None:
        return
    if success and not persist:
        _unified_log_fh.seek(0)
        _unified_log_fh.truncate()
        _unified_log_fh.write(_CLEARED_MARKER)
    _unified_log_fh.close()
    _unified_log_fh = None


def open_pkg_log(path, argv=None) -> None:
    """Open (or create) the per-package log file, appending.
    argv: if provided, written as the first line after the session header."""
    global _pkg_log_fh
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _pkg_log_fh = path.open("a", encoding="utf-8", buffering=1)  # noqa: SIM115 — line-buffered handle held open across calls, closed by close_pkg_log
    _write_to_files(_session_header(f"sysforge build {path.parent.name}"), raw=True)
    if argv:
        _write_to_files(f"# invocation: {' '.join(str(a) for a in argv)}\n", raw=True)


def close_pkg_log(success: bool = True, persist: bool = False) -> None:
    """
    Close the per-package log. Truncates on success unless persist=True.
    """
    global _pkg_log_fh
    if _pkg_log_fh is None:
        return
    if success and not persist:
        _pkg_log_fh.seek(0)
        _pkg_log_fh.truncate()
        _pkg_log_fh.write(_CLEARED_MARKER)
    _pkg_log_fh.close()
    _pkg_log_fh = None


def _write_to_files(line: str, raw: bool = False) -> None:
    """Write a line to all open file handles. raw=True skips formatting."""
    for fh in (_unified_log_fh, _pkg_log_fh):
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.write(line)  # never let file I/O break the build


# ---------------------------------------------------------------------------
# Log functions
# ---------------------------------------------------------------------------

def prompt_prefix(level: str, tag: str) -> str:
    """Return a formatted '[SYSFORGE][LEVEL][TAG] ' prefix for use in input() prompts.

    Applies the same ANSI coloring as :func:`_format_line` when the output
    stream is a colour-capable TTY so prompts match surrounding log lines.
    """
    if not use_color():
        return f"[SYSFORGE][{level}]{tag} "
    r = _ANSI_RESET
    sgr = _LEVEL_SGR.get(level, "")
    lvl_fmt = f"{sgr}{level}{r}" if sgr else level
    tag_fmt = f"{_ANSI_CYAN}{tag}{r}" if tag else ""
    return f"[SYSFORGE][{lvl_fmt}]{tag_fmt} "


# ---------------------------------------------------------------------------
# Logger — bound logger with a fixed tag
# ---------------------------------------------------------------------------

class Logger:
    """A logger bound to a fixed tag.  Create via get_logger()."""

    __slots__ = ("_tag",)

    def __init__(self, tag: str) -> None:
        self._tag = f"[{tag}]"

    def ui(self, message: str) -> None:
        ui(self._tag, message)

    def fatal(self, message: str, exit_code: int = 1) -> NoReturn:
        fatal(self._tag, message, exit_code)

    def error(self, message: str) -> None:
        error(self._tag, message)

    def warn(self, message: str) -> None:
        warn(self._tag, message)

    def info(self, message: str) -> None:
        info(self._tag, message)

    def debug(self, message: str) -> None:
        debug(self._tag, message)

    def newline(self) -> None:
        newline()

    def prompt_prefix(self, level: str) -> str:
        return prompt_prefix(level, self._tag)


def get_logger(tag: str) -> Logger:
    """Return a Logger bound to the given tag (e.g. get_logger("UPDATE"))."""
    return Logger(tag)


def newline() -> None:
    """Emit a blank line to the output stream. Use before async log messages to
    avoid mid-line appends."""
    print("", file=_out())


def ui(tag: str, message: str) -> None:
    """Always printed regardless of verbosity. Always written to log files.
    For interactive output."""
    print(downgrade_glyphs(message), file=_out())
    _write_to_files(f"[SYSFORGE][UI]{tag} {message}\n")


def fatal(tag: str, message: str, exit_code: int = 1) -> NoReturn:
    """Print an error message, write to log files, and terminate the process."""
    with contextlib.suppress(Exception):
        from sysforge.ui import progress as _progress
        _progress.shutdown()
    error(tag, message)
    sys.exit(exit_code)


def error(tag: str, message: str) -> None:
    """Always printed regardless of verbosity. Always written to log files."""
    plain = f"[SYSFORGE][ERROR]{tag} {message}\n"
    print(_format_line("ERROR", tag, message), end="", file=_out())
    _write_to_files(plain)


def warn(tag: str, message: str) -> None:
    """Printed at verbosity >= 1 (-v). Always written to log files."""
    plain = f"[SYSFORGE][WARN]{tag} {message}\n"
    if _VERBOSITY >= 1:
        print(_format_line("WARN", tag, message), end="", file=_out())
    _write_to_files(plain)


def info(tag: str, message: str) -> None:
    """Printed at verbosity >= 2 (-vv). Always written to log files."""
    plain = f"[SYSFORGE][INFO]{tag} {message}\n"
    if _VERBOSITY >= 2:
        print(_format_line("INFO", tag, message), end="", file=_out())
    _write_to_files(plain)


def debug(tag: str, message: str) -> None:
    """
    Printed at verbosity >= 3 (-vvv). Always written to log files.
    Multi-line messages are split and each line is emitted with its own prefix.
    Use for full config/profile/conf body dumps.
    """
    for part in (message.splitlines() or [""]):
        plain = f"[SYSFORGE][DEBUG]{tag} {part}\n"
        if _VERBOSITY >= 3:
            print(_format_line("DEBUG", tag, part), end="", file=_out())
        _write_to_files(plain)
