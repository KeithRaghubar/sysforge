# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""stock_pkg: unprivileged stock-build lookup, fetch, verify, extract (3.4.0-F6)."""
import hashlib
import io
from types import SimpleNamespace

import pytest

from sysforge.primitives import stock_pkg as sp


def _probe(stdout, rc=0, stderr=""):
    return lambda argv, **kw: SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _pkg(data=b"archive", location=None, sha=None):
    return sp.StockPkg("clang", "23.1.1-1", "extra", "clang-23.1.1-1-x86_64.pkg.tar.zst",
                       location or "https://mirror/extra/clang-23.1.1-1-x86_64.pkg.tar.zst",
                       sha or hashlib.sha256(data).hexdigest())


def test_resolve_parses_and_filters_dependency_rows(monkeypatch):
    out = ("llvm-libs|23.1.1-1|extra|llvm-libs-23.1.1-1-x86_64.pkg.tar.zst|https://m/l|aa\n"
           "zlib|1.3-1|core|zlib.pkg.tar.zst|https://m/z|bb\n"
           "clang|23.1.1-1|extra|clang-23.1.1-1-x86_64.pkg.tar.zst|https://m/c|cc\n")
    monkeypatch.setattr(sp.run, "probe", _probe(out))
    pkgs = sp.resolve(["clang", "llvm-libs"])
    assert [p.name for p in pkgs] == ["clang", "llvm-libs"]
    assert pkgs[0].sha256 == "cc" and pkgs[0].repo == "extra"


def test_resolve_unknown_package_raises(monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _probe("", rc=1, stderr="error: target not found: clang"))
    with pytest.raises(sp.StockUnavailable, match="target not found"):
        sp.resolve(["clang"])


def test_resolve_refuses_the_local_repo(monkeypatch):
    out = "clang|23.1.1-1|sysforge-local|c.pkg.tar.zst|file:///x/c|cc\n"
    monkeypatch.setattr(sp.run, "probe", _probe(out))
    with pytest.raises(sp.StockUnavailable, match="sysforge-local"):
        sp.resolve(["clang"])


def test_obtain_uses_cache_without_download(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    pkg = _pkg()
    (cache / pkg.filename).write_bytes(b"archive")
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [cache])

    def no_net(*a, **k):
        raise AssertionError("downloaded despite cache hit")

    assert sp.obtain(pkg, tmp_path, offline=False, opener=no_net) == cache / pkg.filename


def test_obtain_file_location_is_used_in_place(tmp_path, monkeypatch):
    f = tmp_path / "c.pkg.tar.zst"
    f.write_bytes(b"archive")
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    assert sp.obtain(_pkg(location=f"file://{f}"), tmp_path, offline=True) == f


def test_obtain_offline_without_cache_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [tmp_path / "empty"])
    with pytest.raises(sp.StockUnavailable, match="--offline"):
        sp.obtain(_pkg(), tmp_path, offline=True)


def test_obtain_downloads_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    work = tmp_path / "w"
    work.mkdir()
    got = sp.obtain(_pkg(), work, offline=False,
                    opener=lambda url, timeout: io.BytesIO(b"archive"))
    assert got.read_bytes() == b"archive" and got.parent == work


def test_obtain_checksum_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    with pytest.raises(sp.StockUnavailable, match="checksum"):
        sp.obtain(_pkg(sha="0" * 64), tmp_path, offline=False,
                  opener=lambda url, timeout: io.BytesIO(b"archive"))


def test_obtain_checksum_mismatch_on_a_cached_archive_raises(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    pkg = _pkg(sha="0" * 64)
    (cache / pkg.filename).write_bytes(b"tampered")
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [cache])
    with pytest.raises(sp.StockUnavailable, match="checksum"):
        sp.obtain(pkg, tmp_path, offline=True)


def test_obtain_download_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])

    def boom(url, timeout):
        raise OSError("network unreachable")

    with pytest.raises(sp.StockUnavailable, match="network unreachable"):
        sp.obtain(_pkg(), tmp_path, offline=False, opener=boom)


def test_extract_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _probe("", rc=1, stderr="bsdtar: bad archive"))
    with pytest.raises(sp.StockUnavailable, match="bad archive"):
        sp.extract([tmp_path / "x.pkg.tar.zst"], tmp_path / "prefix")


@pytest.mark.parametrize("exc", [
    __import__("http.client").client.IncompleteRead(b"part"),
    ValueError("unknown url type: 'ftp'"),
])
def test_obtain_non_oserror_download_failures_are_stock_unavailable(tmp_path, monkeypatch, exc):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])

    def boom(url, timeout):
        raise exc

    with pytest.raises(sp.StockUnavailable, match="could not download"):
        sp.obtain(_pkg(), tmp_path, offline=False, opener=boom)
