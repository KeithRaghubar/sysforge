# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_makepkg_artifacts.py — the artifact matcher lifted out of build_core (3.1.0-F9)

``find_artifacts`` is the one home for "which .pkg.tar in this directory is
the one I mean". It carries three selection modes, and the reason there are
three is that PKGDEST is a long-lived archive of every historical build
(3.1.0-B1), so "the file for this package" is genuinely ambiguous there:

  1. strict   — exact PKGBUILD version; non-VCS packages match their filename
  2. newest   — filename parse + vercmp; VCS packages, whose pkgver() bumps
                at build time so the static parse never matches
  3. exact    — a caller-supplied version string; the sandbox dep-injection
                path, which must hand the container the version the host
                actually runs, not the newest one lying in the archive
"""
from pathlib import Path

from sysforge.primitives.makepkg_artifacts import find_artifacts


def _touch(d: Path, name: str) -> Path:
    p = d / name
    p.write_text("")
    return p


def test_strict_match_wins_for_a_static_version(tmp_path):
    want = _touch(tmp_path, "foo-1.2.3-1-x86_64.pkg.tar.zst")
    _touch(tmp_path, "foo-1.0.0-1-x86_64.pkg.tar.zst")
    assert find_artifacts(tmp_path, ["foo"], pkgbuild_ver="1.2.3-1") == [want]


def test_fallback_picks_the_newest_when_the_static_version_misses(tmp_path):
    """The VCS case: PKGBUILD says 0.1.0, the artifact says 0.1.0.r45.g123."""
    _touch(tmp_path, "foo-git-0.1.0.r10.gaaa-1-x86_64.pkg.tar.zst")
    newest = _touch(tmp_path, "foo-git-0.1.0.r45.gbbb-1-x86_64.pkg.tar.zst")
    assert find_artifacts(tmp_path, ["foo-git"], pkgbuild_ver="0.1.0") == [newest]


def test_installed_ver_filters_out_anything_not_newer(tmp_path):
    _touch(tmp_path, "foo-1.0.0-1-x86_64.pkg.tar.zst")
    newer = _touch(tmp_path, "foo-2.0.0-1-x86_64.pkg.tar.zst")
    assert find_artifacts(
        tmp_path, ["foo"], pkgbuild_ver=None, installed_ver="1.0.0-1") == [newer]


def test_exact_ver_selects_the_version_the_host_runs(tmp_path):
    """3.1.0-F9's core rule: never the newest in the archive. Handing the
    container a version the host does not run recreates the very skew the
    injection exists to remove."""
    want = _touch(tmp_path, "foo-1.0.0-1-x86_64.pkg.tar.zst")
    _touch(tmp_path, "foo-2.0.0-1-x86_64.pkg.tar.zst")
    assert find_artifacts(tmp_path, ["foo"], exact_ver="1.0.0-1") == [want]


def test_exact_ver_matches_an_epoch_qualified_version(tmp_path):
    """pacman -Q prints ``1:1.94.121-1``; the filename carries the same epoch."""
    want = _touch(tmp_path, "brave-bin-1:1.94.121-1-x86_64.pkg.tar.zst")
    _touch(tmp_path, "brave-bin-1.94.121-1-x86_64.pkg.tar.zst")
    assert find_artifacts(
        tmp_path, ["brave-bin"], exact_ver="1:1.94.121-1") == [want]


def test_exact_ver_returns_nothing_when_the_artifact_was_pruned(tmp_path):
    """A pruned archive entry is a warn-and-continue for the caller, so the
    matcher's job is simply to say 'not here' rather than fall back to a
    different version."""
    _touch(tmp_path, "foo-2.0.0-1-x86_64.pkg.tar.zst")
    assert find_artifacts(tmp_path, ["foo"], exact_ver="1.0.0-1") == []


def test_exact_ver_ignores_a_hyphen_prefix_of_another_package(tmp_path):
    """``linux`` must not match ``linux-custom`` — the filename parser anchors
    on the known pkgname for exactly this reason."""
    _touch(tmp_path, "linux-custom-6.1-1-x86_64.pkg.tar.zst")
    assert find_artifacts(tmp_path, ["linux"], exact_ver="6.1-1") == []


def test_signatures_are_never_returned(tmp_path):
    want = _touch(tmp_path, "foo-1.0.0-1-x86_64.pkg.tar.zst")
    _touch(tmp_path, "foo-1.0.0-1-x86_64.pkg.tar.zst.sig")
    assert find_artifacts(tmp_path, ["foo"], exact_ver="1.0.0-1") == [want]


def test_missing_search_dir_is_empty_not_an_error(tmp_path):
    assert find_artifacts(tmp_path / "absent", ["foo"], exact_ver="1-1") == []
