#!/usr/bin/env python3
"""
Unit tests for pkgbuild_patcher.py.

Covers:
  _expand_wl_token          — basic, multi-sub, single-sub, non-wl passthrough
  _tokenize_flag_value      — whitespace split + Wl expansion
  _strip_var_refs           — self-ref, cross-var-ref, complex bash expression
  _extract_flag_assignments — bare =, +=, export, self-ref strip, cross-ref skip,
                              complex bash skip, multi-key, multi-assign accumulation
  _extract_conditional_blocks — block with extractable key, block without,
                                 nested blocks, no conditionals
  extract_pkgbuild_profile  — real complex2.PKGBUILD function bodies
  write_extracted_profile   — TOML written, roundtrip via load_extracted_profile
  load_extracted_profile    — missing file returns {}
  apply_patch_pkgbuild      — flag lines removed, conditional blocks removed,
                               original untouched, groups line preserved
  cleanup_patch_artifacts   — removes both artifacts, missing files non-fatal
"""
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.pkgbuild_patcher import (
    _expand_wl_token,
    _tokenize_flag_value,
    _strip_var_refs,
    _extract_flag_assignments,
    _extract_conditional_blocks,
    extract_pkgbuild_profile,
    write_extracted_profile,
    load_extracted_profile,
    apply_patch_pkgbuild,
    cleanup_patch_artifacts,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

TESTS_DIR = Path(__file__).parent
COMPLEX2_PKGBUILD = TESTS_DIR / "data/PKGBUILDs/complex2.PKGBUILD"
COSMIC_PKGBUILD = TESTS_DIR / "data/PKGBUILDs/cosmic.PKGBUILD"


# ---------------------------------------------------------------------------
# _expand_wl_token
# ---------------------------------------------------------------------------

def test_expand_wl_multi():
    result = _expand_wl_token("-Wl,-O1,--sort-common,--as-needed")
    assert result == ["-Wl,-O1", "-Wl,--sort-common", "-Wl,--as-needed"]

def test_expand_wl_single():
    assert _expand_wl_token("-Wl,--as-needed") == ["-Wl,--as-needed"]

def test_expand_wl_z_relro():
    # -Wl,-z,relro → two separate -Wl, tokens (semantically equivalent)
    assert _expand_wl_token("-Wl,-z,relro") == ["-Wl,-z", "-Wl,relro"]

def test_expand_wl_non_wl_passthrough():
    assert _expand_wl_token("-pipe") == ["-pipe"]
    assert _expand_wl_token("-march=native") == ["-march=native"]

def test_expand_wl_gc_sections():
    assert _expand_wl_token("-Wl,--gc-sections") == ["-Wl,--gc-sections"]


# ---------------------------------------------------------------------------
# _tokenize_flag_value
# ---------------------------------------------------------------------------

def test_tokenize_basic():
    result = _tokenize_flag_value("-march=native -O2 -pipe")
    assert result == ["-march=native", "-O2", "-pipe"]

def test_tokenize_with_wl_expansion():
    result = _tokenize_flag_value("-march=native -O2 -Wl,-O1,--as-needed -pipe")
    assert result == ["-march=native", "-O2", "-Wl,-O1", "-Wl,--as-needed", "-pipe"]

def test_tokenize_empty():
    assert _tokenize_flag_value("") == []
    assert _tokenize_flag_value("   ") == []


# ---------------------------------------------------------------------------
# _strip_var_refs
# ---------------------------------------------------------------------------

def test_strip_self_ref():
    assert _strip_var_refs("$CFLAGS -fuse-ld=mold") == "-fuse-ld=mold"

def test_strip_brace_ref():
    assert _strip_var_refs("${CFLAGS} -fuse-ld=mold") == "-fuse-ld=mold"

def test_strip_cross_var_ref():
    # $CFLAGS in a CXXFLAGS value
    assert _strip_var_refs("$CFLAGS") == ""

def test_strip_complex_expression():
    # ${CFLAGS/-g /-g1 } — brace form stripped but $ may remain
    # Actually _strip_var_refs strips the whole ${...} so result is empty
    result = _strip_var_refs("${CFLAGS/-g /-g1 }")
    # No $ should remain after stripping ${...}
    assert "$" not in result

def test_strip_leaves_plain_flags():
    result = _strip_var_refs("-fno-stack-protector -m32")
    assert result == "-fno-stack-protector -m32"


# ---------------------------------------------------------------------------
# _extract_flag_assignments
# ---------------------------------------------------------------------------

def test_extract_bare_assignment():
    body = 'CFLAGS="-O2 -pipe"'
    result = _extract_flag_assignments(body)
    assert result.get("CFLAGS") == ["-O2", "-pipe"]

def test_extract_export_assignment():
    body = 'export CFLAGS="-O3 -march=native"'
    result = _extract_flag_assignments(body)
    assert result.get("CFLAGS") == ["-O3", "-march=native"]

def test_extract_append():
    body = 'export CFLAGS+=" -fno-stack-protector"'
    result = _extract_flag_assignments(body)
    assert result.get("CFLAGS") == ["-fno-stack-protector"]

def test_extract_multi_append_accumulates():
    body = 'export CFLAGS+=" -fno-stack-protector"\nexport CFLAGS+=" -m32"'
    result = _extract_flag_assignments(body)
    assert result.get("CFLAGS") == ["-fno-stack-protector", "-m32"]

def test_extract_assign_resets():
    body = 'CFLAGS="-O2"\nCFLAGS="-O3"'
    result = _extract_flag_assignments(body)
    assert result.get("CFLAGS") == ["-O3"]

def test_extract_cross_ref_skipped():
    # CXXFLAGS="$CFLAGS" — $CFLAGS stripped → empty → no tokens → skipped
    body = 'export CXXFLAGS="$CFLAGS"'
    result = _extract_flag_assignments(body)
    assert "CXXFLAGS" not in result

def test_extract_complex_bash_skipped():
    # ${CFLAGS/-g /-g1 } — complex expression, skipped but logged
    body = "CFLAGS=${CFLAGS/-g /-g1 }"
    result = _extract_flag_assignments(body)
    assert "CFLAGS" not in result

def test_extract_wl_expanded():
    body = 'export LDFLAGS+=" -Wl,--gc-sections"'
    result = _extract_flag_assignments(body)
    assert result.get("LDFLAGS") == ["-Wl,--gc-sections"]

def test_extract_wl_packed_expanded():
    body = 'LDFLAGS="-Wl,-O1,--sort-common,--as-needed"'
    result = _extract_flag_assignments(body)
    assert result.get("LDFLAGS") == ["-Wl,-O1", "-Wl,--sort-common", "-Wl,--as-needed"]

def test_extract_non_extractable_key_ignored():
    body = 'SOME_RANDOM_VAR="hello"'
    result = _extract_flag_assignments(body)
    assert "SOME_RANDOM_VAR" not in result

def test_extract_rustflags():
    body = 'RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"'
    result = _extract_flag_assignments(body)
    assert result.get("RUSTFLAGS") == ["-C", "link-arg=-fuse-ld=mold"]


# ---------------------------------------------------------------------------
# _extract_conditional_blocks
# ---------------------------------------------------------------------------

def test_conditional_with_extractable_key():
    body = 'if [[ $CARCH == x86_64 ]]; then\n  CFLAGS+=" -m32"\nfi\n'
    blocks = _extract_conditional_blocks(body)
    assert len(blocks) == 1
    _, _, keys, _ = blocks[0]
    assert "CFLAGS" in keys

def test_conditional_without_extractable_key():
    body = 'if [[ -f something ]]; then\n  echo "hi"\nfi\n'
    blocks = _extract_conditional_blocks(body)
    assert blocks == []

def test_conditional_no_blocks():
    body = 'CFLAGS="-O2"\nexport LDFLAGS="-Wl,--as-needed"\n'
    assert _extract_conditional_blocks(body) == []

def test_conditional_nested_blocks():
    body = (
        'if [[ $A ]]; then\n'
        '  if [[ $B ]]; then\n'
        '    CFLAGS+=" -m32"\n'
        '  fi\n'
        'fi\n'
    )
    # Outer block contains the extractable key; inner is nested inside it
    blocks = _extract_conditional_blocks(body)
    # Should detect one outer block (depth tracking prevents double-counting)
    assert len(blocks) == 1
    _, _, keys, _ = blocks[0]
    assert "CFLAGS" in keys

def test_conditional_block_spans_correct():
    body = 'before\nif true; then\n  CFLAGS+=" -m32"\nfi\nafter\n'
    blocks = _extract_conditional_blocks(body)
    assert len(blocks) == 1
    start, end, _, block_text = blocks[0]
    assert "if true" in block_text
    assert "fi" in block_text
    assert "before" not in block_text
    assert "after" not in block_text


# ---------------------------------------------------------------------------
# extract_pkgbuild_profile (integration — real PKGBUILD)
# ---------------------------------------------------------------------------

def test_extract_complex2_pkgbuild():
    """
    complex2.PKGBUILD (lib32-llvm) build() function contains:
      export CFLAGS+=" -fno-stack-protector"
      export CXXFLAGS="$CFLAGS"           <- cross-ref, skipped
      export LDFLAGS+=" -Wl,--gc-sections"
      CFLAGS=${CFLAGS/-g /-g1 }           <- complex, skipped
      CXXFLAGS=${CXXFLAGS/-g /-g1 }       <- complex, skipped
      export CFLAGS+=" -m32"
      export CXXFLAGS+=" -m32"
    """
    pkgmeta = parse_pkgbuild(COMPLEX2_PKGBUILD)
    profile = extract_pkgbuild_profile(pkgmeta, COMPLEX2_PKGBUILD)

    # CFLAGS: two += appends (-fno-stack-protector, -m32)
    assert "CFLAGS" in profile
    cflags_tokens = profile["CFLAGS"].split()
    assert "-fno-stack-protector" in cflags_tokens
    assert "-m32" in cflags_tokens

    # LDFLAGS: -Wl,--gc-sections (expanded from -Wl,--gc-sections)
    assert "LDFLAGS" in profile
    assert "-Wl,--gc-sections" in profile["LDFLAGS"].split()

    # CXXFLAGS: only -m32 (the $CFLAGS cross-ref is skipped)
    assert "CXXFLAGS" in profile
    assert "-m32" in profile["CXXFLAGS"].split()

def test_extract_cosmic_pkgbuild():
    """
    cosmic.PKGBUILD build() function contains:
      RUSTFLAGS+=" -C link-arg=-fuse-ld=mold"
    """
    pkgmeta = parse_pkgbuild(COSMIC_PKGBUILD)
    profile = extract_pkgbuild_profile(pkgmeta, COSMIC_PKGBUILD)

    assert "RUSTFLAGS" in profile
    tokens = profile["RUSTFLAGS"].split()
    assert "-C" in tokens
    assert "link-arg=-fuse-ld=mold" in tokens

def test_extract_empty_pkgbuild():
    """A PKGBUILD with no extractable flags returns an empty dict."""
    pkgmeta = parse_pkgbuild(TESTS_DIR / "data/PKGBUILDs/htop.PKGBUILD")
    profile = extract_pkgbuild_profile(pkgmeta, TESTS_DIR / "data/PKGBUILDs/htop.PKGBUILD")
    # Strip metadata keys
    clean = {k: v for k, v in profile.items() if not k.startswith("__")}
    assert clean == {}


# ---------------------------------------------------------------------------
# write_extracted_profile / load_extracted_profile
# ---------------------------------------------------------------------------

def test_write_and_load_roundtrip():
    profile = {"CFLAGS": "-fno-stack-protector -m32", "LDFLAGS": "-Wl,--gc-sections"}
    with tempfile.TemporaryDirectory() as d:
        pkgbuild_path = Path(d) / "PKGBUILD"
        pkgbuild_path.touch()
        out = write_extracted_profile(profile, pkgbuild_path)
        assert out.exists()
        loaded = load_extracted_profile(pkgbuild_path)
    assert loaded["CFLAGS"] == "-fno-stack-protector -m32"
    assert loaded["LDFLAGS"] == "-Wl,--gc-sections"

def test_load_missing_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        pkgbuild_path = Path(d) / "PKGBUILD"
        result = load_extracted_profile(pkgbuild_path)
    assert result == {}

def test_write_profile_toml_format():
    """Verify TOML file has the expected section header."""
    profile = {"CFLAGS": "-O3"}
    with tempfile.TemporaryDirectory() as d:
        pkgbuild_path = Path(d) / "PKGBUILD"
        pkgbuild_path.touch()
        out = write_extracted_profile(profile, pkgbuild_path)
        content = out.read_text()
    assert "[profiles.pkgbuild_extracted]" in content
    assert 'CFLAGS = "-O3"' in content

def test_write_excludes_metadata_keys():
    """Internal __ keys must not appear in the written TOML."""
    profile = {
        "CFLAGS": "-O3",
        "__conditional_blocks__": [],
        "__skipped_lines__": [],
    }
    with tempfile.TemporaryDirectory() as d:
        pkgbuild_path = Path(d) / "PKGBUILD"
        pkgbuild_path.touch()
        out = write_extracted_profile(profile, pkgbuild_path)
        content = out.read_text()
    assert "__" not in content


# ---------------------------------------------------------------------------
# apply_patch_pkgbuild
# ---------------------------------------------------------------------------

SIMPLE_PKGBUILD = """\
pkgname=mypkg
pkgver=1.0
pkgrel=1
groups=('mygroup')

build() {
  export CFLAGS+=" -fno-stack-protector"
  export LDFLAGS+=" -Wl,--gc-sections"
  make
}
"""

COND_PKGBUILD = """\
pkgname=mypkg
pkgver=1.0
pkgrel=1

build() {
  if [[ $CARCH == x86_64 ]]; then
    CFLAGS+=" -m32"
  fi
  make
}
"""

def _make_pkgbuild(d, content):
    p = Path(d) / "PKGBUILD"
    p.write_text(content)
    return p

def test_apply_removes_flag_lines():
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, SIMPLE_PKGBUILD)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        patched = apply_patch_pkgbuild(pb, pkgmeta)
        content = patched.read_text()
    assert "export CFLAGS" not in content
    assert "export LDFLAGS" not in content
    assert "make" in content  # non-flag lines preserved

def test_apply_preserves_original():
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, SIMPLE_PKGBUILD)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        apply_patch_pkgbuild(pb, pkgmeta)
        original_content = pb.read_text()
    assert "export CFLAGS" in original_content  # original untouched

def test_apply_removes_conditional_block():
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, COND_PKGBUILD)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        patched = apply_patch_pkgbuild(pb, pkgmeta)
        content = patched.read_text()
    assert "if [[ $CARCH" not in content
    assert "fi" not in content
    assert "make" in content

def test_apply_preserves_groups():
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, SIMPLE_PKGBUILD)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        patched = apply_patch_pkgbuild(pb, pkgmeta)
        content = patched.read_text()
    assert "groups=('mygroup')" in content

def test_apply_writes_sysforge_copy():
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, SIMPLE_PKGBUILD)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        patched = apply_patch_pkgbuild(pb, pkgmeta)
    assert patched.name == "PKGBUILD.sysforge"


# ---------------------------------------------------------------------------
# cleanup_patch_artifacts
# ---------------------------------------------------------------------------

def test_cleanup_removes_both_artifacts():
    with tempfile.TemporaryDirectory() as d:
        pb = Path(d) / "PKGBUILD"
        pb.touch()
        sysforge = Path(d) / "PKGBUILD.sysforge"
        sysforge.touch()
        toml = Path(d) / "pkgbuild_extracted_profile.toml"
        toml.touch()
        cleanup_patch_artifacts(pb)
        assert not sysforge.exists()
        assert not toml.exists()

def test_cleanup_missing_files_nonfatal():
    with tempfile.TemporaryDirectory() as d:
        pb = Path(d) / "PKGBUILD"
        pb.touch()
        # Neither artifact exists — should not raise
        cleanup_patch_artifacts(pb)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
