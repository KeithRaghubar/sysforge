# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_progress_lifecycle.py — one owner for the progress bar's lifetime (3.3.0-F2).

``cli.main`` wraps the run in ``progress.session()``, which releases the
terminal however the run ends: a normal ``sys.exit``, Ctrl-C (before
``aborted (Ctrl-C)`` prints), or ``log.fatal``. ``log`` no longer reaches up
into ``ui`` to do it.
"""
import ast
import io
import sys
from pathlib import Path

import pytest

from sysforge import cli, log
from sysforge.ui import progress


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def tty_stderr(monkeypatch):
    """Returns an installer: pytest puts its own sys.stderr back between
    fixture setup and the test call, so the test body installs the fake."""
    monkeypatch.setattr(log, "_DRY_RUN", False)
    for var in ("CI", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    def install() -> _TTY:
        buf = _TTY()
        monkeypatch.setattr(sys, "stderr", buf)
        return buf

    yield install
    progress.shutdown()


def test_a_normal_exit_releases_the_region(tty_stderr, monkeypatch):
    stderr = tty_stderr()
    def fake_main():
        progress.init()
        progress.phase("building")
        sys.exit(0)

    monkeypatch.setattr(cli, "_main", fake_main)
    with pytest.raises(SystemExit):
        cli.main()
    assert progress._bar.reserved is False
    assert progress._RESET_REGION in stderr.getvalue()


@pytest.mark.parametrize("in_prompt", [False, True], ids=["mid-run", "mid-prompt"])
def test_ctrl_c_releases_the_region_before_the_abort_message(tty_stderr, monkeypatch, in_prompt):
    stderr = tty_stderr()
    def fake_main():
        progress.init()
        progress.phase("building")
        if in_prompt:
            with progress.yield_terminal("prompt"):
                raise KeyboardInterrupt
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_main", fake_main)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 130
    out = stderr.getvalue()
    assert out.rindex(progress._RESET_REGION) < out.index("aborted (Ctrl-C)")
    assert progress._bar.reserved is False


def test_log_fatal_exits_through_the_session(tty_stderr):
    """fatal() no longer tears the bar down itself; the session does, on the
    way out, after the message."""
    stderr = tty_stderr()
    with pytest.raises(SystemExit), progress.session():
        progress.init()
        progress.phase("building")
        log.fatal("[TEST]", "cannot continue")
    out = stderr.getvalue()
    assert out.index("cannot continue") < out.rindex(progress._RESET_REGION)
    assert progress._bar.reserved is False


def test_closing_the_unified_log_leaves_the_bar_alone(tty_stderr):
    tty_stderr()
    progress.init()
    progress.phase("building")
    log.close_unified_log()
    assert progress._bar.reserved is True


def test_log_does_not_import_the_ui_layer():
    """log is below ui; the lifecycle belongs to cli.main, not to log."""
    tree = ast.parse(Path(log.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(m == "sysforge.ui" or m.startswith("sysforge.ui.") for m in imported), imported
