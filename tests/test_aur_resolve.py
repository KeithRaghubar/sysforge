"""
test_aur_resolve.py — tests for recursive AUR dependency resolution
"""
import textwrap
from pathlib import Path

import pytest

from sysforge.primitives.aur_resolve import (
    ResolvedDep,
    _get_missing_deps,
    _is_soname,
    _strip_version,
    resolve_aur_deps,
    resolve_all_deps,
)


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------

class TestStripVersion:
    def test_bare_name(self):
        assert _strip_version("cmake") == "cmake"

    def test_ge(self):
        assert _strip_version("cmake>=3.16") == "cmake"

    def test_le(self):
        assert _strip_version("python<=3.12") == "python"

    def test_eq(self):
        assert _strip_version("foo=1.0") == "foo"

    def test_gt(self):
        assert _strip_version("bar>2") == "bar"

    def test_lt(self):
        assert _strip_version("baz<5") == "baz"

    def test_ne(self):
        assert _strip_version("qux!=3") == "qux"


class TestIsSoname:
    def test_simple_soname(self):
        assert _is_soname("libfoo.so") is True

    def test_versioned_soname(self):
        assert _is_soname("libfoo.so=2") is True

    def test_regular_package(self):
        assert _is_soname("cmake") is False

    def test_package_with_dots(self):
        assert _is_soname("python3.12") is False


# ---------------------------------------------------------------------------
# Integration tests — resolution (mocked subprocess + network)
# ---------------------------------------------------------------------------

def _make_pkgbuild(tmp_path, name, depends=None, makedepends=None):
    """Create a minimal PKGBUILD in tmp_path/name/PKGBUILD."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    deps_str = " ".join(f"'{d}'" for d in (depends or []))
    makedeps_str = " ".join(f"'{d}'" for d in (makedepends or []))
    content = textwrap.dedent(f"""\
        pkgname={name}
        pkgver=1.0
        pkgrel=1
        depends=({deps_str})
        makedepends=({makedeps_str})

        build() {{
            true
        }}

        package() {{
            true
        }}
    """)
    (d / "PKGBUILD").write_text(content)
    return d / "PKGBUILD"


class TestResolveAurDeps:
    """Test the resolution algorithm with mocked externals."""

    def test_no_deps(self, tmp_path, monkeypatch):
        """Package with no deps returns empty list."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg")
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps", lambda specs: []
        )
        result = resolve_aur_deps(pkgbuild, None)
        assert result == []

    def test_all_installed(self, tmp_path, monkeypatch):
        """All deps installed → empty list."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg", depends=["foo", "bar"])
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps", lambda specs: []
        )
        result = resolve_aur_deps(pkgbuild, None)
        assert result == []

    def test_repo_deps_only(self, tmp_path, monkeypatch):
        """All missing deps are in repos → no AUR deps returned."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg", depends=["cmake", "git"])
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: ["cmake", "git"],
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(names),
        )
        result = resolve_aur_deps(pkgbuild, None)
        assert result == []

    def test_single_aur_dep(self, tmp_path, monkeypatch):
        """One AUR dep → resolved and returned."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg", depends=["aurpkg"])
        aur_pkgbuild = _make_pkgbuild(tmp_path, "aurpkg")

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: [s for s in specs if _strip_version(s) == "aurpkg"],
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names if n == "aurpkg"},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: aur_pkgbuild,
        )

        result = resolve_aur_deps(pkgbuild, None)
        assert len(result) == 1
        assert result[0].name == "aurpkg"
        assert result[0].source == "aur"
        assert result[0].depth == 0
        assert result[0].required_by == ["mypkg"]

    def test_transitive_aur_deps(self, tmp_path, monkeypatch):
        """A → B (AUR) → C (AUR): topo order is [C, B]."""
        pkg_a = _make_pkgbuild(tmp_path, "A", depends=["B"])
        pkg_b = _make_pkgbuild(tmp_path, "B", depends=["C"])
        pkg_c = _make_pkgbuild(tmp_path, "C")

        aur_set = {"B", "C"}

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: [s for s in specs if _strip_version(s) in aur_set],
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names if n in aur_set},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: tmp_path / name / "PKGBUILD",
        )

        result = resolve_aur_deps(pkg_a, None)
        names = [d.name for d in result]
        assert names == ["C", "B"]
        assert result[0].depth == 1
        assert result[0].required_by == ["B"]
        assert result[1].depth == 0
        assert result[1].required_by == ["A"]

    def test_cycle_detection(self, tmp_path, monkeypatch):
        """A → B → A cycle raises RuntimeError."""
        pkg_a = _make_pkgbuild(tmp_path, "A", depends=["B"])
        pkg_b = _make_pkgbuild(tmp_path, "B", depends=["A"])

        aur_set = {"A", "B"}

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: [s for s in specs if _strip_version(s) in aur_set],
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names if n in aur_set},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: tmp_path / name / "PKGBUILD",
        )

        with pytest.raises(RuntimeError, match="cycle"):
            resolve_aur_deps(pkg_a, None)

    def test_diamond_dedup(self, tmp_path, monkeypatch):
        """Diamond: A → {B, C}, B → D, C → D. D appears once."""
        _make_pkgbuild(tmp_path, "A", depends=["B", "C"])
        _make_pkgbuild(tmp_path, "B", depends=["D"])
        _make_pkgbuild(tmp_path, "C", depends=["D"])
        _make_pkgbuild(tmp_path, "D")

        aur_set = {"B", "C", "D"}

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: [s for s in specs if _strip_version(s) in aur_set],
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names if n in aur_set},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: tmp_path / name / "PKGBUILD",
        )

        pkg_a = tmp_path / "A" / "PKGBUILD"
        result = resolve_aur_deps(pkg_a, None)
        names = [d.name for d in result]

        # D must come before both B and C
        assert names.count("D") == 1
        assert names.index("D") < names.index("B")
        assert names.index("D") < names.index("C")

    def test_soname_deps_skipped(self, tmp_path, monkeypatch):
        """Soname deps like libfoo.so should not be queried as AUR packages."""
        pkgbuild = _make_pkgbuild(
            tmp_path, "mypkg", depends=["libfoo.so", "libbar.so=2", "realpackage"]
        )

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: specs,  # all missing
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        # Only "realpackage" should reach aur_info
        queried = []
        def mock_aur_info(names):
            queried.extend(names)
            return {n: {"Name": n} for n in names}
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info", mock_aur_info,
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: _make_pkgbuild(tmp_path, name),
        )

        result = resolve_aur_deps(pkgbuild, None)
        assert queried == ["realpackage"]
        assert len(result) == 1
        assert result[0].name == "realpackage"

    def test_version_constraints_stripped(self, tmp_path, monkeypatch):
        """Version constraints are stripped for classification."""
        pkgbuild = _make_pkgbuild(
            tmp_path, "mypkg", depends=["aurpkg>=2.0"]
        )
        aur_pkgbuild = _make_pkgbuild(tmp_path, "aurpkg")

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: specs,
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: aur_pkgbuild,
        )

        result = resolve_aur_deps(pkgbuild, None)
        assert len(result) == 1
        assert result[0].name == "aurpkg"

    def test_unknown_dep_warned(self, tmp_path, monkeypatch):
        """Deps not in repos or AUR are marked unknown."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg", depends=["ghost"])

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: specs,
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {},  # not found
        )

        result = resolve_aur_deps(pkgbuild, None)
        # Unknown deps are not returned by resolve_aur_deps (AUR-only)
        assert result == []

    def test_aur_rpc_fallback(self, tmp_path, monkeypatch):
        """When fetch=False, uses AUR RPC metadata instead of cloning."""
        pkgbuild = _make_pkgbuild(tmp_path, "mypkg", depends=["aurpkg"])

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            lambda specs: specs,
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: set(),
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {
                "aurpkg": {
                    "Name": "aurpkg",
                    "Depends": ["repodep"],
                    "MakeDepends": [],
                }
            },
        )

        # repodep should be classified as missing → repo
        call_count = {"repo_packages": 0}
        original_get_missing = lambda specs: specs

        def mock_get_missing(specs):
            stripped = [_strip_version(s) for s in specs]
            return specs  # all missing

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps",
            mock_get_missing,
        )

        def mock_repo_packages(names):
            call_count["repo_packages"] += 1
            if "repodep" in names:
                return {"repodep"}
            return set()

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            mock_repo_packages,
        )

        result = resolve_aur_deps(pkgbuild, None, fetch=False)
        assert len(result) == 1
        assert result[0].name == "aurpkg"
        assert result[0].pkgbuild_path is None  # no fetch


class TestResolveAllDeps:
    """Test resolve_all_deps which returns all dep types."""

    def test_mixed_deps(self, tmp_path, monkeypatch):
        """Returns AUR, repo, and installed deps."""
        pkgbuild = _make_pkgbuild(
            tmp_path, "mypkg", depends=["aurpkg", "repopkg", "installedpkg"]
        )
        aur_pkgbuild = _make_pkgbuild(tmp_path, "aurpkg")

        def mock_missing(specs):
            stripped = {_strip_version(s) for s in specs}
            return [s for s in specs if _strip_version(s) in {"aurpkg", "repopkg"}]

        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve._get_missing_deps", mock_missing,
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.repo_packages",
            lambda names: {n for n in names if n == "repopkg"},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.aur_info",
            lambda names: {n: {"Name": n} for n in names if n == "aurpkg"},
        )
        monkeypatch.setattr(
            "sysforge.primitives.aur_resolve.find_pkgbuild",
            lambda name, cfg: aur_pkgbuild,
        )

        result = resolve_all_deps(pkgbuild, None)
        sources = {d.name: d.source for d in result}
        assert sources["aurpkg"] == "aur"
        assert sources["repopkg"] == "repo"
        assert sources["installedpkg"] == "installed"
