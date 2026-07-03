"""
test_git_ops.py — unit tests for sysforge.primitives.git_ops.

Covers:
    purge_srcdest
        removes matching tarballs for a pkgbase
        ignores different pkgbase
        ignores prefix-sharing names
        no-ops on missing srcdest_dir
        no-ops on None srcdest_dir
        no-ops when srcdest is inside pkgbuild_dir
    purge_src
        allows detached HEAD at a remote-reachable commit (source=repo pin)
        refuses detached HEAD on a local-only commit
"""

import subprocess

import pytest

from sysforge.primitives.git_ops import purge_src, purge_srcdest


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo_with_remote(tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    _git("clone", str(remote), str(work), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "PKGBUILD").write_text("pkgname=x\n")
    _git("add", "PKGBUILD", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    _git("push", "origin", "HEAD", cwd=work)
    _git("tag", "1-1", cwd=work)
    _git("push", "origin", "1-1", cwd=work)
    return work


def test_purge_src_allows_detached_head_at_remote_reachable_commit(tmp_path):
    work = _make_repo_with_remote(tmp_path)
    _git("checkout", "--detach", "1-1", cwd=work)
    purge_src(work)          # must not raise
    assert not work.exists()


def test_purge_src_refuses_detached_head_on_local_only_commit(tmp_path):
    work = _make_repo_with_remote(tmp_path)
    _git("checkout", "--detach", "1-1", cwd=work)
    (work / "PKGBUILD").write_text("pkgname=y\n")
    _git("commit", "-am", "local-only work", cwd=work)
    with pytest.raises(RuntimeError, match="refusing to purge"):
        purge_src(work)


def test_purge_srcdest_removes_matching_tarballs(tmp_path):
    srcdest = tmp_path / "srcdest"
    srcdest.mkdir()
    victim = srcdest / "linux-custom-7.0.12.tar.zst"
    victim.write_bytes(b"x")
    keeper_other_pkg = srcdest / "linux-custom-headers-extra.tar.zst"
    keeper_other_pkg.write_bytes(b"x")
    keeper_prefix = srcdest / "linux-custom-tools-1.0.tar.gz"  # different pkgbase sharing prefix
    keeper_prefix.write_bytes(b"x")

    n = purge_srcdest("linux-custom", srcdest)

    assert n == 1
    assert not victim.exists()
    assert keeper_other_pkg.exists()
    assert keeper_prefix.exists()


def test_purge_srcdest_missing_dir_is_noop(tmp_path):
    assert purge_srcdest("foo", tmp_path / "nope") == 0


def test_purge_srcdest_none_srcdest_is_noop():
    assert purge_srcdest("foo", None) == 0


def test_purge_srcdest_inside_pkgbuild_dir_is_noop(tmp_path):
    # makepkg default: SRCDEST unset → sources live in the checkout, which the
    # checkout rmtree already covers.
    pkgdir = tmp_path / "pkg"
    pkgdir.mkdir()
    (pkgdir / "foo-1.0.tar.gz").write_bytes(b"x")
    assert purge_srcdest("foo", pkgdir, pkgbuild_dir=pkgdir) == 0
    assert (pkgdir / "foo-1.0.tar.gz").exists()
