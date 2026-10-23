"""
term_model.py — a small terminal model for testing what sysforge's output does
to a real screen (3.5.0-DEV22).

The progress bar's tests check the escape sequences it writes; this checks
where they leave the text and the cursor. ``Terminal.feed`` interprets the
subset of VT100/xterm control sequences sysforge emits and raises on any other,
so a new sequence cannot be silently misread: teach the model it first.

Two flavours differ where real terminals do, in the rule both 3.5.0-B132 and
B133 turned on:

- ``"xterm"``: a saved cursor (DECSC, ``ESC 7``) keeps its screen row across a
  scroll.
- ``"xterm.js"`` (VS Code's terminal, vhs): a saved cursor follows its content
  line, so a scroll of the region holding it moves it up with the text.

Both restore the saved cursor on ``CSI ?1049l`` even when the alternate screen
was never entered, and keep one saved cursor per screen buffer.

Lines a scroll pushes off a region whose top is the first row go to
``scrollback``, as in both. Newlines are fed as the terminal receives them
from a tty with ONLCR (``\\n`` → CR LF), the default.
"""
from __future__ import annotations

import re

FLAVORS = ("xterm", "xterm.js")

_CSI_RE = re.compile(r"\[([0-?]*)([ -/]*)([@-~])")


class UnknownSequence(AssertionError):
    """An escape sequence the model does not interpret."""


class Terminal:
    def __init__(self, rows: int = 10, cols: int = 40, *, flavor: str = "xterm") -> None:
        if flavor not in FLAVORS:
            raise ValueError(f"flavor must be one of {FLAVORS}")
        self.rows, self.cols, self.flavor = rows, cols, flavor
        self.scrollback: list[str] = []
        self._normal = self._blank_screen()
        self._alt = self._blank_screen()
        self.alt_active = False
        self.row = self.col = 0
        self._wrap_pending = False
        self.top, self.bottom = 0, rows - 1
        # One saved cursor per buffer: normal, alternate.
        self._saved: dict[bool, tuple[int, int] | None] = {False: None, True: None}

    # -- the screen ---------------------------------------------------------

    def _blank_screen(self) -> list[list[str]]:
        return [[" "] * self.cols for _ in range(self.rows)]

    @property
    def _screen(self) -> list[list[str]]:
        return self._alt if self.alt_active else self._normal

    def line(self, row: int) -> str:
        """Row ``row`` of the visible screen, trailing blanks stripped."""
        return "".join(self._screen[row]).rstrip()

    def lines(self) -> list[str]:
        return [self.line(r) for r in range(self.rows)]

    def transcript(self) -> list[str]:
        """Every non-empty line, scrollback then screen, in order."""
        return [ln for ln in self.scrollback + self.lines() if ln]

    @property
    def cursor(self) -> tuple[int, int]:
        return self.row, self.col

    # -- input --------------------------------------------------------------

    def feed(self, data: str) -> None:
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                i = self._escape(data, i + 1)
                continue
            if ch == "\n":
                self.col = 0
                self._linefeed()
            elif ch == "\r":
                self.col = 0
                self._wrap_pending = False
            elif ch == "\b":
                self.col = max(0, self.col - 1)
                self._wrap_pending = False
            elif ch == "\a":
                pass
            elif ch < " ":
                raise UnknownSequence(f"control character {ch!r}")
            else:
                self._put(ch)
            i += 1

    def _put(self, ch: str) -> None:
        if self._wrap_pending:
            self.col = 0
            self._linefeed()
        self._screen[self.row][self.col] = ch
        if self.col == self.cols - 1:
            self._wrap_pending = True
        else:
            self.col += 1

    def _linefeed(self) -> None:
        self._wrap_pending = False
        if self.row == self.bottom:
            self._scroll_up()
        elif self.row < self.rows - 1:
            self.row += 1

    def _scroll_up(self) -> None:
        screen = self._screen
        gone = "".join(screen[self.top]).rstrip()
        if self.top == 0 and not self.alt_active:
            self.scrollback.append(gone)
        del screen[self.top]
        screen.insert(self.bottom, [" "] * self.cols)
        saved = self._saved[self.alt_active]
        if (self.flavor == "xterm.js" and saved is not None
                and self.top <= saved[0] <= self.bottom):
            self._saved[self.alt_active] = (max(self.top, saved[0] - 1), saved[1])

    # -- escape sequences ---------------------------------------------------

    def _escape(self, data: str, i: int) -> int:
        """Interpret the sequence after an ESC at ``data[i]``; return the
        index after it."""
        if i >= len(data):
            raise UnknownSequence("truncated escape sequence")
        ch = data[i]
        if ch == "7":
            self._saved[self.alt_active] = (self.row, self.col)
        elif ch == "8":
            self._restore()
        elif ch == "D":
            self._linefeed()
        elif ch == "]":
            end = min((j for j in (data.find("\a", i), data.find("\x1b\\", i)) if j >= 0),
                      default=-1)
            if end < 0:
                raise UnknownSequence("unterminated OSC")
            return end + (1 if data[end] == "\a" else 2)
        elif ch == "[":
            m = _CSI_RE.match(data, i)
            if m is None:
                raise UnknownSequence(f"malformed CSI at {data[i:i + 10]!r}")
            self._csi(m.group(1), m.group(2), m.group(3))
            return m.end()
        else:
            raise UnknownSequence(f"ESC {ch!r}")
        return i + 1

    def _restore(self) -> None:
        saved = self._saved[self.alt_active] or (0, 0)
        self.row, self.col = saved
        self._wrap_pending = False

    def _csi(self, params: str, inter: str, final: str) -> None:
        if inter:
            raise UnknownSequence(f"CSI {params}{inter}{final}")
        if params.startswith("?"):
            self._private_mode(params[1:], final)
            return
        nums = [int(p) if p else 0 for p in params.split(";")] if params else []

        def arg(k: int, default: int) -> int:
            return nums[k] if len(nums) > k and nums[k] else default

        self._wrap_pending = False
        if final == "A":
            floor = self.top if self.row >= self.top else 0
            self.row = max(floor, self.row - arg(0, 1))
        elif final == "B":
            ceiling = self.bottom if self.row <= self.bottom else self.rows - 1
            self.row = min(ceiling, self.row + arg(0, 1))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + arg(0, 1))
        elif final == "D":
            self.col = max(0, self.col - arg(0, 1))
        elif final in ("H", "f"):
            self.row = min(self.rows, arg(0, 1)) - 1
            self.col = min(self.cols, arg(1, 1)) - 1
        elif final == "K":
            mode = arg(0, 0)
            cells = self._screen[self.row]
            span = {0: range(self.col, self.cols), 1: range(0, self.col + 1),
                    2: range(self.cols)}.get(mode)
            if span is None:
                raise UnknownSequence(f"CSI {params}K")
            for c in span:
                cells[c] = " "
        elif final == "J":
            if arg(0, 0) != 0:
                raise UnknownSequence(f"CSI {params}J")
            for c in range(self.col, self.cols):
                self._screen[self.row][c] = " "
            for r in range(self.row + 1, self.rows):
                self._screen[r] = [" "] * self.cols
        elif final == "r":
            top, bottom = arg(0, 1) - 1, arg(1, self.rows) - 1
            if top < bottom <= self.rows - 1:
                self.top, self.bottom = top, bottom
            self.row = self.col = 0  # DECSTBM homes the cursor
        elif final == "m":
            pass  # colour and attributes: not modelled
        else:
            raise UnknownSequence(f"CSI {params}{final}")

    def _private_mode(self, params: str, final: str) -> None:
        if final not in ("h", "l"):
            raise UnknownSequence(f"CSI ?{params}{final}")
        for mode in params.split(";"):
            if mode in ("25", "7", "2004"):
                continue  # cursor visibility, autowrap, bracketed paste
            if mode != "1049":
                raise UnknownSequence(f"CSI ?{mode}{final}")
            if final == "h":
                self._saved[False] = (self.row, self.col)
                self.alt_active = True
                self._alt = self._blank_screen()
                self.row = self.col = 0
            else:
                # Restores the cursor even when the alternate screen was never
                # entered (the 3.5.0-B132 mechanism).
                self.alt_active = False
                self._restore()
