"""
test_pty_runner.py — unit tests for the pty_runner helper.

These tests spawn small bash/python subprocesses, so they require a working
shell on PATH but no sysforge state.
"""
import io
import os
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


def _child_rows_cmd() -> list[str]:
    return [
        sys.executable, "-c",
        "import os, sys; print(os.get_terminal_size(sys.stdout.fileno()).lines)",
    ]


def test_no_reserve_gives_child_full_height(tmp_path, monkeypatch):
    # With reserve_bottom_rows defaulting to 0 the child sees the full terminal.
    monkeypatch.setattr(
        "sysforge.primitives.pty_runner.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 30)),
    )
    lines: list[str] = []
    rc = run_with_pty(
        _child_rows_cmd(),
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append, forward_bytes=False,
    )
    assert rc == 0
    assert lines == ["30"]


def test_reserve_bottom_rows_shrinks_child_winsize(tmp_path, monkeypatch):
    # The progress bar reserves the bottom row; the child must be sized one row
    # shorter so its full-screen redraws never touch the bar row.
    monkeypatch.setattr(
        "sysforge.primitives.pty_runner.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 30)),
    )
    lines: list[str] = []
    rc = run_with_pty(
        _child_rows_cmd(),
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append, forward_bytes=False,
        reserve_bottom_rows=1,
    )
    assert rc == 0
    assert lines == ["29"]


def _stdin_rows_cmd() -> list[str]:
    return [
        sys.executable, "-c",
        "import os\n"
        "try:\n"
        "    print(os.get_terminal_size(0).lines)\n"
        "except OSError:\n"
        "    print('NOTTY')\n",
    ]


def test_reserve_bottom_rows_also_sizes_child_stdin(tmp_path, monkeypatch):
    # 3.2.0-B3: a winsize belongs to the tty device, not the fd, so sizing only
    # stdout leaves stdin reporting the *unreserved* height of the inherited
    # terminal. Under a reservation the child's stdin must be our pty too.
    monkeypatch.setattr(
        "sysforge.primitives.pty_runner.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 30)),
    )
    lines: list[str] = []
    rc = run_with_pty(
        _stdin_rows_cmd(),
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append, forward_bytes=False,
        reserve_bottom_rows=1,
    )
    assert rc == 0
    assert lines == ["29"]


def test_no_reserve_leaves_child_stdin_inherited(tmp_path, monkeypatch):
    # Without a reservation there is nothing to protect, so stdin keeps the
    # documented inherit-from-parent behaviour (DEVNULL under pytest capture).
    monkeypatch.setattr(
        "sysforge.primitives.pty_runner.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 30)),
    )
    lines: list[str] = []
    rc = run_with_pty(
        _stdin_rows_cmd(),
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append, forward_bytes=False,
    )
    assert rc == 0
    assert lines == ["NOTTY"]


# The sandbox shape: makechrootpkg -> sudo -> arch-nspawn -> systemd-nspawn
# --console=autopipe each allocate a *nested* pty, and that inner pty is sized
# from stdin. This child stands in for that whole stack -- it opens its own pty
# and reports the size the innermost process sees, which is the size the
# container's build output actually scrolls within.
_NESTED_PTY_CHILD = """
import os, pty, subprocess, sys
m, s = pty.openpty()
# Mirror what a pty-allocating wrapper does: seed the new pty from stdin.
try:
    import fcntl, struct, termios
    fcntl.ioctl(s, termios.TIOCSWINSZ,
                fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8))
except OSError:
    pass
p = subprocess.Popen(
    [sys.executable, "-c",
     "import os; print(os.get_terminal_size(1).lines)"],
    stdin=s, stdout=s, stderr=s, close_fds=True)
os.close(s)
p.wait()
out = b""
while True:
    try:
        c = os.read(m, 4096)
    except OSError:
        break
    if not c:
        break
    out += c
sys.stdout.write(out.decode().strip() + "\\n")
"""


def test_reservation_survives_a_nested_pty(tmp_path, monkeypatch):
    # The regression itself: with stdin inherited the nested pty was sized from
    # the real terminal, so the container scrolled over the reserved bar row and
    # the progress indicator vanished for the whole sandboxed build.
    monkeypatch.setattr(
        "sysforge.primitives.pty_runner.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 30)),
    )
    lines: list[str] = []
    rc = run_with_pty(
        [sys.executable, "-c", _NESTED_PTY_CHILD],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        line_callback=lines.append, forward_bytes=False,
        reserve_bottom_rows=1,
    )
    assert rc == 0
    assert [ln for ln in lines if ln.strip()] == ["29"]


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
        [sys.executable, "-c",
         f"import sys; sys.stdout.write({payload!r}); sys.stdout.write('\\n')"],
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


def test_strip_ansi_removes_charset_designation_escapes():
    """The exact 3.2.0-B8 corruption. makepkg's message helpers close every
    line with ``ESC ( B`` (designate ASCII into G0) beside the SGR reset. It
    is an nF-class escape — ESC, an intermediate byte, a final byte — which
    none of the OSC/CSI/two-byte-C1 branches match, so the whole sequence
    survived and the non-printing ESC left a bare ``(B`` in the output."""
    assert strip_ansi("==>\x1b(B Retrieving sources...\x1b(B") == "==> Retrieving sources..."
    assert strip_ansi("\x1b(B\x1b[m==> ERROR:\x1b(B") == "==> ERROR:"


def test_strip_ansi_removes_the_other_nf_designations():
    """G1/G2/G3 and the line-drawing set are the same escape class; a fix
    that special-cases ``ESC ( B`` alone just moves the corruption."""
    for seq in ("\x1b(0", "\x1b)B", "\x1b*A", "\x1b+B", "\x1b(1"):
        assert strip_ansi(f"a{seq}b") == "ab", f"{seq!r} survived"


def test_strip_ansi_keeps_an_ordinary_parenthesis():
    """The final byte is only special after ESC — bare text must not lose it."""
    assert strip_ansi("f(x) == (B)") == "f(x) == (B)"


def test_raw_dump_captures_pre_strip_bytes(tmp_path, monkeypatch):
    """3.2.0-B9 instrumentation. The bar's disappearance cannot be diagnosed
    from the normal log: that is written after strip_ansi, so the very
    sequences under suspicion (DECSTBM reset, RIS, alt-screen) are gone by the
    time anything is recorded. The dump is the raw child stream, untouched."""
    dump = tmp_path / "raw.bin"
    monkeypatch.setenv("SYSFORGE_PTY_RAW_DUMP", str(dump))
    lines = []
    rc = run_with_pty(
        ["printf", "a\\033[1;40rb\\033(Bc\\n"],
        cwd=tmp_path, env=dict(os.environ), line_callback=lines.append,
        forward_bytes=False,
    )
    assert rc == 0
    raw = dump.read_bytes()
    assert b"\x1b[1;40r" in raw, f"escape did not survive into the dump: {raw!r}"
    assert b"\x1b(B" in raw
    # The callback is unaffected: run_with_pty relays verbatim and stripping
    # is the caller's job, which is exactly why the dump has to sit here.
    assert lines == ["a\x1b[1;40rb\x1b(Bc"]


def test_raw_dump_records_the_negotiated_winsize(tmp_path, monkeypatch):
    """The other half of the B9 question: whether the reservation actually
    reached the pty, or was lost across the nested-pty layer."""
    dump = tmp_path / "raw.bin"
    monkeypatch.setenv("SYSFORGE_PTY_RAW_DUMP", str(dump))
    run_with_pty(
        ["true"], cwd=tmp_path, env=dict(os.environ),
        line_callback=lambda _l: None, forward_bytes=False,
        reserve_bottom_rows=1,
    )
    header = dump.read_bytes().split(b"\n", 1)[0]
    assert b"winsize" in header and b"reserve=1" in header, header


def test_raw_dump_is_off_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSFORGE_PTY_RAW_DUMP", raising=False)
    run_with_pty(["true"], cwd=tmp_path, env=dict(os.environ),
                 line_callback=lambda _l: None, forward_bytes=False)
    assert list(tmp_path.iterdir()) == []


def test_raw_dump_failure_never_breaks_the_build(tmp_path, monkeypatch):
    """Diagnostics are best-effort: an unwritable path must not take the
    build down with it."""
    monkeypatch.setenv("SYSFORGE_PTY_RAW_DUMP", str(tmp_path / "nope" / "x.bin"))
    lines = []
    rc = run_with_pty(["printf", "ok\\n"], cwd=tmp_path, env=dict(os.environ),
                      line_callback=lines.append, forward_bytes=False)
    assert rc == 0 and lines == ["ok"]
