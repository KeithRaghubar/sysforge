# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""3.2.0-B31: partial replacement of a split pkgbase by stock packages.

Replacing one member of a split ``-git`` pkgbase with its stock repo package
(``pacman -S adwaita-icon-theme`` displacing ``adwaita-icon-theme-git``) leaves
the sibling (``adwaita-cursors-git``) installed and tracked, so ``update`` keeps
rebuilding the pkgbase under the name of the package that was just removed.
"""

from pathlib import Path

from sysforge.primitives.pkgbuild_meta import member_relations, parse_pkgbuild
from sysforge.update_result import _UpdateResult
from sysforge.update_split import annotate_split_members, detect_split_members

# Trimmed from the real AUR adwaita-icon-theme-git PKGBUILD: per-member
# provides/conflicts override the (absent) globals, and reference a private
# scalar that must be expanded.
_ADWAITA = """\
_pkgname=adwaita-icon-theme
pkgbase="$_pkgname-git"
pkgname=("$pkgbase" adwaita-cursors-git)
pkgver=51.0.r2.g82d305723
pkgrel=1
arch=(any)
depends=(gtk-update-icon-cache hicolor-icon-theme librsvg)

package_adwaita-icon-theme-git() {
\tdepends+=('adwaita-cursors')
\tprovides=("$_pkgname")
\tconflicts=("$_pkgname")
\tmeson install -C build --destdir "$pkgdir"
}

package_adwaita-cursors-git() {
\tunset depends
\tprovides=('adwaita-cursors')
\tconflicts=('adwaita-cursors')
\tcp -r cursors "$pkgdir/usr/share/icons/Adwaita/"
}
"""

# Split package whose second member is intentionally optional (never
# installed) and has no stock counterpart installed.
_FOO = """\
pkgbase=foo-git
pkgname=(foo-git foo-git-docs)
pkgver=1.0
pkgrel=1
provides=('foo=1.0')
conflicts=('foo')

package_foo-git() {
\ttrue
}

package_foo-git-docs() {
\tprovides+=('foo-docs>=1')
\ttrue
}
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "PKGBUILD"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# member_relations — PKGBUILD(5) PACKAGE SPLITTING override semantics
# ---------------------------------------------------------------------------

def test_member_relations_function_override_with_var_expansion(tmp_path):
    parsed = parse_pkgbuild(_write(tmp_path, _ADWAITA))
    assert member_relations(parsed, "adwaita-icon-theme-git") == {"adwaita-icon-theme"}
    assert member_relations(parsed, "adwaita-cursors-git") == {"adwaita-cursors"}


def test_member_relations_falls_back_to_globals_and_strips_versions(tmp_path):
    parsed = parse_pkgbuild(_write(tmp_path, _FOO))
    # No override in package_foo-git(): the globals apply, constraint stripped.
    assert member_relations(parsed, "foo-git") == {"foo"}
    # ``provides+=`` appends to the global array rather than replacing it.
    assert member_relations(parsed, "foo-git-docs") == {"foo", "foo-docs"}


def test_member_relations_unknown_member_uses_globals(tmp_path):
    parsed = parse_pkgbuild(_write(tmp_path, _FOO))
    assert member_relations(parsed, "nope") == {"foo"}


# ---------------------------------------------------------------------------
# detect_split_members
# ---------------------------------------------------------------------------

_REPO = {"adwaita-icon-theme", "adwaita-cursors", "foo"}


def _in_repo(name: str) -> bool:
    return name in _REPO


def test_detects_member_replaced_by_stock(tmp_path):
    info = detect_split_members(
        "adwaita-icon-theme-git",
        tracked=["adwaita-cursors-git"],
        pkgbuild_path=_write(tmp_path, _ADWAITA),
        installed={"adwaita-cursors-git": "51-1", "adwaita-icon-theme": "50.0-1"},
        in_repo=_in_repo,
    )
    assert info.replaced == {"adwaita-icon-theme-git": "adwaita-icon-theme"}
    assert info.stock_for_remaining == {"adwaita-cursors-git": "adwaita-cursors"}
    # The pkgbase names a member that is no longer installed → label it.
    assert info.via == ["adwaita-cursors-git"]


def test_never_installed_member_is_not_a_replacement(tmp_path):
    """A split member the user simply never installed (no stock counterpart
    on the system) is normal, not a partial replacement."""
    info = detect_split_members(
        "foo-git",
        tracked=["foo-git"],
        pkgbuild_path=_write(tmp_path, _FOO),
        installed={"foo-git": "1.0-1"},
        in_repo=_in_repo,
    )
    assert info.replaced == {}
    assert info.via == []


def test_stock_counterpart_must_come_from_a_repo(tmp_path):
    """An installed name that no sync repo carries (another foreign build)
    is not a stock replacement."""
    info = detect_split_members(
        "adwaita-icon-theme-git",
        tracked=["adwaita-cursors-git"],
        pkgbuild_path=_write(tmp_path, _ADWAITA),
        installed={"adwaita-cursors-git": "51-1", "adwaita-icon-theme": "50.0-1"},
        in_repo=lambda _n: False,
    )
    assert info.replaced == {}
    # The label still applies: the pkgbase name isn't on the system.
    assert info.via == ["adwaita-cursors-git"]


def test_single_package_pkgbuild_yields_nothing(tmp_path):
    p = _write(tmp_path, "pkgname=bar\npkgver=1\npkgrel=1\nprovides=(baz)\n")
    info = detect_split_members(
        "bar", tracked=["bar"], pkgbuild_path=p,
        installed={"bar": "1-1", "baz": "1-1"}, in_repo=lambda _n: True,
    )
    assert info.replaced == {} and info.via == []


# ---------------------------------------------------------------------------
# annotate_split_members — the update Phase 3→4 pass
# ---------------------------------------------------------------------------

def _result(pkgbase, pkgnames, action="NEEDS_REBUILD"):
    return _UpdateResult(
        pkgbase=pkgbase, pkgnames=pkgnames, action=action,
        installed_ver="1-1", pkgbuild_ver="2-1", pkgbuild_path=None,
    )


def test_annotate_sets_fields_for_source_built_entries(tmp_path):
    _write(tmp_path, _ADWAITA)
    r = _result("adwaita-icon-theme-git", ["adwaita-cursors-git"], action="DEVEL")
    annotate_split_members(
        [r],
        {"adwaita-icon-theme-git": {"build_mode": "source_built",
                                    "pkgbuild_dir": str(tmp_path)}},
        {"adwaita-cursors-git": "51-1", "adwaita-icon-theme": "50.0-1"},
        in_repo=_in_repo,
    )
    assert r.replaced_members == {"adwaita-icon-theme-git": "adwaita-icon-theme"}
    assert r.stock_for_remaining == {"adwaita-cursors-git": "adwaita-cursors"}
    assert r.via == ["adwaita-cursors-git"]


def test_annotate_skips_pacman_entries_and_missing_dirs(tmp_path):
    a = _result("mesa", ["mesa"], action="NEEDS_PACMAN_UPGRADE")
    b = _result("gone-git", ["gone-git"])
    annotate_split_members(
        [a, b],
        {"mesa": {"build_mode": "pacman"},
         "gone-git": {"build_mode": "source_built",
                      "pkgbuild_dir": str(tmp_path / "missing")}},
        {"mesa": "1-1", "gone-git": "1-1"},
        in_repo=_in_repo,
    )
    assert a.replaced_members == {} and a.via == []
    assert b.replaced_members == {} and b.via == []
