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
