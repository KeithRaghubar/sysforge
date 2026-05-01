"""
test_log.py — unit tests for sysforge.log structured logging.

Covers:
    set_verbosity / get_verbosity   — clamping, get/set round-trip
    error / warn / info / debug     — verbosity gating on stderr,
                                      unconditional write to log files,
                                      correct [SYSFORGE][LEVEL][TAG] format
    debug                           — multiline split, empty string
    open/close_unified_log          — file creation, parent mkdir, session header,
                                      purge mode, success+persist combos
    open/close_pkg_log              — same lifecycle, append mode
    _write_to_files                 — both handles written simultaneously,
                                      no crash when handle is None
    simultaneous unified + pkg log  — message written to both files
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sysforge.log as log


# ---------------------------------------------------------------------------
# Fixture: reset module-level state around every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _log_reset():
    """Close any open handles and restore verbosity after each test."""
    saved = log.get_verbosity()
    yield
    # Close handles defensively (tests may leave them open on failure)
    if log._unified_log_fh is not None:
        try:
            log._unified_log_fh.close()
        except Exception:
            pass
        log._unified_log_fh = None
    if log._pkg_log_fh is not None:
        try:
            log._pkg_log_fh.close()
        except Exception:
            pass
        log._pkg_log_fh = None
    log.set_verbosity(saved)


# ---------------------------------------------------------------------------
# set_verbosity / get_verbosity
# ---------------------------------------------------------------------------

def test_set_get_verbosity():
    log.set_verbosity(2)
    assert log.get_verbosity() == 2


def test_set_verbosity_clamps_negative():
    log.set_verbosity(-5)
    assert log.get_verbosity() == 0


def test_set_verbosity_zero():
    log.set_verbosity(0)
    assert log.get_verbosity() == 0


def test_set_verbosity_high():
    log.set_verbosity(3)
    assert log.get_verbosity() == 3


# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------

def test_error_format(capsys):
    log.set_verbosity(0)
    log.error("[TAG]", "something broke")
    out = capsys.readouterr().err
    assert out == "[SYSFORGE][ERROR][TAG] something broke\n"


def test_warn_format(capsys):
    log.set_verbosity(1)
    log.warn("[TAG]", "careful")
    out = capsys.readouterr().err
    assert out == "[SYSFORGE][WARN][TAG] careful\n"


def test_info_format(capsys):
    log.set_verbosity(2)
    log.info("[TAG]", "hello")
    out = capsys.readouterr().err
    assert out == "[SYSFORGE][INFO][TAG] hello\n"


def test_debug_format(capsys):
    log.set_verbosity(3)
    log.debug("[TAG]", "verbose")
    out = capsys.readouterr().err
    assert out == "[SYSFORGE][DEBUG][TAG] verbose\n"


# ---------------------------------------------------------------------------
# Verbosity gating on stderr
# ---------------------------------------------------------------------------

def test_error_always_shown(capsys):
    log.set_verbosity(0)
    log.error("[X]", "msg")
    assert "[SYSFORGE][ERROR]" in capsys.readouterr().err


def test_warn_suppressed_at_v0(capsys):
    log.set_verbosity(0)
    log.warn("[X]", "msg")
    assert capsys.readouterr().err == ""


def test_warn_shown_at_v1(capsys):
    log.set_verbosity(1)
    log.warn("[X]", "msg")
    assert "[SYSFORGE][WARN]" in capsys.readouterr().err


def test_info_suppressed_at_v1(capsys):
    log.set_verbosity(1)
    log.info("[X]", "msg")
    assert capsys.readouterr().err == ""


def test_info_shown_at_v2(capsys):
    log.set_verbosity(2)
    log.info("[X]", "msg")
    assert "[SYSFORGE][INFO]" in capsys.readouterr().err


def test_debug_suppressed_at_v2(capsys):
    log.set_verbosity(2)
    log.debug("[X]", "msg")
    assert capsys.readouterr().err == ""


def test_debug_shown_at_v3(capsys):
    log.set_verbosity(3)
    log.debug("[X]", "msg")
    assert "[SYSFORGE][DEBUG]" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# debug — multiline splitting
# ---------------------------------------------------------------------------

def test_debug_multiline_splits_lines(capsys):
    log.set_verbosity(3)
    log.debug("[X]", "line1\nline2\nline3")
    out = capsys.readouterr().err
    lines = out.splitlines()
    assert len(lines) == 3
    assert all("[SYSFORGE][DEBUG][X]" in ln for ln in lines)
    assert "line1" in lines[0]
    assert "line2" in lines[1]
    assert "line3" in lines[2]


def test_debug_empty_string(capsys):
    log.set_verbosity(3)
    log.debug("[X]", "")
    out = capsys.readouterr().err
    assert "[SYSFORGE][DEBUG][X] \n" == out


# ---------------------------------------------------------------------------
# open / close unified log
# ---------------------------------------------------------------------------

def test_open_unified_log_creates_file(tmp_path):
    path = tmp_path / "sub" / "sysforge.log"
    log.open_unified_log(path)
    assert path.exists()
    log.close_unified_log(success=False, persist=True)


def test_open_unified_log_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "sysforge.log"
    log.open_unified_log(path)
    assert path.parent.is_dir()
    log.close_unified_log(success=False, persist=True)


def test_open_unified_log_writes_session_header(tmp_path):
    path = tmp_path / "sysforge.log"
    log.open_unified_log(path)
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert "sysforge pipeline" in content
    assert "─" in content  # separator line


def test_open_unified_log_appends_by_default(tmp_path):
    path = tmp_path / "sysforge.log"
    path.write_text("existing\n")
    log.open_unified_log(path, purge=False)
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert content.startswith("existing\n")


def test_open_unified_log_purge_truncates(tmp_path):
    path = tmp_path / "sysforge.log"
    path.write_text("old content\n")
    log.open_unified_log(path, purge=True)
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert "old content" not in content


def test_close_unified_log_success_no_persist_leaves_marker(tmp_path):
    path = tmp_path / "sysforge.log"
    log.open_unified_log(path)
    log.info("[X]", "some message")
    log.close_unified_log(success=True, persist=False)
    content = path.read_text()
    assert content == log._CLEARED_MARKER


def test_close_unified_log_success_persist_keeps_content(tmp_path):
    path = tmp_path / "sysforge.log"
    log.set_verbosity(2)
    log.open_unified_log(path)
    log.info("[X]", "important")
    log.close_unified_log(success=True, persist=True)
    content = path.read_text()
    assert "important" in content


def test_close_unified_log_failure_keeps_content(tmp_path):
    path = tmp_path / "sysforge.log"
    log.set_verbosity(2)
    log.open_unified_log(path)
    log.info("[X]", "debug info")
    log.close_unified_log(success=False, persist=False)
    content = path.read_text()
    assert "debug info" in content


def test_close_unified_log_noop_when_not_open():
    # Should not raise
    log.close_unified_log(success=True)
    log.close_unified_log(success=False)


def test_unified_log_handle_none_after_close(tmp_path):
    path = tmp_path / "sysforge.log"
    log.open_unified_log(path)
    log.close_unified_log(success=False, persist=True)
    assert log._unified_log_fh is None


# ---------------------------------------------------------------------------
# open / close per-package log
# ---------------------------------------------------------------------------

def test_open_pkg_log_creates_file(tmp_path):
    path = tmp_path / "mypkg" / "sysforge_mypkg.log"
    log.open_pkg_log(path)
    assert path.exists()
    log.close_pkg_log(success=False, persist=True)


def test_open_pkg_log_appends(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    path.parent.mkdir()
    path.write_text("prior\n")
    log.open_pkg_log(path)
    log.close_pkg_log(success=False, persist=True)
    content = path.read_text()
    assert content.startswith("prior\n")


def test_open_pkg_log_writes_session_header(tmp_path):
    path = tmp_path / "mypkg" / "sysforge_mypkg.log"
    log.open_pkg_log(path)
    log.close_pkg_log(success=False, persist=True)
    content = path.read_text()
    assert "sysforge build" in content


def test_close_pkg_log_success_no_persist_leaves_marker(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    log.open_pkg_log(path)
    log.close_pkg_log(success=True, persist=False)
    assert path.read_text() == log._CLEARED_MARKER


def test_close_pkg_log_success_persist_keeps_content(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    log.set_verbosity(2)
    log.open_pkg_log(path)
    log.info("[X]", "keepme")
    log.close_pkg_log(success=True, persist=True)
    assert "keepme" in path.read_text()


def test_close_pkg_log_failure_keeps_content(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    log.set_verbosity(2)
    log.open_pkg_log(path)
    log.info("[X]", "diag")
    log.close_pkg_log(success=False, persist=False)
    assert "diag" in path.read_text()


def test_close_pkg_log_noop_when_not_open():
    log.close_pkg_log(success=True)
    log.close_pkg_log(success=False)


# ---------------------------------------------------------------------------
# Messages written to log files (always, regardless of verbosity)
# ---------------------------------------------------------------------------

def test_messages_written_to_unified_log_at_any_verbosity(tmp_path):
    path = tmp_path / "sysforge.log"
    log.set_verbosity(0)   # only errors on stderr
    log.open_unified_log(path)
    log.warn("[X]", "warnmsg")
    log.info("[X]", "infomsg")
    log.debug("[X]", "debugmsg")
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert "warnmsg" in content
    assert "infomsg" in content
    assert "debugmsg" in content


def test_messages_written_to_pkg_log_at_any_verbosity(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    log.set_verbosity(0)
    log.open_pkg_log(path)
    log.debug("[X]", "deepdebug")
    log.close_pkg_log(success=False, persist=True)
    assert "deepdebug" in path.read_text()


# ---------------------------------------------------------------------------
# Simultaneous unified + per-package log
# ---------------------------------------------------------------------------

def test_message_written_to_both_logs(tmp_path):
    upath = tmp_path / "sysforge.log"
    ppath = tmp_path / "pkg" / "sysforge_pkg.log"
    log.set_verbosity(0)
    log.open_unified_log(upath)
    log.open_pkg_log(ppath)
    log.error("[X]", "bothsides")
    log.close_unified_log(success=False, persist=True)
    log.close_pkg_log(success=False, persist=True)
    assert "bothsides" in upath.read_text()
    assert "bothsides" in ppath.read_text()


# ---------------------------------------------------------------------------
# Logger class and get_logger()
# ---------------------------------------------------------------------------

def test_get_logger_returns_logger_instance():
    logger = log.get_logger("UPDATE")
    assert isinstance(logger, log.Logger)


def test_get_logger_stores_tag():
    assert log.get_logger("UPDATE")._tag == "[UPDATE]"


def test_get_logger_two_distinct_loggers_have_distinct_tags():
    assert log.get_logger("CONF")._tag != log.get_logger("BUILD")._tag


# Format — each level

def test_logger_error_format(capsys):
    log.set_verbosity(0)
    log.get_logger("UPDATE").error("something broke")
    assert capsys.readouterr().err == "[SYSFORGE][ERROR][UPDATE] something broke\n"


def test_logger_warn_format(capsys):
    log.set_verbosity(1)
    log.get_logger("UPDATE").warn("careful")
    assert capsys.readouterr().err == "[SYSFORGE][WARN][UPDATE] careful\n"


def test_logger_info_format(capsys):
    log.set_verbosity(2)
    log.get_logger("UPDATE").info("hello")
    assert capsys.readouterr().err == "[SYSFORGE][INFO][UPDATE] hello\n"


def test_logger_debug_format(capsys):
    log.set_verbosity(3)
    log.get_logger("UPDATE").debug("verbose")
    assert capsys.readouterr().err == "[SYSFORGE][DEBUG][UPDATE] verbose\n"


def test_logger_ui_format(capsys):
    log.set_verbosity(0)
    log.get_logger("UPDATE").ui("status message")
    assert capsys.readouterr().err == "status message\n"


# Verbosity gating via Logger

def test_logger_warn_suppressed_at_v0(capsys):
    log.set_verbosity(0)
    log.get_logger("X").warn("msg")
    assert capsys.readouterr().err == ""


def test_logger_info_suppressed_at_v1(capsys):
    log.set_verbosity(1)
    log.get_logger("X").info("msg")
    assert capsys.readouterr().err == ""


def test_logger_debug_suppressed_at_v2(capsys):
    log.set_verbosity(2)
    log.get_logger("X").debug("msg")
    assert capsys.readouterr().err == ""


def test_logger_ui_always_shown_at_v0(capsys):
    log.set_verbosity(0)
    log.get_logger("X").ui("always")
    assert "always" in capsys.readouterr().err


def test_logger_error_always_shown_at_v0(capsys):
    log.set_verbosity(0)
    log.get_logger("X").error("err")
    assert "[SYSFORGE][ERROR]" in capsys.readouterr().err


# Logger writes to log files (always, regardless of verbosity)

def test_logger_info_written_to_unified_log_at_v0(tmp_path):
    path = tmp_path / "sysforge.log"
    log.set_verbosity(0)
    log.open_unified_log(path)
    log.get_logger("UPDATE").info("logged at v0")
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert "[UPDATE]" in content
    assert "logged at v0" in content


def test_logger_warn_written_to_pkg_log_at_v0(tmp_path):
    path = tmp_path / "pkg" / "sysforge_pkg.log"
    log.set_verbosity(0)
    log.open_pkg_log(path)
    log.get_logger("CONF").warn("conf issue")
    log.close_pkg_log(success=False, persist=True)
    content = path.read_text()
    assert "[CONF]" in content
    assert "conf issue" in content


# Logger.prompt_prefix

def test_logger_prompt_prefix_error():
    assert log.get_logger("UPDATE").prompt_prefix("ERROR") == "[SYSFORGE][ERROR][UPDATE] "


def test_logger_prompt_prefix_warn():
    assert log.get_logger("CONF").prompt_prefix("WARN") == "[SYSFORGE][WARN][CONF] "


# Logger.debug — multiline splitting

def test_logger_debug_multiline(capsys):
    log.set_verbosity(3)
    log.get_logger("CONF").debug("line1\nline2\nline3")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    assert all("[SYSFORGE][DEBUG][CONF]" in ln for ln in lines)


# Multiple loggers are independent

def test_multiple_loggers_independent(capsys):
    log.set_verbosity(2)
    log.get_logger("CONF").info("conf msg")
    log.get_logger("BUILD").info("build msg")
    out = capsys.readouterr().err
    assert "[SYSFORGE][INFO][CONF] conf msg" in out
    assert "[SYSFORGE][INFO][BUILD] build msg" in out


# ---------------------------------------------------------------------------
# Colour — TTY + NO_COLOR gating
# ---------------------------------------------------------------------------

class _FakeTTY:
    """Stand-in stream that reports isatty()=True. Discards writes."""
    def isatty(self): return True
    def write(self, _): pass
    def flush(self): pass


def test_use_color_false_under_capsys():
    # Pytest captures stderr via a non-TTY buffer, so colour is off by default.
    assert log.use_color() is False


def test_use_color_false_when_no_color_set(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    assert log.use_color() is False


def test_use_color_false_when_no_color_empty_string(monkeypatch):
    # NO_COLOR standard: empty value does NOT disable; only non-empty does.
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    assert log.use_color() is True


def test_use_color_true_on_tty_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    assert log.use_color() is True


def test_format_line_plain_when_color_disabled(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert log._format_line("ERROR", "[TAG]", "boom") == "[SYSFORGE][ERROR][TAG] boom\n"


def test_format_line_colored_when_color_enabled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    line = log._format_line("ERROR", "[TAG]", "boom")
    # Bold + red wraps ERROR; cyan wraps tag; reset after each.
    assert log._ANSI_RED in line and log._ANSI_BOLD in line
    assert log._ANSI_CYAN in line
    assert line.endswith("boom\n")
    assert "ERROR" in line and "[TAG]" in line


def test_format_line_warn_uses_yellow(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    line = log._format_line("WARN", "[UPDATE]", "careful")
    assert log._ANSI_YELLOW in line


def test_format_line_debug_uses_dim(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    line = log._format_line("DEBUG", "[X]", "deep")
    assert log._ANSI_DIM in line


def test_stream_output_plain_under_capsys(capsys):
    # Regression: capsys captures non-TTY, so existing exact-match assertions
    # around the rest of this file must keep working.
    log.set_verbosity(0)
    log.error("[TAG]", "boom")
    out = capsys.readouterr().err
    assert out == "[SYSFORGE][ERROR][TAG] boom\n"
    assert "\033[" not in out


def test_file_log_has_no_ansi_even_on_tty(tmp_path, monkeypatch):
    # Even if the current output stream is a TTY, log files must stay plain.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(log, "_out", lambda: _FakeTTY())
    path = tmp_path / "sysforge.log"
    log.set_verbosity(2)
    log.open_unified_log(path)
    log.error("[TAG]", "redline")
    log.warn("[TAG]", "yellowline")
    log.info("[TAG]", "plainline")
    log.close_unified_log(success=False, persist=True)
    content = path.read_text()
    assert "redline" in content
    assert "yellowline" in content
    assert "plainline" in content
    assert "\033[" not in content
