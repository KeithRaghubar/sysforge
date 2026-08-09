# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""The source=() warning: what the freeze cannot cover (3.0.0-F2)."""
from sysforge.primitives.net_policy import warn_ungated_sources


def _write(tmp_path, body):
    d = tmp_path / "pkg"
    d.mkdir(exist_ok=True)
    (d / "PKGBUILD").write_text(body, encoding="utf-8")
    return d


def test_remote_uncached_source_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_srcdest", lambda: tmp_path / "empty")
    d = _write(tmp_path, 'pkgname=mesa\nsource=("https://example.org/mesa.tar.xz")\n')
    assert warn_ungated_sources(d) == ["https://example.org/mesa.tar.xz"]


def test_local_file_source_is_not_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_srcdest", lambda: tmp_path / "empty")
    d = _write(tmp_path, 'pkgname=mesa\nsource=("fix.patch")\n')
    (d / "fix.patch").write_text("", encoding="utf-8")
    assert warn_ungated_sources(d) == []


def test_already_cached_source_is_not_reported(tmp_path, monkeypatch):
    """A warning that fires on every build is a warning nobody reads."""
    srcdest = tmp_path / "srcdest"
    srcdest.mkdir()
    (srcdest / "mesa.tar.xz").write_text("", encoding="utf-8")
    monkeypatch.setattr("sysforge.primitives.pacman.get_srcdest", lambda: srcdest)
    d = _write(tmp_path, 'pkgname=mesa\nsource=("https://example.org/mesa.tar.xz")\n')
    assert warn_ungated_sources(d) == []


def test_arch_split_sources_are_fully_reported(tmp_path, monkeypatch):
    """source_<arch> is NOT merged by parse_pkgbuild — read it explicitly.

    Missing this silently under-reports on every arch-split PKGBUILD.
    """
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_srcdest", lambda: tmp_path / "empty")
    d = _write(tmp_path, (
        'pkgname=mesa\n'
        'source=("https://example.org/common.tar.xz")\n'
        'source_x86_64=("https://example.org/x86.tar.xz")\n'
    ))
    found = warn_ungated_sources(d)
    assert "https://example.org/common.tar.xz" in found
    assert "https://example.org/x86.tar.xz" in found


def test_git_source_with_fragment_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_srcdest", lambda: tmp_path / "empty")
    d = _write(tmp_path, 'pkgname=m\nsource=("git+https://example.org/m.git#tag=v1")\n')
    assert warn_ungated_sources(d) == ["git+https://example.org/m.git#tag=v1"]


def test_named_source_uses_the_local_name_for_the_cache_check(tmp_path, monkeypatch):
    """`name::url` caches as `name`, not as the URL basename."""
    srcdest = tmp_path / "srcdest"
    srcdest.mkdir()
    (srcdest / "mesa-25.tar.xz").write_text("", encoding="utf-8")
    monkeypatch.setattr("sysforge.primitives.pacman.get_srcdest", lambda: srcdest)
    d = _write(tmp_path, 'pkgname=m\nsource=("mesa-25.tar.xz::https://example.org/d")\n')
    assert warn_ungated_sources(d) == []


def test_missing_pkgbuild_returns_empty(tmp_path):
    d = tmp_path / "nothing"
    d.mkdir()
    assert warn_ungated_sources(d) == []
