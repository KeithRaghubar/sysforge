# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""stock_pkg: unprivileged stock-build lookup, fetch, verify, extract (3.4.0-F6)."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from sysforge.primitives import stock_pkg as sp


_REAL_REPO_SERVERS = sp._repo_servers


@pytest.fixture(autouse=True)
def _no_host_mirrorlist(monkeypatch):
    """Mirror fallback reads pacman-conf; keep this host's mirrorlist out of tests."""
    monkeypatch.setattr(sp, "_repo_servers", lambda repo: [])


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


# ---------------------------------------------------------------------------
# 3.4.0-B19: get through where pacman itself would
# ---------------------------------------------------------------------------

def test_resolve_skips_dependency_resolution(monkeypatch):
    """`-Sp` resolves deps against the installed system, so an unrelated
    conflict would block a bench that needs only two archives: `-dd` decouples."""
    seen = {}

    def probe(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="clang|23.1.1-1|extra|c.pkg.tar.zst|https://m/c|cc\n")

    monkeypatch.setattr(sp.run, "probe", probe)
    sp.resolve(["clang"])
    assert "-dd" in seen["argv"]


def _servers(monkeypatch, *servers):
    def probe(argv, **kw):
        assert argv == ["pacman-conf", "--repo", "extra", "Server"]
        return SimpleNamespace(returncode=0, stderr="", stdout="".join(f"{s}\n" for s in servers))

    monkeypatch.setattr(sp, "_repo_servers", _REAL_REPO_SERVERS)
    monkeypatch.setattr(sp.run, "probe", probe)


def test_obtain_falls_back_to_the_next_mirror(tmp_path, monkeypatch):
    """`%l` names only the first Server; a lagging mirror 404s where pacman
    would simply try the next one."""
    import urllib.error

    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    _servers(monkeypatch, "https://mirror/extra", "https://good/extra")
    tried = []

    def opener(url, timeout):
        tried.append(url)
        if url.startswith("https://mirror/"):
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return io.BytesIO(b"archive")

    got = sp.obtain(_pkg(), tmp_path, offline=False, opener=opener)
    assert got.read_bytes() == b"archive"
    assert tried == [_pkg().location, "https://good/extra/" + _pkg().filename]


def test_obtain_reports_every_mirror_when_all_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    _servers(monkeypatch, "https://mirror/extra", "https://other/extra")

    def opener(url, timeout):
        raise OSError(f"down: {url}")

    with pytest.raises(sp.StockUnavailable, match="other/extra"):
        sp.obtain(_pkg(), tmp_path, offline=False, opener=opener)


def test_obtain_corrupt_cache_downloads_a_fresh_copy(tmp_path, monkeypatch):
    """A cached archive that fails the checksum is skipped, not fatal, and the
    cache file itself is left untouched."""
    cache = tmp_path / "cache"
    cache.mkdir()
    pkg = _pkg()
    (cache / pkg.filename).write_bytes(b"tampered")
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [cache])
    work = tmp_path / "w"
    work.mkdir()
    got = sp.obtain(pkg, work, offline=False, opener=lambda url, timeout: io.BytesIO(b"archive"))
    assert got.parent == work and got.read_bytes() == b"archive"
    assert (cache / pkg.filename).read_bytes() == b"tampered"


# ---------------------------------------------------------------------------
# 3.4.0-B21: extract only what the compiler needs, drop downloads once unpacked
# ---------------------------------------------------------------------------

_CLANG_LIST = "\n".join([
    ".PKGINFO", "usr/", "usr/bin/", "usr/bin/clang", "usr/bin/clang++", "usr/bin/clang-23",
    "usr/bin/clang-tidy", "usr/bin/clangd", "usr/bin/scan-build",
    "usr/include/clang/Basic/X.h", "usr/lib/libclang-cpp.so.23.1", "usr/lib/libclang.so",
    "usr/lib/libclangAST.a", "usr/lib/clang/", "usr/lib/clang/23/include/stddef.h",
    "usr/share/man/man1/clang.1.gz",
]) + "\n"
_LIBS_LIST = "usr/\nusr/lib/\nusr/lib/libLLVM.so.23.1\nusr/lib/libLLVM.so\nusr/lib/libLTO.so\n"


def _tar(monkeypatch, listings):
    calls = []

    def probe(argv, **kw):
        calls.append(argv)
        if "-tf" in argv:
            return SimpleNamespace(returncode=0, stderr="", stdout=listings[Path(argv[-1]).name])
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(sp.run, "probe", probe)
    return calls


def test_extract_passes_only_matching_members(tmp_path, monkeypatch):
    calls = _tar(monkeypatch, {"clang.pkg": _CLANG_LIST, "libs.pkg": _LIBS_LIST})
    sp.extract([tmp_path / "clang.pkg", tmp_path / "libs.pkg"], tmp_path / "prefix")
    xs = [c for c in calls if "-xf" in c]
    clang_members = xs[0][xs[0].index(str(tmp_path / "prefix")) + 1:]
    libs_members = xs[1][xs[1].index(str(tmp_path / "prefix")) + 1:]
    assert clang_members == ["usr/bin/clang", "usr/bin/clang++", "usr/bin/clang-[0-9]*",
                             "usr/lib/libclang-cpp.so*", "usr/lib/clang"]
    assert libs_members == ["usr/lib/libLLVM*.so*"]


def test_extract_removes_a_downloaded_archive_and_keeps_a_cached_one(tmp_path, monkeypatch):
    work, cache = tmp_path / "w", tmp_path / "cache"
    work.mkdir()
    cache.mkdir()
    downloaded, cached = work / "libs.pkg", cache / "clang.pkg"
    downloaded.write_bytes(b"x")
    cached.write_bytes(b"x")
    _tar(monkeypatch, {"clang.pkg": _CLANG_LIST, "libs.pkg": _LIBS_LIST})
    sp.extract([cached, downloaded], tmp_path / "prefix", downloaded_in=work)
    assert not downloaded.exists()
    assert cached.exists()
