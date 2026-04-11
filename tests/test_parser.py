"""
test_parser.py — smoke tests for the static PKGBUILD parser.

Verifies that parse_pkgbuild returns well-formed output for the sample
PKGBUILDs in tests/data/PKGBUILDs/. Does not test every field value — those
are covered by the integration tests in test_pipeline.py.
"""
from pathlib import Path

import pytest

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

TESTS_DIR = Path(__file__).parent
PKGBUILDS_DIR = TESTS_DIR / "data/PKGBUILDs"

SAMPLES = [
    "htop.PKGBUILD",
    "llvm.PKGBUILD",
    "lib32-llvm.PKGBUILD",
    "complex2.PKGBUILD",
    "cosmic.PKGBUILD",
    "vulkan-headers-git.PKGBUILD",
]


@pytest.mark.parametrize("filename", SAMPLES)
def test_parse_returns_globals_and_functions(filename):
    result = parse_pkgbuild(PKGBUILDS_DIR / filename)
    assert "globals" in result
    assert "functions" in result
    assert isinstance(result["globals"], dict)
    assert isinstance(result["functions"], dict)


@pytest.mark.parametrize("filename", SAMPLES)
def test_parse_has_pkgname(filename):
    result = parse_pkgbuild(PKGBUILDS_DIR / filename)
    assert "pkgname" in result["globals"]


def test_htop_globals():
    result = parse_pkgbuild(PKGBUILDS_DIR / "htop.PKGBUILD")
    g = result["globals"]
    assert g["pkgname"] == "htop"
    assert "git" in g.get("makedepends", [])
    assert "ncurses" in g.get("depends", [])


def test_complex2_has_build_function():
    result = parse_pkgbuild(PKGBUILDS_DIR / "complex2.PKGBUILD")
    assert "build" in result["functions"]
    assert len(result["functions"]["build"]) > 0


def test_complex2_split_pkgname():
    result = parse_pkgbuild(PKGBUILDS_DIR / "complex2.PKGBUILD")
    pkgname = result["globals"]["pkgname"]
    # split package — pkgname is a list
    assert isinstance(pkgname, list)
    assert "lib32-llvm" in pkgname


def test_cosmic_makedepends_has_cargo():
    result = parse_pkgbuild(PKGBUILDS_DIR / "cosmic.PKGBUILD")
    assert "cargo" in result["globals"].get("makedepends", [])


def test_unquoted_array_items_parsed():
    """PKGBUILDs like gcc use unquoted items on separate lines — all must be captured."""
    result = parse_pkgbuild(PKGBUILDS_DIR / "gcc-split.PKGBUILD")
    pkgname = result["globals"]["pkgname"]
    assert isinstance(pkgname, list)
    assert "gcc" in pkgname
    assert "gcc-libs" in pkgname
    assert "gcc-fortran" in pkgname
    assert "gcc-ada" in pkgname


def test_pkgname_variable_expansion_scalar():
    """PKGBUILDs that define pkgname via a shell var must resolve to the real name."""
    result = parse_pkgbuild(PKGBUILDS_DIR / "vulkan-headers-git.PKGBUILD")
    # _pkgname=vulkan-headers; pkgname=$_pkgname-git → should be vulkan-headers-git
    assert result["globals"]["pkgname"] == "vulkan-headers-git"


def test_array_variable_expansion(tmp_path):
    """Array items referencing scalar globals should be expanded in place."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgbase=linux-custom\n"
        'pkgname=("$pkgbase" "$pkgbase-headers")\n'
        "pkgver=6.19.9.arch1\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
    )
    result = parse_pkgbuild(pkgbuild)
    assert result["globals"]["pkgname"] == ["linux-custom", "linux-custom-headers"]
    assert result["globals"]["pkgbase"] == "linux-custom"


def test_unresolved_variable_preserved(tmp_path):
    """References we cannot resolve should be left alone, not wiped."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        'pkgname=foo\n'
        'pkgver=1.0\n'
        'pkgrel=1\n'
        'arch=(x86_64)\n'
        'source=("$pkgname-$pkgver.tar.gz::https://example.com/$unknown_var/file")\n'
    )
    result = parse_pkgbuild(pkgbuild)
    src = result["globals"]["source"][0]
    assert src.startswith("foo-1.0.tar.gz::")
    assert "$unknown_var" in src  # unresolved refs preserved verbatim


def test_parameter_expansion_not_mangled(tmp_path):
    """${var:-default} and similar forms must not be touched."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        'pkgname=foo\n'
        'pkgver=1.0\n'
        'pkgrel=1\n'
        'arch=(x86_64)\n'
        '_flags=("${CFLAGS:-default}" "${pkgname%-git}")\n'
    )
    result = parse_pkgbuild(pkgbuild)
    flags = result["globals"]["_flags"]
    assert "${CFLAGS:-default}" in flags
    assert "${pkgname%-git}" in flags
