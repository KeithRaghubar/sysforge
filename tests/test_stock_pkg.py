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


# --- private sync-DB refresh (3.5.0-F1) -----------------------------------


def test_obtain_all_mirrors_failing_is_a_download_failure(tmp_path, monkeypatch):
    """Its own type, so the bench can tell a stale sync DB from an offline miss."""
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])

    def opener(url, timeout):
        raise OSError("HTTP Error 404: Not Found")

    with pytest.raises(sp.StockDownloadFailed):
        sp.obtain(_pkg(), tmp_path, offline=False, opener=opener)


def test_offline_miss_is_not_a_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.pacman, "get_pacman_cache_dirs", lambda: [])
    with pytest.raises(sp.StockUnavailable) as exc:
        sp.obtain(_pkg(), tmp_path, offline=True)
    assert not isinstance(exc.value, sp.StockDownloadFailed)


def test_resolve_reads_the_given_dbpath(tmp_path, monkeypatch):
    seen = []
    out = "clang|23.1.3-3|extra|clang-23.1.3-3-x86_64.pkg.tar.zst|https://m/c|cc\n"

    def probe(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(sp.run, "probe", probe)
    assert sp.resolve(["clang"], dbpath=tmp_path / "db")[0].version == "23.1.3-3"
    argv = seen[0]
    assert argv[argv.index("--dbpath") + 1] == str(tmp_path / "db")


def test_resolve_without_dbpath_uses_the_system_db(monkeypatch):
    seen = []
    monkeypatch.setattr(sp.run, "probe", lambda argv, **kw: seen.append(argv) or
                        SimpleNamespace(returncode=0, stderr="",
                                        stdout="clang|1-1|extra|c|https://m/c|cc\n"))
    sp.resolve(["clang"])
    assert "--dbpath" not in seen[0]


def test_private_sync_runs_unprivileged_into_its_own_dbpath(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sp, "_local_db", lambda: Path("/var/lib/pacman/local"))
    monkeypatch.setattr(sp.run, "probe", lambda argv, **kw: seen.append(argv) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    db = sp.private_sync(tmp_path)
    argv = seen[0]
    assert argv[:2] == ["fakeroot", "--"]
    assert "sudo" not in argv
    assert argv[argv.index("pacman") + 1] == "-Sy"
    assert argv[argv.index("--dbpath") + 1] == str(db)
    # never the system log, and pacman's root-only download sandbox is off
    assert argv[argv.index("--logfile") + 1] == "/dev/null"
    assert "--disable-sandbox" in argv
    assert db.parent == tmp_path
    # -Sp needs a local DB to read; it is the system's, linked, never copied
    assert (db / "local").is_symlink()
    assert (db / "local").readlink() == Path("/var/lib/pacman/local")


def test_private_sync_failure_raises_with_pacmans_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_local_db", lambda: Path("/var/lib/pacman/local"))
    monkeypatch.setattr(sp.run, "probe", _probe(
        "", rc=1, stderr="error: failed to synchronize all databases"))
    with pytest.raises(sp.StockUnavailable, match="failed to synchronize"):
        sp.private_sync(tmp_path)


def test_private_sync_without_fakeroot_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_local_db", lambda: Path("/var/lib/pacman/local"))
    monkeypatch.setattr(sp.run, "probe", _probe(
        "", rc=127, stderr="fakeroot: command not found"))
    with pytest.raises(sp.StockUnavailable, match="fakeroot"):
        sp.private_sync(tmp_path)


def test_local_db_follows_pacman_conf_dbpath(monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _probe("/srv/alpm/\n"))
    assert sp._local_db() == Path("/srv/alpm/local")


def test_local_db_defaults_when_pacman_conf_fails(monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _probe("", rc=1))
    assert sp._local_db() == Path("/var/lib/pacman/local")


# --- clang 23.1.3-4 split out clang-libs (3.5.0-B146) ------------------------


def _sync(rows):
    """pacman -Sp over a sync DB holding *rows* ({name: repo}): one unknown
    target fails the whole call and prints nothing, as pacman does."""
    def probe(argv, **kw):
        names = [a for a in argv[argv.index(sp._FORMAT) + 1:] if not a.startswith("-")]
        unknown = [n for n in names if n not in rows]
        if unknown:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr=f"error: target not found: {unknown[0]}")
        out = "".join(f"{n}|23.1.3-4|{rows[n]}|{n}.pkg.tar.zst|https://m/{n}|aa\n"
                      for n in names)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return probe


def test_resolve_includes_an_optional_package_the_repo_carries(monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _sync(
        {"clang": "extra", "llvm-libs": "extra", "clang-libs": "extra"}))
    pkgs = sp.resolve(["clang", "llvm-libs"], optional=["clang-libs"])
    assert [p.name for p in pkgs] == ["clang", "llvm-libs", "clang-libs"]


def test_resolve_drops_an_optional_package_the_repo_lacks(monkeypatch):
    """A sync DB from before the split has no clang-libs: the required
    packages must still resolve, not fail with the optional one."""
    monkeypatch.setattr(sp.run, "probe", _sync({"clang": "extra", "llvm-libs": "extra"}))
    pkgs = sp.resolve(["clang", "llvm-libs"], optional=["clang-libs"])
    assert [p.name for p in pkgs] == ["clang", "llvm-libs"]


def test_resolve_refuses_an_optional_package_from_the_local_repo(monkeypatch):
    monkeypatch.setattr(sp.run, "probe", _sync(
        {"clang": "extra", "llvm-libs": "extra", "clang-libs": "sysforge-local"}))
    with pytest.raises(sp.StockUnavailable, match="clang-libs resolves to sysforge-local"):
        sp.resolve(["clang", "llvm-libs"], optional=["clang-libs"])


def _prefix(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    return tmp_path


_COMPLETE = ("usr/bin/clang", "usr/lib/libclang-cpp.so.23.1", "usr/lib/libLLVM.so.23.1",
             "usr/lib/clang/23/include/stddef.h")


def test_check_complete_accepts_a_full_prefix(tmp_path):
    sp.check_complete(_prefix(tmp_path, *_COMPLETE))


@pytest.mark.parametrize("missing, named", [
    ("usr/lib/libclang-cpp.so.23.1", "libclang-cpp"),
    ("usr/lib/libLLVM.so.23.1", "libLLVM"),
    ("usr/lib/clang/23/include/stddef.h", "builtin headers"),
])
def test_check_complete_names_what_is_missing(tmp_path, missing, named):
    """A part missing from the prefix would be loaded from the host (the
    sysforge build) or not found at all: fail before timing anything."""
    prefix = _prefix(tmp_path, *(f for f in _COMPLETE if f != missing))
    with pytest.raises(sp.StockUnavailable, match=named):
        sp.check_complete(prefix)
