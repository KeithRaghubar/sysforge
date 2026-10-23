"""
test_terminal_model.py — the progress bar and update's terminal reset, replayed
through a terminal model under both saved-cursor rules (3.5.0-DEV22).

``test_progress.py`` checks the sequences the bar writes; these check what they
do to the screen: the command line survives, output follows it in order, the
bar leaves nothing behind and the next prompt lands after the last line, under
xterm's and xterm.js's rules for a saved cursor (``tests/term_model.py``). The
``*_pre_fix_*`` tests replay the sequences 3.5.0-B132 and B133 replaced, to
show the model sees both bugs.
"""
import os
import shutil
import sys

import pytest

from sysforge import log
from sysforge.ui import progress
from sysforge.update import _TERMINAL_RESET
from term_model import FLAVORS, Terminal, UnknownSequence

ROWS, COLS = 10, 50
COMMAND = "$ sysforge build cbonsai"


class _TermStream:
    """A terminal stderr: isatty() is true and every write reaches the model."""

    def __init__(self, term: Terminal) -> None:
        self._term = term

    def isatty(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._term.feed(s)
        return len(s)

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _tty_env(monkeypatch):
    monkeypatch.setattr(log, "_DRY_RUN", False)
    for var in ("CI", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(shutil, "get_terminal_size",
                        lambda fallback=None: os.terminal_size((COLS, ROWS)))
    yield
    progress.shutdown()


@pytest.fixture(params=FLAVORS)
def term(request) -> Terminal:
    return Terminal(ROWS, COLS, flavor=request.param)


def _shell(term: Terminal, earlier: int) -> list[str]:
    """``earlier`` lines of earlier output, then the command typed and Enter
    pressed; returns what the transcript holds so far."""
    before = [f"earlier {n}" for n in range(1, earlier + 1)]
    for line in before:
        term.feed(line + "\n")
    term.feed(COMMAND + "\n")
    return [*before, COMMAND]


def _run(term: Terminal, output: list[str]) -> None:
    """A command's run: the bar comes up, output scrolls past while the bar
    repaints, and the bar is released.

    stderr is bound here, in the test's own phase: pytest's capture puts its
    own back between a fixture's setup and the test body."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "stderr", _TermStream(term))
        progress.init()
        assert progress._bar.mode == "tty"
        progress.phase("syncing sources")
        for n, line in enumerate(output):
            term.feed(line + "\n")
            if n % 3 == 0:
                progress.phase(f"building {n}")   # a repaint saves the cursor
        progress.shutdown()


def _no_bar_left(term: Terminal) -> None:
    assert not any("PROGRESS" in line for line in term.transcript())


# --- B133: the first line of output and the command line ----------------------

def test_output_follows_a_command_mid_screen(term):
    seen = _shell(term, earlier=2)
    _run(term, ["line 1", "line 2"])
    assert term.transcript() == [*seen, "line 1", "line 2"]
    assert term.scrollback == []   # free rows below: nothing pushed off the top
    _no_bar_left(term)


def test_output_follows_a_command_on_the_bottom_row(term):
    seen = _shell(term, earlier=ROWS - 1)   # Enter on the bottom row scrolls
    _run(term, ["line 1", "line 2"])
    assert term.transcript() == [*seen, "line 1", "line 2"]
    _no_bar_left(term)


def test_long_output_scrolls_in_order_past_the_bar(term):
    seen = _shell(term, earlier=4)
    output = [f"line {n}" for n in range(1, 3 * ROWS)]
    _run(term, output)
    assert term.transcript() == [*seen, *output]
    _no_bar_left(term)


def test_establish_pre_fix_overwrites_the_command_on_xterm_js(term, monkeypatch):
    """The establish sequence B133 replaced (save, jump to the bottom, scroll,
    set region, restore, up one): fine on xterm, one line high on xterm.js."""
    def pre_fix() -> str:
        if progress._bar.reserved:
            return ""
        progress._refresh_size()
        rows = progress._bar.rows
        progress._bar.reserved = True
        e = progress._ESC
        return (f"{progress._SAVE}{e}[{rows};1H{progress._INDEX}{e}[1;{rows - 1}r"
                f"{progress._RESTORE}{e}[1A")

    monkeypatch.setattr(progress, "_establish_seq", pre_fix)
    seen = _shell(term, earlier=ROWS - 1)
    _run(term, ["line 1", "line 2"])
    if term.flavor == "xterm.js":
        assert COMMAND not in term.transcript()
    else:
        assert term.transcript() == [*seen, "line 1", "line 2"]


# --- B132: the end-of-run reset and the next prompt ---------------------------

def _summary_then_prompt(term: Terminal, reset: str) -> list[str]:
    """update's ending: the summary printed after the bar is released, the
    terminal reset, then the shell's next prompt."""
    summary = ["Update complete: 1 built", "  cbonsai: 1.4.2-1"]
    for line in summary:
        term.feed(line + "\n")
    term.feed(reset)
    term.feed("$ next")
    return summary


def test_next_prompt_lands_after_the_summary(term):
    seen = _shell(term, earlier=3)
    output = [f"line {n}" for n in range(1, 6)]
    _run(term, output)
    summary = _summary_then_prompt(term, _TERMINAL_RESET)
    assert term.transcript() == [*seen, *output, *summary, "$ next"]


def test_reset_pre_fix_sends_the_prompt_back_over_the_output(term):
    """B132's reset, without the DECSC ahead of ?1049l, restores the bar's
    last saved cursor on both terminals."""
    seen = _shell(term, earlier=3)
    output = [f"line {n}" for n in range(1, 6)]
    _run(term, output)
    summary = _summary_then_prompt(term, "\x1b[?1049l\x1b[?25h\x1b[0m")
    assert term.transcript() != [*seen, *output, *summary, "$ next"]


def test_reset_after_a_killed_pager_returns_to_the_normal_screen(term):
    """A pager killed on the alternate screen: the reset leaves it, and the
    cursor goes back to where the pager started."""
    seen = _shell(term, earlier=2)
    term.feed("\x1b[?1049h" + "diff --git a/PKGBUILD b/PKGBUILD\n+pkgver=2\n")
    term.feed(_TERMINAL_RESET)
    term.feed("$ next")
    assert not term.alt_active
    assert term.transcript() == [*seen, "$ next"]


# --- the model itself --------------------------------------------------------

def test_model_refuses_a_sequence_it_does_not_know():
    with pytest.raises(UnknownSequence):
        Terminal().feed("\x1b[5S")   # scroll up: not modelled


@pytest.mark.parametrize("flavor", FLAVORS)
def test_saved_cursor_across_a_scroll(flavor):
    t = Terminal(3, 10, flavor=flavor)
    t.feed("a\nb\nc")      # cursor on the bottom row, on "c"
    t.feed("\x1b7\n\x1b8")  # save, scroll one, restore
    assert t.lines() == ["b", "c", ""]
    assert t.cursor == ((2, 1) if flavor == "xterm" else (1, 1))
