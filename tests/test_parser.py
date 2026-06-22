"""
test_parser.py — smoke tests for the static PKGBUILD parser.

Verifies that parse_pkgbuild returns well-formed output for the sample
PKGBUILDs in tests/data/PKGBUILDs/. Does not test every field value — those
are covered by the integration tests in test_pipeline.py.
"""
from pathlib import Path

import pytest

from sysforge.primitives.pkgbuild_meta import (
    has_hardcoded_gcc,
    is_musl_static_build,
    parse_pkgbuild,
)

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


def test_brace_expansion_in_makedepends(tmp_path):
    """Unquoted bash brace lists expand to separate array items, matching how
    makepkg/bash sees them (regression for afdko's python-{build,installer,wheel}
    being treated as one bogus dependency)."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "makedepends=(cmake python-{build,installer,wheel} ninja)\n"
    )
    result = parse_pkgbuild(pkgbuild)
    md = result["globals"]["makedepends"]
    assert "python-build" in md
    assert "python-installer" in md
    assert "python-wheel" in md
    assert "python-{build,installer,wheel}" not in md
    assert md == ["cmake", "python-build", "python-installer", "python-wheel", "ninja"]


def test_brace_expansion_leaves_parameter_expansion_intact(tmp_path):
    """An unquoted ${...} must not be split on its internal punctuation — brace
    expansion never reaches into parameter expansions."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "depends=(lib-${_x} bare{a,b})\n"
    )
    result = parse_pkgbuild(pkgbuild)
    deps = result["globals"]["depends"]
    # ${_x} is unresolved → preserved verbatim, not brace-split.
    assert "lib-${_x}" in deps
    assert "barea" in deps and "bareb" in deps


def test_array_parameter_expansion_prefix(tmp_path):
    """``${arr[@]/#/python-}`` splices the referenced array, prefixing each item.

    Regression for afdko's ``depends=(... "${_pydeps[@]/#/python-}")``: the static
    parser used to leave a single bogus ``${_pydeps[@]/#/python-}`` token and drop
    python-ufonormalizer from the AUR dep graph, so a later ``makepkg --syncdeps``
    aborted with "target not found: python-ufonormalizer".
    """
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=afdko\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "_pydeps=(booleanoperations defcon ufonormalizer zopfli)\n"
        'depends=(python "${_pydeps[@]/#/python-}")\n'
    )
    deps = parse_pkgbuild(pkgbuild)["globals"]["depends"]
    assert "python-ufonormalizer" in deps
    assert deps == [
        "python",
        "python-booleanoperations",
        "python-defcon",
        "python-ufonormalizer",
        "python-zopfli",
    ]
    assert not any("${" in d for d in deps)


def test_array_parameter_expansion_plain(tmp_path):
    """``${arr[@]}`` with no transform splices the array elements verbatim."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "_common=(glibc gcc-libs)\n"
        'depends=("${_common[@]}" zlib)\n'
    )
    deps = parse_pkgbuild(pkgbuild)["globals"]["depends"]
    assert deps == ["glibc", "gcc-libs", "zlib"]


def test_array_parameter_expansion_suffix_and_replace(tmp_path):
    """``/%/SUFFIX`` appends, ``/PAT/REPL`` replaces, and ``[*]`` acts like ``[@]``."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "_mods=(comp settings)\n"
        "_libs=(libfoo libbar)\n"
        'makedepends=("${_mods[@]/%/-git}" "${_libs[*]/lib/lib32-}")\n'
    )
    md = parse_pkgbuild(pkgbuild)["globals"]["makedepends"]
    assert md == ["comp-git", "settings-git", "lib32-foo", "lib32-bar"]


def test_array_parameter_expansion_unknown_array_preserved(tmp_path):
    """A reference to an array we never captured is left verbatim (no garbage)."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        'depends=(zlib "${_missing[@]/#/python-}")\n'
    )
    deps = parse_pkgbuild(pkgbuild)["globals"]["depends"]
    assert "zlib" in deps
    # Preserved, not partially expanded — the resolver's RPC rescue handles it.
    assert "${_missing[@]/#/python-}" in deps


def test_array_parameter_expansion_unsupported_transform_preserved(tmp_path):
    """An unsupported transform (slice) leaves the token verbatim rather than guess."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "_a=(x y z)\n"
        'depends=("${_a[@]:1:2}")\n'
    )
    deps = parse_pkgbuild(pkgbuild)["globals"]["depends"]
    assert deps == ["${_a[@]:1:2}"]


def test_arch_specific_makedepends_merged(tmp_path):
    """makedepends_x86_64 entries merge into the canonical makedepends array.

    Without the merge, consumes inference (which only sees ``makedepends``)
    would miss ``lib32-rust`` declared under an arch-specific array, and the
    i686 rust cross-probe would never fire.
    """
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=lib32-foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "makedepends=('meson')\n"
        "makedepends_x86_64=('lib32-rust' 'lib32-glib2')\n"
    )
    result = parse_pkgbuild(pkgbuild)
    g = result["globals"]
    # Both plain and arch-specific entries appear in the canonical list.
    assert "meson" in g["makedepends"]
    assert "lib32-rust" in g["makedepends"]
    assert "lib32-glib2" in g["makedepends"]
    # The arch-specific key is preserved for callers that need it.
    assert g["makedepends_x86_64"] == ["lib32-rust", "lib32-glib2"]


def test_arch_specific_depends_merged(tmp_path):
    """depends_aarch64 merges into depends for the same reason."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64 aarch64)\n"
        "depends=('glibc')\n"
        "depends_aarch64=('libffi')\n"
    )
    result = parse_pkgbuild(pkgbuild)
    assert "glibc" in result["globals"]["depends"]
    assert "libffi" in result["globals"]["depends"]


def test_arch_specific_merge_deduplicates(tmp_path):
    """Entries appearing in both plain and arch-specific arrays appear once."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "makedepends=('rust' 'meson')\n"
        "makedepends_x86_64=('rust' 'lib32-rust')\n"
    )
    result = parse_pkgbuild(pkgbuild)
    md = result["globals"]["makedepends"]
    assert md.count("rust") == 1
    assert "meson" in md
    assert "lib32-rust" in md


def test_lib32_rust_stub_fixture_parses():
    """End-to-end: the lib32-rust-stub fixture must produce a makedepends list
    containing lib32-rust (declared only via makedepends_x86_64)."""
    result = parse_pkgbuild(PKGBUILDS_DIR / "lib32-rust-stub.PKGBUILD")
    md = result["globals"].get("makedepends", [])
    assert "meson" in md
    assert "lib32-rust" in md


# ---------------------------------------------------------------------------
# has_hardcoded_gcc
# ---------------------------------------------------------------------------

def _parse_with_build(tmp_path, body):
    """Helper: write a minimal PKGBUILD with a build() body and parse it."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "build() {\n"
        f"{body}\n"
        "}\n"
    )
    return parse_pkgbuild(pkgbuild)


def test_hardcoded_gcc_direct_gcc_invocation(tmp_path):
    parsed = _parse_with_build(tmp_path, "  gcc -O2 -o foo foo.c")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_direct_gxx_invocation(tmp_path):
    parsed = _parse_with_build(tmp_path, "  g++ -O2 -o foo foo.cpp")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_ccache_prefix(tmp_path):
    parsed = _parse_with_build(tmp_path, "  ccache g++ -O2 -o foo foo.cpp")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_make_with_cc_assignment(tmp_path):
    parsed = _parse_with_build(tmp_path, "  make CXX=g++ CC=gcc -j8")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_env_prefixed_assignment(tmp_path):
    parsed = _parse_with_build(tmp_path, "  CC=gcc make")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_respects_cxx_variable(tmp_path):
    parsed = _parse_with_build(tmp_path, '  $CXX -O2 -o foo foo.cpp')
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_cxx_braced(tmp_path):
    parsed = _parse_with_build(tmp_path, '  ${CXX} -O2 -o foo foo.cpp')
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_plain_make(tmp_path):
    parsed = _parse_with_build(tmp_path, "  make -j8")
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_ignores_lgcc_library(tmp_path):
    parsed = _parse_with_build(tmp_path, '  clang++ -o foo foo.cpp -lgcc_s')
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_ignores_libgcc_reference(tmp_path):
    parsed = _parse_with_build(tmp_path, '  cp libgcc.a /tmp/')
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_detects_in_package_function(tmp_path):
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "build() { make; }\n"
        'package() {\n'
        '  gcc -o helper helper.c\n'
        '  cp helper "$pkgdir/usr/bin/"\n'
        '}\n'
    )
    parsed = parse_pkgbuild(pkgbuild)
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_empty_parsed():
    assert has_hardcoded_gcc({}) is False
    assert has_hardcoded_gcc({"functions": {}}) is False
    assert has_hardcoded_gcc({"functions": {"build": ""}}) is False


def test_hardcoded_gcc_gpu_burn_style_makefile_not_in_pkgbuild(tmp_path):
    """
    Proactive detection is limited: gpu-burn's PKGBUILD just calls `make`
    while the Makefile itself hardcodes g++. Proactive path returns False;
    the reactive post-failure retry handles this case.
    """
    parsed = _parse_with_build(tmp_path, '  make')
    assert has_hardcoded_gcc(parsed) is False


# ---------------------------------------------------------------------------
# has_hardcoded_gcc — quoted assignment forms (lib32-* style PKGBUILDs)
# ---------------------------------------------------------------------------

def test_hardcoded_gcc_single_quoted_with_m32(tmp_path):
    """lib32-* PKGBUILD pattern: export CC='gcc -m32'."""
    parsed = _parse_with_build(tmp_path, "  export CC='gcc -m32'")
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_double_quoted(tmp_path):
    parsed = _parse_with_build(tmp_path, '  export CC="gcc"')
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_double_quoted_with_extra_flags(tmp_path):
    parsed = _parse_with_build(tmp_path, '  export CXX="g++ -fwhatever -m32"')
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_negative_substring_lgcc(tmp_path):
    """Word-boundary regression: -lgcc must not match."""
    parsed = _parse_with_build(tmp_path, '  $CC -O2 -o foo foo.c -lgcc')
    assert has_hardcoded_gcc(parsed) is False


def test_hardcoded_gcc_negative_var_dollar(tmp_path):
    parsed = _parse_with_build(tmp_path, '  CC=$gcc make')
    assert has_hardcoded_gcc(parsed) is False


# ---------------------------------------------------------------------------
# has_hardcoded_gcc — PKGBUILD(5) build-time function coverage
# ---------------------------------------------------------------------------

def test_hardcoded_gcc_in_check(tmp_path):
    """check() is a spec-defined build-time function — must be scanned."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "build() { make; }\n"
        "check() {\n"
        "  gcc -o test test.c\n"
        "  ./test\n"
        "}\n"
    )
    parsed = parse_pkgbuild(pkgbuild)
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_in_split_package_function(tmp_path):
    """package_<pkgname>() variants for split packages must be scanned."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgbase=foo\n"
        "pkgname=(foo foo-tools)\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "build() { make; }\n"
        "package_foo() { make install; }\n"
        "package_foo-tools() {\n"
        "  g++ -o helper helper.cpp\n"
        "}\n"
    )
    parsed = parse_pkgbuild(pkgbuild)
    assert has_hardcoded_gcc(parsed) is True


def test_hardcoded_gcc_verify_function_not_scanned(tmp_path):
    """
    verify() authenticates sources; never compiles. Even a contrived body
    that would otherwise match must not trigger detection — exercising the
    spec-derived function allowlist.
    """
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        "verify() {\n"
        "  CC=gcc make -C /tmp/check\n"
        "}\n"
        "build() { make; }\n"
    )
    parsed = parse_pkgbuild(pkgbuild)
    assert has_hardcoded_gcc(parsed) is False


# ---------------------------------------------------------------------------
# is_musl_static_build (pacman-static-style static-musl bootstraps)
# ---------------------------------------------------------------------------

def _parse_musl_pkgbuild(tmp_path, *, makedepends, build_body):
    """Write a PKGBUILD with the given makedepends array + build() body."""
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        "pkgname=foo\n"
        "pkgver=1.0\n"
        "pkgrel=1\n"
        "arch=(x86_64)\n"
        f"makedepends=({makedepends})\n"
        "build() {\n"
        f"{build_body}\n"
        "}\n"
    )
    return parse_pkgbuild(pkgbuild)


def test_musl_static_cc_musl_gcc(tmp_path):
    """pacman-static pattern: musl makedepend + export CC=musl-gcc."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'meson' 'cmake' 'musl' 'kernel-headers-musl'",
        build_body="  export CC=musl-gcc\n  make",
    )
    assert is_musl_static_build(parsed) is True


def test_musl_static_static_ldflags(tmp_path):
    """Detected via the -static LDFLAGS append alone (CC set elsewhere)."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'musl'",
        build_body='  export LDFLAGS="$LDFLAGS -static"\n  make',
    )
    assert is_musl_static_build(parsed) is True


def test_musl_static_quoted_cc_value(tmp_path):
    """export CC="musl-gcc -fno-stack-protector" form."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'kernel-headers-musl'",
        build_body='  export CC="musl-gcc -fno-stack-protector"\n  make',
    )
    assert is_musl_static_build(parsed) is True


def test_musl_static_requires_makedepend(tmp_path):
    """musl-gcc in body but no musl makedepend → not authoritative, False."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'meson' 'cmake'",
        build_body="  export CC=musl-gcc\n  make",
    )
    assert is_musl_static_build(parsed) is False


def test_musl_static_makedepend_without_build_signal(tmp_path):
    """musl makedepend but no CC=musl-gcc / -static in body → False."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'musl'",
        build_body="  make",
    )
    assert is_musl_static_build(parsed) is False


def test_musl_static_static_libgcc_not_matched(tmp_path):
    """Word-boundary: -static-libgcc must not trip the -static detector."""
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'musl'",
        build_body='  export LDFLAGS="$LDFLAGS -static-libgcc"\n  make',
    )
    assert is_musl_static_build(parsed) is False


def test_musl_static_plain_gcc_pkgbuild(tmp_path):
    parsed = _parse_musl_pkgbuild(
        tmp_path,
        makedepends="'cmake'",
        build_body="  gcc -O2 -o foo foo.c",
    )
    assert is_musl_static_build(parsed) is False


def test_musl_static_empty_parsed():
    assert is_musl_static_build({}) is False
    assert is_musl_static_build({"globals": {}, "functions": {}}) is False
