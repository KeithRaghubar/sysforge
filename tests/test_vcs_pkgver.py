"""
test_vcs_pkgver.py — unit tests for sysforge.primitives.vcs_pkgver

All makepkg invocations are mocked; no real subprocess is executed.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.vcs_pkgver import (
    evaluate_vcs_pkgver,
    peek_upstream_commit,
    read_built_upstream_commit,
)


def _make_pkgbuild(tmp_path: Path) -> Path:
    pkg_dir = tmp_path / "foo-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=foo-git\n")
    return pkg_dir


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_evaluate_returns_resolved_pkgver(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    listing_stdout = "/var/cache/pkgdest/foo-git-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"

    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "0.1.r5.gabcdef-1"

    # Ensure both makepkg passes ran in the right cwd
    cwds = [call.kwargs.get("cwd") for call in run.call_args_list]
    assert all(cwd == pkg_dir for cwd in cwds)


def test_evaluate_returns_resolved_pkgver_with_epoch(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    listing_stdout = "foo-git-2:0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"

    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "2:0.1.r5.gabcdef-1"


def test_evaluate_returns_first_parseable_line_for_split_pkgs(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    # Split package: two pkgnames, same pkgver/pkgrel — either line is fine.
    listing_stdout = (
        "foo-git-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"
        "foo-git-docs-0.1.r5.gabcdef-1-x86_64.pkg.tar.zst\n"
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout=listing_stdout)]
        assert evaluate_vcs_pkgver(pkg_dir) == "0.1.r5.gabcdef-1"


def test_evaluate_returns_none_on_makepkg_resolve_failure(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(1, stderr="==> ERROR: pkgver() failed")]
        assert evaluate_vcs_pkgver(pkg_dir) is None
        # --packagelist must not be invoked after resolve fails
        assert run.call_count == 1


def test_evaluate_returns_none_on_packagelist_failure(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(2, stderr="boom")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_resolve_timeout(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd="makepkg", timeout=300)
        assert evaluate_vcs_pkgver(pkg_dir, timeout=300) is None


def test_evaluate_returns_none_on_packagelist_timeout(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), subprocess.TimeoutExpired(cmd="makepkg", timeout=30)]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_unparseable_packagelist(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout="not-a-pkg-filename\nstill-garbage\n")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_on_empty_packagelist(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = [_result(0), _result(0, stdout="")]
        assert evaluate_vcs_pkgver(pkg_dir) is None


def test_evaluate_returns_none_when_pkgbuild_missing(tmp_path):
    pkg_dir = tmp_path / "no-pkgbuild"
    pkg_dir.mkdir()
    # No subprocess call should be attempted.
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert evaluate_vcs_pkgver(pkg_dir) is None
        run.assert_not_called()


def test_evaluate_returns_none_when_makepkg_missing(tmp_path):
    pkg_dir = _make_pkgbuild(tmp_path)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = FileNotFoundError("makepkg")
        assert evaluate_vcs_pkgver(pkg_dir) is None


# ---------------------------------------------------------------------------
# peek_upstream_commit / read_built_upstream_commit
# ---------------------------------------------------------------------------

_REMOTE_SHA = "abcdef0123456789abcdef0123456789abcdef01"
_BUILT_SHA = "1234567890abcdef1234567890abcdef12345678"


def _write_pkgbuild(pkg_dir: Path, source_lines: str, *, pkgname: str = "foo-git") -> Path:
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgname={pkgname}\npkgver=0\npkgrel=1\n{source_lines}\n"
    )
    return pkg_dir


def test_peek_upstream_commit_branch_via_lsremote(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git#branch=main')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_REMOTE_SHA}\trefs/heads/main\n")
        assert peek_upstream_commit(pkg_dir) == _REMOTE_SHA
    args, kwargs = run.call_args
    assert args[0] == ["git", "ls-remote", "https://example.com/foo.git", "main"]


def test_peek_upstream_commit_no_fragment_uses_head(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_REMOTE_SHA}\tHEAD\n")
        assert peek_upstream_commit(pkg_dir) == _REMOTE_SHA
    args, _ = run.call_args
    assert args[0][-1] == "HEAD"


def test_peek_upstream_commit_pinned_fragment_returns_directly(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        f"source=('git+https://example.com/foo.git#commit={_REMOTE_SHA}')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert peek_upstream_commit(pkg_dir) == _REMOTE_SHA
        run.assert_not_called()


def test_peek_upstream_commit_named_clone_form(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('myclone::git+https://example.com/foo.git#tag=v1.2')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_REMOTE_SHA}\trefs/tags/v1.2\n")
        assert peek_upstream_commit(pkg_dir) == _REMOTE_SHA


def test_peek_upstream_commit_returns_none_for_multi_git_source(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://a.example.com/foo.git' 'git+https://b.example.com/bar.git')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert peek_upstream_commit(pkg_dir) is None
        run.assert_not_called()


def test_peek_upstream_commit_returns_none_for_unresolved_pkgver(tmp_path):
    pkg_dir = pkg_dir = tmp_path / "foo-git"
    pkg_dir.mkdir()
    # Use a custom var the parser cannot resolve (no scalar definition).
    (pkg_dir / "PKGBUILD").write_text(
        'pkgname=foo-git\npkgver=0\npkgrel=1\n'
        'source=("git+https://example.com/foo.git#tag=v${_unresolved}")\n'
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert peek_upstream_commit(pkg_dir) is None
        run.assert_not_called()


def test_peek_upstream_commit_ignores_non_git_sources(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git' '0001-fix.patch')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_REMOTE_SHA}\tHEAD\n")
        # Patch alongside a single git source still counts as single-git-source.
        assert peek_upstream_commit(pkg_dir) == _REMOTE_SHA


def test_peek_upstream_commit_returns_none_when_lsremote_fails(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(128, stderr="fatal: unable to access ...")
        assert peek_upstream_commit(pkg_dir) is None


def test_peek_upstream_commit_returns_none_on_timeout(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)
        assert peek_upstream_commit(pkg_dir) is None


def test_peek_upstream_commit_returns_none_when_pkgbuild_missing(tmp_path):
    pkg_dir = tmp_path / "absent"
    pkg_dir.mkdir()
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert peek_upstream_commit(pkg_dir) is None
        run.assert_not_called()


def test_read_built_upstream_commit_runs_rev_parse(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    src_dir = pkg_dir / "src" / "foo"
    src_dir.mkdir(parents=True)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_BUILT_SHA}\n")
        assert read_built_upstream_commit(pkg_dir) == _BUILT_SHA
    args, _ = run.call_args
    assert args[0][:3] == ["git", "-C", str(src_dir)]
    assert args[0][3:] == ["rev-parse", "HEAD"]


def test_read_built_upstream_commit_named_clone(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('myclone::git+https://example.com/foo.git')",
    )
    src_dir = pkg_dir / "src" / "myclone"
    src_dir.mkdir(parents=True)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(0, stdout=f"{_BUILT_SHA}\n")
        assert read_built_upstream_commit(pkg_dir) == _BUILT_SHA


def test_read_built_upstream_commit_none_when_srcdir_missing(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    # No src/foo directory exists yet — function must return None without
    # touching subprocess.
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert read_built_upstream_commit(pkg_dir) is None
        run.assert_not_called()


def test_read_built_upstream_commit_none_on_rev_parse_failure(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://example.com/foo.git')",
    )
    (pkg_dir / "src" / "foo").mkdir(parents=True)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        run.return_value = _result(128, stderr="not a git repo")
        assert read_built_upstream_commit(pkg_dir) is None


def test_read_built_upstream_commit_none_for_multi_git_source(tmp_path):
    pkg_dir = _write_pkgbuild(
        tmp_path / "foo-git",
        "source=('git+https://a.example.com/foo.git' 'git+https://b.example.com/bar.git')",
    )
    (pkg_dir / "src" / "foo").mkdir(parents=True)
    with patch("sysforge.primitives.vcs_pkgver.subprocess.run") as run:
        assert read_built_upstream_commit(pkg_dir) is None
        run.assert_not_called()
