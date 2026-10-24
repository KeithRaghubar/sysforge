# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""A packages.toml entry's `cache = false` turns the compiler caches off (3.5.0-B139).

`packages add --no-cache` wrote the key and `packages list` showed it, but no
build read it, so ccache and sccache stayed on. The opt-out reaches makepkg's
environment: CCACHE_DISABLE for ccache (BUILDENV's or a ccache directory first
on PATH alike) and an empty RUSTC_WRAPPER, which wins over one exported by the
shell or a profile.
"""
from unittest.mock import patch

import pytest

from sysforge.build import makepkg_wrapper as mw
from sysforge.build.makepkg_wrapper import BuildOptions
from sysforge.primitives import config as cfgmod
from sysforge.primitives import paths


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "packages.toml"
    monkeypatch.setattr(paths, "PACKAGES_PATH", path)
    return path


@pytest.mark.parametrize("text, names, expected", [
    ('[[package]]\nname = "foo"\ncache = false\n', ["foo"], True),
    ('[[package]]\nname = "foo"\ncache = false\n', ["foo-git", "foo"], True),
    ('[[package]]\nname = "foo"\ncache = true\n', ["foo"], False),
    ('[[package]]\nname = "foo"\nenable_build_from_source = true\n', ["foo"], False),
    ('[[package]]\nname = "bar"\ncache = false\n', ["foo"], False),
    ('[group.g]\npackages = ["foo"]\ncache = false\n', ["foo"], True),
    # An explicit entry wins over a group default outright.
    ('[[package]]\nname = "foo"\n\n[group.g]\npackages = ["foo"]\ncache = false\n',
     ["foo"], False),
], ids=["entry", "any-name", "cache-true", "no-key", "other-pkg", "group", "entry-over-group"])
def test_cache_disabled_for(manifest, text, names, expected):
    manifest.write_text(text)
    assert cfgmod.cache_disabled_for({}, names) is expected


def test_no_manifest_disables_nothing(manifest):
    assert cfgmod.cache_disabled_for({}, ["foo"]) is False


def test_an_unreadable_manifest_disables_nothing(manifest):
    manifest.write_text("[[package]\n")
    assert cfgmod.cache_disabled_for({}, ["foo"]) is False


@pytest.fixture
def checkout(tmp_path):
    d = tmp_path / "foo"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=foo\npkgver=1\npkgrel=1\n")
    return d


def _injected_env(pkgbuild, extra_env=None):
    seen = {}
    with patch.object(mw, "_run_build", side_effect=lambda *a, **k: seen.update(k)):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override="standard",
            extra_env=extra_env))
    return seen["injected_env"] or {}


def test_opted_out_build_turns_both_caches_off(manifest, checkout):
    manifest.write_text('[[package]]\nname = "foo"\ncache = false\n')
    env = _injected_env(checkout / "PKGBUILD")
    assert env["CCACHE_DISABLE"] == "1"
    assert env["RUSTC_WRAPPER"] == ""


def test_opt_out_keeps_the_callers_env(manifest, checkout):
    manifest.write_text('[[package]]\nname = "foo"\ncache = false\n')
    env = _injected_env(checkout / "PKGBUILD", extra_env={"LLVM_PROFILE_FILE": "x"})
    assert env["LLVM_PROFILE_FILE"] == "x"
    assert env["CCACHE_DISABLE"] == "1"


def test_a_build_without_the_opt_out_leaves_the_caches_alone(manifest, checkout):
    manifest.write_text('[[package]]\nname = "foo"\nenable_build_from_source = true\n')
    env = _injected_env(checkout / "PKGBUILD")
    assert "CCACHE_DISABLE" not in env
    assert "RUSTC_WRAPPER" not in env
