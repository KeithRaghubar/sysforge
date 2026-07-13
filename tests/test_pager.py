"""
test_pager.py — the shared interactive paging seam (`primitives/pager.py`).

Covers `_pager_candidates`, the pure argv-builder behind `maybe_pager`. The
two defects it guards are B5:

  * the ``less`` fallback must never carry ``-X`` — that flag suppresses the
    alternate-screen switch, so less paints inline into scrollback and its
    redraws desync on modern terminals (blank open, scroll-up-only, looping
    the top);
  * ``$PAGER`` must be parsed as a shell word list, so ``PAGER="less -RF"``
    (a value carrying its own flags) yields a spawnable argv instead of a
    single un-runnable token.

This is the seam shared by ``sysforge log`` and ``sysforge state orphans`` —
which is why the same mangling reproduced across both verbs.
"""
from __future__ import annotations

import contextlib

from sysforge.primitives import pager
from sysforge.ui import progress


def _clear(monkeypatch):
    monkeypatch.delenv("PAGER", raising=False)


def test_less_fallback_never_uses_dash_x(monkeypatch):
    _clear(monkeypatch)
    cands = pager._pager_candidates()
    less = [c for c in cands if c and c[0] == "less"]
    assert less, "a less candidate must always be offered"
    for c in cands:
        assert "-X" not in c, f"-X breaks alternate-screen paging (B5): {c}"
        # and it must never be smuggled inside a combined short flag
        assert not any("X" in tok for tok in c if tok.startswith("-")), c


def test_default_fallbacks_present_and_ordered(monkeypatch):
    _clear(monkeypatch)
    assert pager._pager_candidates() == [["less", "-RF"], ["more"]]


def test_pager_env_is_shlex_split(monkeypatch):
    monkeypatch.setenv("PAGER", "less -RF")
    cands = pager._pager_candidates()
    # The value with its own flags becomes a real argv, first-preferred.
    assert cands[0] == ["less", "-RF"]
    # Built-in fallbacks still follow, so a failed spawn degrades gracefully.
    assert ["less", "-RF"] in cands[1:] or ["more"] in cands


def test_single_token_pager_env(monkeypatch):
    monkeypatch.setenv("PAGER", "most")
    cands = pager._pager_candidates()
    assert cands[0] == ["most"]
    assert cands[-1] == ["more"]


def test_empty_pager_env_ignored(monkeypatch):
    monkeypatch.setenv("PAGER", "   ")
    # Whitespace-only $PAGER shlex-splits to nothing → skip it, use defaults.
    assert pager._pager_candidates() == [["less", "-RF"], ["more"]]


def test_pager_env_less_dash_x_combined_is_stripped(monkeypatch):
    # $PAGER="less -RFX" — a combined short-flag cluster smuggling -X. The seam
    # must strip only the X, keeping -RF, so an inherited $PAGER can't
    # reintroduce the B5 alt-screen mangling on the --interactive review path.
    monkeypatch.setenv("PAGER", "less -RFX")
    cands = pager._pager_candidates()
    assert cands[0] == ["less", "-RF"]
    for c in cands:
        assert not any("X" in tok for tok in c if tok.startswith("-")), c


def test_pager_env_less_separate_dash_x_stripped(monkeypatch):
    monkeypatch.setenv("PAGER", "less -R -X")
    assert pager._pager_candidates()[0] == ["less", "-R"]


def test_pager_env_less_no_init_long_flag_stripped(monkeypatch):
    # --no-init is the long spelling of -X; it must go too.
    monkeypatch.setenv("PAGER", "less --no-init -R")
    assert pager._pager_candidates()[0] == ["less", "-R"]


def test_pager_env_non_less_pager_untouched(monkeypatch):
    # Only `less` reads -X as alt-screen suppression; leave other pagers'
    # flags alone (a foreign pager may use -X for its own meaning).
    monkeypatch.setenv("PAGER", "most -X")
    assert pager._pager_candidates()[0] == ["most", "-X"]


def test_sanitize_less_value_strips_x():
    assert pager._sanitize_less_value("-RFX") == "-RF"
    assert pager._sanitize_less_value("-R -X") == "-R"
    assert pager._sanitize_less_value("--no-init -R") == "-R"
    assert pager._sanitize_less_value("-RF") == "-RF"
    assert pager._sanitize_less_value("-X") == ""
    assert pager._sanitize_less_value("") == ""


class _FakeProc:
    def __init__(self):
        import io

        self.stdin = io.StringIO()
        self.waited = False

    def wait(self):
        self.waited = True


def test_pager_releases_progress_region_around_subprocess(monkeypatch):
    """The pager must run inside ``progress.suspended()`` so an active DECSTBM
    scroll region (from e.g. ``state orphans``' pre-scan phase) is released
    before less takes the terminal — else less is clamped to ``[1, N-1]`` and
    its redraws desync (B5 mangling reproduced via ``state orphans``)."""
    events: list[str] = []

    monkeypatch.setattr(pager.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(pager.subprocess, "Popen", lambda *a, **k: _FakeProc())

    @contextlib.contextmanager
    def _spy_suspended():
        events.append("suspend-enter")
        try:
            yield
        finally:
            events.append("suspend-exit")

    monkeypatch.setattr(progress, "suspended", _spy_suspended)

    with pager.maybe_pager(True):
        events.append("body")

    # Region released before the body writes to the pager, restored after.
    assert events == ["suspend-enter", "body", "suspend-exit"]


def test_maybe_pager_strips_dash_x_from_less_env(monkeypatch):
    """An inherited ``$LESS`` carrying ``-X`` is read by less *automatically*
    even when the argv is the built-in ``less -RF`` fallback — so the seam must
    hand the pager subprocess a sanitized ``$LESS``. This is the second B1
    vector (the first being the ``$PAGER`` argv, covered above)."""
    monkeypatch.delenv("PAGER", raising=False)
    monkeypatch.setenv("LESS", "-RFX")
    monkeypatch.setattr(pager.sys.stdout, "isatty", lambda: True, raising=False)

    captured: dict = {}

    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return _FakeProc()

    monkeypatch.setattr(pager.subprocess, "Popen", _fake_popen)

    with pager.maybe_pager(True):
        pass

    env = captured["env"]
    assert env is not None, "pager must run with an explicit sanitized env"
    assert "X" not in env.get("LESS", ""), env.get("LESS")
