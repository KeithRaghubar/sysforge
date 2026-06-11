"""
test_pkgbuild_review.py — unit tests for primitives/pkgbuild_review.py.

Drives the review gate against real git repos in tmp_path (git is a hard
dependency of sysforge's source handling, so it is available wherever the
suite runs). TTY-dependent paths monkeypatch the isatty probes and input();
the pager is stubbed in the view test so nothing spawns.
"""
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.primitives.pkgbuild_review import (
    DECISION_ABORT,
    DECISION_ACCEPT,
    DECISION_CLEAN,
    DECISION_NO_GIT,
    DECISION_SKIP,
    head_commit,
    commit_exists,
    review_target,
)


# ---------------------------------------------------------------------------
# git fixtures
# ---------------------------------------------------------------------------

_GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test"]


def _git(d, *args):
    subprocess.run(
        ["git", "-C", str(d), *_GIT_ID, *args],
        check=True, capture_output=True, text=True,
    )


def _repo(tmp_path, name="htop", pkgbuild="pkgname=htop\npkgver=1.0\n"):
    d = tmp_path / name
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "PKGBUILD").write_text(pkgbuild)
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "initial")
    return d


def _commit(d, fname, content, msg="change"):
    (d / fname).write_text(content)
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", msg)
    return head_commit(d)


def _tty(monkeypatch, answers):
    """Pretend stdin+stdout are TTYs and feed canned prompt answers."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(it))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_head_commit_non_repo_is_none(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert head_commit(d) is None


def test_commit_exists(tmp_path):
    d = _repo(tmp_path)
    sha = head_commit(d)
    assert commit_exists(d, sha)
    assert not commit_exists(d, "0" * 40)


# ---------------------------------------------------------------------------
# review_target — non-interactive decisions
# ---------------------------------------------------------------------------

def test_non_git_dir(tmp_path):
    d = tmp_path / "local-pkg"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=local-pkg\n")
    assert review_target("local-pkg", d, None) == DECISION_NO_GIT


def test_clean_when_head_matches_reviewed(tmp_path):
    d = _repo(tmp_path)
    assert review_target("htop", d, head_commit(d)) == DECISION_CLEAN


def test_non_tty_auto_accepts_change(tmp_path):
    """Unattended runs must not hang on the prompt — auto-accept with a warn.
    (pytest's stdin/stdout are not TTYs, so this is the natural path here.)"""
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    assert review_target("htop", d, old) == DECISION_ACCEPT


# ---------------------------------------------------------------------------
# review_target — interactive prompt
# ---------------------------------------------------------------------------

def test_prompt_accept(tmp_path, monkeypatch, capsys):
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    _tty(monkeypatch, ["a"])
    assert review_target("htop", d, old) == DECISION_ACCEPT
    out = capsys.readouterr().out
    assert "source changed since last accepted build" in out
    assert "PKGBUILD" in out  # --stat names the changed file


def test_prompt_skip(tmp_path, monkeypatch):
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    _tty(monkeypatch, ["s"])
    assert review_target("htop", d, old) == DECISION_SKIP


def test_prompt_abort(tmp_path, monkeypatch):
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    _tty(monkeypatch, ["b"])
    assert review_target("htop", d, old) == DECISION_ABORT


def test_prompt_view_then_accept(tmp_path, monkeypatch, capsys):
    """'v' pages the full patch (pager stubbed) and re-prompts."""
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "evil.install", "post_install() { :; }\n")

    @contextmanager
    def _no_pager(use_pager):
        yield

    monkeypatch.setattr(
        "sysforge.primitives.pkgbuild_review.maybe_pager", _no_pager)
    _tty(monkeypatch, ["v", "a"])
    assert review_target("htop", d, old) == DECISION_ACCEPT
    out = capsys.readouterr().out
    # The full-tree patch surfaces the new .install file, not just PKGBUILD.
    assert "evil.install" in out
    assert "post_install" in out


def test_prompt_reprompts_on_invalid_input(tmp_path, monkeypatch):
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    _tty(monkeypatch, ["x", "", "a"])
    assert review_target("htop", d, old) == DECISION_ACCEPT


def test_prompt_eof_aborts(tmp_path, monkeypatch):
    """No answer is not consent: EOF at the prompt aborts the run."""
    d = _repo(tmp_path)
    old = head_commit(d)
    _commit(d, "PKGBUILD", "pkgname=htop\npkgver=2.0\n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert review_target("htop", d, old) == DECISION_ABORT


# ---------------------------------------------------------------------------
# review_target — diff-base selection
# ---------------------------------------------------------------------------

def test_first_review_is_full_content(tmp_path, monkeypatch, capsys):
    """No recorded reviewed_commit → review against git's empty tree, so the
    user sees the whole content of a brand-new clone."""
    d = _repo(tmp_path)
    _tty(monkeypatch, ["a"])
    assert review_target("htop", d, None) == DECISION_ACCEPT
    out = capsys.readouterr().out
    assert "first review" in out
    assert "PKGBUILD" in out


def test_vanished_reviewed_commit_falls_back_to_full_review(
        tmp_path, monkeypatch, capsys):
    """A recorded sha that no longer exists (purge + re-clone) must not crash
    or silently pass — it degrades to the full-content review."""
    d = _repo(tmp_path)
    _tty(monkeypatch, ["a"])
    assert review_target("htop", d, "0" * 40) == DECISION_ACCEPT
    out = capsys.readouterr().out
    assert "first review" in out
