"""
test_pty_runner.py — unit tests for the pty_runner helper.

These tests spawn small bash/python subprocesses, so they require a working
shell on PATH but no sysforge state.
"""
import io
import sys
import threading
from types import SimpleNamespace

from sysforge.primitives.pty_runner import run_with_pty, strip_ansi


def _capture_stdout_buffer(monkeypatch) -> io.BytesIO:
    """Redirect sys.stdout to an object whose .buffer is an in-memory sink."""
    sink = io.BytesIO()
    fake_stdout = SimpleNamespace(buffer=sink, isatty=lambda: True)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    return sink


def test_emits_lines_on_newline(tmp_path):
    lines: list[str] = []
    rc = run_with_pty(
        ["bash", "-c", "printf 'a\\nb\\nc\\n'"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    assert lines == ["a", "b", "c"]


def test_preserves_cr_for_progress_bar_bytes(tmp_path, monkeypatch):
    sink = _capture_stdout_buffer(monkeypatch)
    lines: list[str] = []
    rc = run_with_pty(
        ["bash", "-c", "printf 'x\\rprog 1/3\\rprog 2/3\\nDone\\n'"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=True,
    )
    assert rc == 0
    # Bytes forwarded verbatim include the \r redraws.
    forwarded = sink.getvalue()
    assert b"\r" in forwarded
    assert b"prog 1/3" in forwarded
    assert b"prog 2/3" in forwarded
    # Line callback receives whatever was between newlines, minus trailing \r;
    # intermediate \r-separated segments stay in the string (terminal display
    # semantics don't apply to a captured byte stream — patterns matched via
    # `in` still find their needles regardless of intermediate \r).
    assert lines == ["x\rprog 1/3\rprog 2/3", "Done"]


def test_flushes_trailing_partial_line(tmp_path):
    lines: list[str] = []
    rc = run_with_pty(
        ["bash", "-c", "printf 'final-no-newline'"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    assert lines == ["final-no-newline"]


def test_isatty_in_child(tmp_path):
    lines: list[str] = []
    rc = run_with_pty(
        [sys.executable, "-c", "import sys; print(sys.stdout.isatty())"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    assert lines == ["True"]


def test_returns_nonzero(tmp_path):
    rc = run_with_pty(
        ["bash", "-c", "exit 7"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lambda _line: None,
        forward_bytes=False,
    )
    assert rc == 7


def test_forward_bytes_false_does_not_write_stdout(tmp_path, monkeypatch):
    sink = _capture_stdout_buffer(monkeypatch)
    lines: list[str] = []
    rc = run_with_pty(
        ["bash", "-c", "printf 'a\\rb\\rc\\nDone\\n'"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    assert sink.getvalue() == b""
    assert lines == ["a\rb\rc", "Done"]


def test_off_main_thread_no_signal_install(tmp_path):
    """SIGWINCH install is main-thread-only; off-thread calls must still work."""
    result: dict = {}

    def _worker():
        lines: list[str] = []
        try:
            result["rc"] = run_with_pty(
                ["bash", "-c", "printf 'thread\\n'"],
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
                line_callback=lines.append,
                forward_bytes=False,
            )
            result["lines"] = lines
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=10)
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result["rc"] == 0
    assert result["lines"] == ["thread"]


def test_utf8_split_across_chunks(tmp_path):
    """Multi-byte codepoint at a chunk boundary must not crash decoding."""
    lines: list[str] = []
    # Build a long line that crosses the 4096-byte read boundary at a
    # multi-byte codepoint. 4094 ASCII 'a's + 'é' (2 bytes UTF-8) + \n
    # means the byte at offset 4095 (the second byte of é) is in chunk 2.
    payload = "a" * 4094 + "é"
    rc = run_with_pty(
        [sys.executable, "-c", f"import sys; sys.stdout.write({payload!r}); sys.stdout.write('\\n')"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    assert lines == [payload]


def test_idle_callback_flushes_latest_cr_segment(tmp_path):
    """Mimics ninja: emit a few \\r-redrawn status lines, then go quiet."""
    idle: list[str | None] = []
    rc = run_with_pty(
        # printf flushes by default; sleep keeps the pty open while idle.
        ["bash", "-c", "printf 'a\\rb\\rc'; sleep 0.4"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lambda _line: None,
        forward_bytes=False,
        idle_callback=idle.append,
        idle_timeout_s=0.05,
    )
    assert rc == 0
    # At least one heartbeat fired during the sleep with the latest
    # \r-overwritten segment ("c"), not the full redraw history.
    assert "c" in idle
    # Every heartbeat is either the latest segment "c" or None (if it fired
    # before any data arrived); we never leak the redraw history.
    assert all(s in (None, "c") for s in idle)


def test_idle_callback_fires_with_none_on_silence(tmp_path):
    """When the child produces no output, idle_callback gets None."""
    idle: list[str | None] = []
    rc = run_with_pty(
        ["bash", "-c", "sleep 0.3"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        line_callback=lambda _line: None,
        forward_bytes=False,
        idle_callback=idle.append,
        idle_timeout_s=0.05,
    )
    assert rc == 0
    assert None in idle


def test_strip_ansi_removes_osc8_hyperlinks_inside_tokens():
    """GCC >= 16 wraps quoted option names in OSC-8 hyperlinks under a pty;
    the visible token must survive contiguously for substring matching."""
    line = (
        "cc1plus: error: unrecognized argument to ‘\x1b[K"
        "\x1b]8;;https://gcc.gnu.org/onlinedocs/gcc-16.1.0/gcc/"
        "Optimize-Options.html#index-flto\x1b\\-flto=\x1b]8;;\x1b\\"
        "\x1b[K’ option: ‘\x1b[Kthin\x1b[K’"
    )
    assert strip_ansi(line) == (
        "cc1plus: error: unrecognized argument to "
        "‘-flto=’ option: ‘thin’"
    )


def test_strip_ansi_removes_csi_and_plain_text_passthrough():
    # SGR colors + erase-line, as makepkg emits them.
    assert strip_ansi("\x1b[1m\x1b[31m==> ERROR:\x1b[0m done") == "==> ERROR: done"
    # Text without escapes is returned unchanged.
    assert strip_ansi("plain text 'quoted'") == "plain text 'quoted'"


def test_strip_ansi_tolerates_unterminated_osc():
    # An OSC split across read chunks can reach the matcher unterminated;
    # stripping must not swallow the rest of the line or raise.
    assert strip_ansi("before \x1b]8;;https://example.com") == "before "
