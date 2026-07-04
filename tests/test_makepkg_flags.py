# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

from sysforge.primitives.makepkg_flags import (
    _strip_all_lto,
    _strip_full_lto,
    resolve_effective_linker,
)


def test_strip_all_lto_removes_thin_unlike_strip_full_lto():
    flags = "-O2 -flto=thin -flto -flto=auto -flto=4 -pipe"
    cleaned, stripped = _strip_all_lto(flags)
    assert cleaned == "-O2 -pipe"
    assert set(stripped) == {"-flto=thin", "-flto", "-flto=auto", "-flto=4"}
    # Contrast: _strip_full_lto deliberately preserves clang-PGO-friendly thin.
    full_cleaned, _ = _strip_full_lto(flags)
    assert "-flto=thin" in full_cleaned


def test_strip_all_lto_noop_without_lto():
    cleaned, stripped = _strip_all_lto("-O2 -pipe")
    assert cleaned == "-O2 -pipe"
    assert stripped == []


def test_effective_linker_override_wins(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    got = resolve_effective_linker(
        ld_override="lld",
        profile_ldflags="-fuse-ld=mold",
        system_ldflags="-fuse-ld=gold",
    )
    assert got == "lld"


def test_effective_linker_profile_fallback(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    got = resolve_effective_linker(
        ld_override=None,
        profile_ldflags="-fuse-ld=lld -Wl,-O2",
        system_ldflags="-fuse-ld=gold",
    )
    assert got == "lld"


def test_effective_linker_system_fallback(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    got = resolve_effective_linker(
        ld_override=None,
        profile_ldflags="",
        system_ldflags="-fuse-ld=gold",
    )
    assert got == "gold"


def test_effective_linker_defaults_to_ld(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    got = resolve_effective_linker(
        ld_override=None, profile_ldflags="", system_ldflags=""
    )
    assert got == "ld"


def test_effective_linker_not_on_path_falls_back(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    got = resolve_effective_linker(
        ld_override=None,
        profile_ldflags="-fuse-ld=lld",
        system_ldflags="",
    )
    assert got == "ld"
