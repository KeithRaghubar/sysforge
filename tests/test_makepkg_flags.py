# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

from sysforge.primitives.makepkg_flags import resolve_effective_linker


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
