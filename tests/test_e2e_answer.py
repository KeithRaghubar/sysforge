"""
test_e2e_answer.py — what tools/e2e/answer.py lets through to the terminal (4.0.0-DEV3).

answer.py runs a scenario's command on a pty that never had a window size, so
sysforge falls back to 80x24 and its progress bar sets a scroll region for
rows 1-23 and paints row 24. The raw stream reached the terminal running
`make e2e` through `podman exec`: text overlapped on row 24 there, whatever
that terminal's real height. The filter keeps the text and drops what
places it.
"""
import importlib.util
import subprocess  # noqa: TID251 — drives answer.py on a real pty
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "e2e" / "answer.py"


def _load():
    spec = importlib.util.spec_from_file_location("e2e_answer", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


answer = _load()

# What ui/progress.py emits on a 24-row terminal: _establish_seq, a bar draw,
# and _release_seq.
ESTABLISH = b"\x1bD\x1b[1A\x1b7\x1b[1;23r\x1b8"
PAINT = b"\x1b7\x1b[24;1H\x1b[2K[building] cbonsai 3/10\x1b8"
RELEASE = b"\x1b7\x1b[r\x1b[24;1H\x1b[2K\x1b8"


def _filter(*chunks: bytes) -> bytes:
    f = answer.TerminalFilter()
    return b"".join(f.feed(c) for c in chunks) + f.finish()


def test_scroll_region_and_bar_paint_never_reach_the_terminal():
    out = _filter(ESTABLISH, b"line one\n", PAINT, b"line two\n", RELEASE)
    assert out == b"line one\nline two\n"


def test_colour_carriage_return_and_erase_line_survive():
    """Position-independent: they act on the line the text is already on."""
    text = b"\x1b[1;32mok\x1b[0m 1/3\r\x1b[Kok 2/3\r\x1b[0Kdone\n"
    assert _filter(text) == text


def test_a_sequence_split_across_reads_is_still_removed():
    region = b"\x1b[1;23r"
    out = _filter(b"before" + region[:4], region[4:] + b"after\n")
    assert out == b"beforeafter\n"


def test_a_paint_split_across_reads_is_still_discarded():
    out = _filter(b"a\n" + PAINT[:12], PAINT[12:] + b"b\n")
    assert out == b"a\nb\n"


def test_an_unrestored_save_does_not_swallow_output():
    """A bar paint is one row: a newline inside the bracket means it was not
    one, so the text is kept."""
    out = _filter(b"\x1b7", b"kept\n", b"also kept\n")
    assert out == b"kept\nalso kept\n"


def test_an_unrestored_save_at_the_end_keeps_its_text():
    assert _filter(b"\x1b7tail") == b"tail"


def test_alternate_screen_title_and_cursor_visibility_are_dropped():
    out = _filter(b"\x1b[?1049h\x1b]0;sysforge\x07\x1b[?25lshown\x1b[?25h\n")
    assert out == b"shown\n"


def test_answer_py_filters_a_real_pty_stream_and_still_answers():
    """The prompt is still matched and answered; only the placement is gone."""
    child = ("import sys;"
             "sys.stdout.write('\\x1b[1;23r\\x1b7\\x1b[24;1Hbar\\x1b8Go? ');"
             "sys.stdout.flush();"
             "print('got', input(), end='\\x1b[r\\n')")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), r"Go\? ", r"yes\n", "--", sys.executable, "-c", child],
        capture_output=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert b"\x1b[" not in proc.stdout and b"\x1b7" not in proc.stdout
    assert b"bar" not in proc.stdout
    assert b"Go? " in proc.stdout and b"got yes" in proc.stdout
