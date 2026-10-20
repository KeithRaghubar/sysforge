# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`clean_builddir = true` asks makepkg for --cleanbuild (3.5.0-B118).

It used to delete every entry of the PKGBUILD's directory except the PKGBUILD,
taking the checkout's .git, .SRCINFO and the local files source=() names with
it. The fixture config's `batch` profile sets the key, as the shipped one does.
"""
from unittest.mock import patch

import pytest

from sysforge.build import makepkg_wrapper as mw
from sysforge.build.makepkg_wrapper import BuildOptions


@pytest.fixture
def checkout(tmp_path):
    d = tmp_path / "foo-git"
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
    (d / ".SRCINFO").write_text("pkgbase = foo-git\n")
    (d / "fix-build.patch").write_text("--- a\n+++ b\n")
    (d / "src").mkdir()
    (d / "PKGBUILD").write_text(
        "pkgname=foo-git\npkgver=1\npkgrel=1\nsource=(fix-build.patch)\n")
    return d


def _run(pkgbuild, profile, extra_flags=None):
    seen = {}
    with patch.object(mw, "_run_build", side_effect=lambda *a, **k: seen.update(k)):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override=profile,
            extra_flags=extra_flags))
    return seen


def test_clean_builddir_keeps_the_checkout(checkout):
    _run(checkout / "PKGBUILD", "batch")
    assert (checkout / ".git" / "HEAD").is_file()
    assert (checkout / ".SRCINFO").is_file()
    assert (checkout / "fix-build.patch").is_file()
    assert (checkout / "src").is_dir()  # makepkg's -C clears it, at build time


def test_clean_builddir_asks_makepkg_for_cleanbuild(checkout):
    seen = _run(checkout / "PKGBUILD", "batch", extra_flags=["--nocheck"])
    assert seen["extra_flags"] == ["--nocheck", "--cleanbuild"]


@pytest.mark.parametrize("flag", ["-C", "--cleanbuild"])
def test_clean_builddir_adds_no_second_cleanbuild(checkout, flag):
    seen = _run(checkout / "PKGBUILD", "batch", extra_flags=[flag])
    assert seen["extra_flags"] == [flag]


def test_a_profile_without_the_key_adds_nothing(checkout):
    seen = _run(checkout / "PKGBUILD", "standard", extra_flags=["--nocheck"])
    assert seen["extra_flags"] == ["--nocheck"]
