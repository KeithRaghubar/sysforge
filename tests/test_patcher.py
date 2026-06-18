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
    patch_kernel_kconfig_apply,
    patch_noninteractive_kconfig,
    patch_pkgbuild_groups,
    patch_subshell_env_reset,
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

_PKGBUILD_DATA = Path(__file__).parent / "data" / "PKGBUILDs"
SIMPLE_PKGBUILD = (_PKGBUILD_DATA / "patcher-simple.PKGBUILD").read_text()
COND_PKGBUILD = (_PKGBUILD_DATA / "conditional.PKGBUILD").read_text()

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

def test_apply_preserves_non_managed_make_invocations():
    """make LOCALVERSION=... and make INSTALL_MOD_PATH=... must NOT be removed — they
    are real kernel build commands, not flag assignments sysforge manages."""
    pkgbuild = (_PKGBUILD_DATA / "kernel-make.PKGBUILD").read_text()
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, pkgbuild)
        pkgmeta = {
            "globals": {"pkgbase": "linux-custom", "pkgname": ["linux-custom", "linux-custom-headers"]},
            "functions": {},
        }
        patched = apply_patch_pkgbuild(pb, pkgmeta)
        content = patched.read_text()
    assert 'make LOCALVERSION=' in content
    assert 'make INSTALL_MOD_PATH=' in content

def test_apply_removes_managed_inline_make():
    """make CFLAGS=... should still be removed (it's in _EXTRACTABLE_KEYS)."""
    pkgbuild = (_PKGBUILD_DATA / "make-cflags.PKGBUILD").read_text()
    with tempfile.TemporaryDirectory() as d:
        pb = _make_pkgbuild(d, pkgbuild)
        pkgmeta = {"globals": {"pkgname": "mypkg"}, "functions": {}}
        patched = apply_patch_pkgbuild(pb, pkgmeta)
        content = patched.read_text()
    assert 'make CFLAGS=' not in content       # managed key — removed
    assert 'make LOCALVERSION=' in content     # unmanaged key — preserved


# ---------------------------------------------------------------------------
# patch_noninteractive_kconfig
# ---------------------------------------------------------------------------

def test_patch_noninteractive_kconfig_replaces_oldconfig(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make oldconfig\n")
    patch_noninteractive_kconfig(pb)
    assert pb.read_text() == "  make olddefconfig\n"

def test_patch_noninteractive_kconfig_replaces_nconfig(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make nconfig\n")
    patch_noninteractive_kconfig(pb)
    assert "olddefconfig" in pb.read_text()
    assert "nconfig" not in pb.read_text()

def test_patch_noninteractive_kconfig_replaces_menuconfig(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make menuconfig\n")
    patch_noninteractive_kconfig(pb)
    assert "olddefconfig" in pb.read_text()

def test_patch_noninteractive_kconfig_preserves_var_args(tmp_path):
    """VAR=val arguments before the target are preserved."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make ARCH=x86_64 oldconfig\n")
    patch_noninteractive_kconfig(pb)
    content = pb.read_text()
    assert "ARCH=x86_64" in content
    assert "olddefconfig" in content
    assert "oldconfig" not in content.replace("olddefconfig", "")

def test_patch_noninteractive_kconfig_preserves_comment(tmp_path):
    """Trailing comments are preserved."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make oldconfig # configure\n")
    patch_noninteractive_kconfig(pb)
    content = pb.read_text()
    assert "# configure" in content
    assert "olddefconfig" in content

def test_patch_noninteractive_kconfig_no_match_is_noop(tmp_path):
    """File with no interactive targets is unchanged."""
    original = "  make olddefconfig\n  make modules_install\n"
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(original)
    patch_noninteractive_kconfig(pb)
    assert pb.read_text() == original

def test_patch_noninteractive_kconfig_multiple_targets(tmp_path):
    """All interactive targets in the file are replaced."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make oldconfig\n  make nconfig\n")
    patch_noninteractive_kconfig(pb)
    content = pb.read_text()
    assert content.count("olddefconfig") == 2
    assert "nconfig" not in content
    assert "oldconfig" not in content.replace("olddefconfig", "")

def test_patch_noninteractive_kconfig_preserves_non_kconfig_make(tmp_path):
    """Other make invocations are untouched."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("  make oldconfig\n  make LOCALVERSION=v1 all\n  make modules_install\n")
    patch_noninteractive_kconfig(pb)
    content = pb.read_text()
    assert "olddefconfig" in content
    assert "make LOCALVERSION=v1 all" in content
    assert "make modules_install" in content


# ---------------------------------------------------------------------------
# patch_kernel_kconfig_apply (fragment merge + interactive nconfig injection)
# ---------------------------------------------------------------------------

_STOCK_PREPARE = (
    "prepare() {\n"
    "  cd $_srcname\n"
    "  cp ../config.$CARCH .config\n"
    "  make olddefconfig\n"
    "  make -s kernelrelease > version\n"
    "}\n"
)


def test_kconfig_apply_injects_merge_and_nconfig_when_interactive(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(_STOCK_PREPARE)
    patch_kernel_kconfig_apply(pb, interactive=True)
    text = pb.read_text()
    assert "merge_config.sh -m .config \"$startdir/sysforge.config\"" in text
    assert "make nconfig" in text
    # Merge is injected after the anchor (make olddefconfig), before nconfig.
    assert text.index("make olddefconfig") < text.index("merge_config.sh")
    assert text.index("merge_config.sh") < text.index("make nconfig")


def test_kconfig_apply_injects_merge_without_nconfig_when_noninteractive(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(_STOCK_PREPARE)
    patch_kernel_kconfig_apply(pb, interactive=False)
    text = pb.read_text()
    assert "merge_config.sh" in text
    assert "make nconfig" not in text


def test_kconfig_apply_skips_sysforge_aware_pkgbuild(tmp_path):
    """A PKGBUILD that already calls merge_config.sh is left untouched."""
    aware = (
        "prepare() {\n"
        "  cd $_srcname\n"
        "  cp ../config.$CARCH .config\n"
        "  ./scripts/kconfig/merge_config.sh -m .config ../sysforge.config\n"
        "  make olddefconfig\n"
        "}\n"
    )
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(aware)
    patch_kernel_kconfig_apply(pb, interactive=True)
    assert pb.read_text() == aware  # no injection


def test_kconfig_apply_does_not_duplicate_existing_interactive_target(tmp_path):
    """If the PKGBUILD already has an interactive target, don't add a 2nd one —
    just inject the fragment merge before it."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(
        "prepare() {\n"
        "  cp ../config.$CARCH .config\n"
        "  make olddefconfig\n"
        "  make nconfig\n"
        "}\n"
    )
    patch_kernel_kconfig_apply(pb, interactive=True)
    text = pb.read_text()
    assert text.count("make nconfig") == 1
    assert "merge_config.sh" in text


def test_kconfig_apply_falls_back_to_config_seed_anchor(tmp_path):
    """No make-config line, but a `cp … .config` seed is a valid anchor."""
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(
        "prepare() {\n"
        "  cp ../config.$CARCH .config\n"
        "  make -s kernelrelease > version\n"
        "}\n"
    )
    patch_kernel_kconfig_apply(pb, interactive=False)
    text = pb.read_text()
    assert "merge_config.sh" in text
    # injected right after the .config seed
    assert text.index(".config\n") < text.index("merge_config.sh")


def test_kconfig_apply_no_anchor_is_noop(tmp_path):
    """No anchor at all → no injection (warned), file unchanged."""
    original = "prepare() {\n  echo nothing to configure\n}\n"
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text(original)
    patch_kernel_kconfig_apply(pb, interactive=True)
    assert pb.read_text() == original


def test_kconfig_apply_preserves_anchor_indentation(tmp_path):
    pb = tmp_path / "PKGBUILD.sysforge"
    pb.write_text("prepare() {\n\tmake olddefconfig\n}\n")
    patch_kernel_kconfig_apply(pb, interactive=False)
    text = pb.read_text()
    # injected block carries the anchor's tab indentation
    assert "\n\tif [ -f \"$startdir/sysforge.config\" ]; then" in text


# ---------------------------------------------------------------------------
# patch_pkgbuild_groups
# ---------------------------------------------------------------------------

def test_patch_groups_inline_pkgname():
    """Single-line pkgname=foo — groups inserted on the line immediately after."""
    with tempfile.TemporaryDirectory() as d:
        pb = Path(d) / "PKGBUILD"
        pb.write_text("pkgname=htop\npkgver=1.0\n")
        result = patch_pkgbuild_groups(pb, ["sf-build"])
        lines = result.read_text().splitlines()
    assert lines[0] == "pkgname=htop"
    assert lines[1] == 'groups=("sf-build")'
    assert lines[2] == "pkgver=1.0"


def test_patch_groups_multiline_pkgname():
    """Multi-line pkgname=(\n  ...\n) — groups must appear AFTER the closing ), not inside."""
    with tempfile.TemporaryDirectory() as d:
        pb = Path(d) / "PKGBUILD"
        pb.write_text("pkgname=(\n  gcc\n  gcc-libs\n)\npkgver=14.0\n")
        result = patch_pkgbuild_groups(pb, ["sf-build"])
        text = result.read_text()
    # groups must not appear inside the pkgname array
    assert "(\n  gcc\n  gcc-libs\n)\n" in text
    groups_pos = text.index("groups=")
    close_paren_pos = text.index(")\n")
    assert groups_pos > close_paren_pos, "groups= must come after the closing ) of pkgname"


def test_patch_groups_replaces_existing():
    """Existing groups=(...) is replaced, not duplicated."""
    with tempfile.TemporaryDirectory() as d:
        pb = Path(d) / "PKGBUILD"
        pb.write_text('pkgname=htop\ngroups=("old-group")\npkgver=1.0\n')
        result = patch_pkgbuild_groups(pb, ["sf-build"])
        text = result.read_text()
    assert text.count("groups=") == 1
    assert 'groups=("sf-build")' in text
    assert "old-group" not in text


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
# patch_subshell_env_reset
# ---------------------------------------------------------------------------

def test_subshell_env_reset_injects_unset():
    """Subshell functions get 'unset CC CXX' when profile sets CC=clang."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        p.write_text(
            "_build_musl32() (\n"
            "  echo musl\n"
            "  ./configure --prefix=foo\n"
            ")\n"
            "\n"
            "build() {\n"
            "  _build_musl32\n"
            "}\n"
        )
        count = patch_subshell_env_reset(p, {"CC": "clang", "CXX": "clang++"})
        assert count == 1
        text = p.read_text()
        assert "unset CC CXX" in text
        # unset should be inside the subshell function, not in build()
        lines = text.splitlines()
        unset_idx = next(i for i, ln in enumerate(lines) if "unset CC CXX" in ln)
        func_idx = next(
            i for i, ln in enumerate(lines) if "_build_musl32" in ln and "(" in ln)
        assert unset_idx == func_idx + 1


def test_subshell_env_reset_multiple_functions():
    """All subshell functions get the reset."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        p.write_text(
            "_build_a() (\n  echo a\n)\n\n"
            "_build_b() (\n  echo b\n)\n\n"
            "build() {\n  _build_a\n  _build_b\n}\n"
        )
        count = patch_subshell_env_reset(p, {"CC": "clang"})
        assert count == 2
        assert p.read_text().count("unset CC") == 2


def test_subshell_env_reset_skips_brace_functions():
    """Regular brace functions are not touched."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        original = "build() {\n  make\n}\n"
        p.write_text(original)
        count = patch_subshell_env_reset(p, {"CC": "clang"})
        assert count == 0
        assert p.read_text() == original


def test_subshell_env_reset_noop_for_gcc():
    """No reset injected when profile already uses gcc."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        original = "_helper() (\n  echo hi\n)\n"
        p.write_text(original)
        count = patch_subshell_env_reset(p, {"CC": "gcc", "CXX": "g++"})
        assert count == 0
        assert p.read_text() == original


def test_subshell_env_reset_empty_toolchain():
    """No reset when toolchain_env is empty."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        original = "_helper() (\n  echo hi\n)\n"
        p.write_text(original)
        count = patch_subshell_env_reset(p, {})
        assert count == 0
        assert p.read_text() == original


def test_subshell_env_reset_only_cc():
    """Only CC is unset when only CC differs from default."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        p.write_text("_helper() (\n  echo hi\n)\n")
        patch_subshell_env_reset(p, {"CC": "clang", "CXX": "g++"})
        text = p.read_text()
        assert "unset CC\n" in text
        assert "CXX" not in text


def test_subshell_env_reset_inherited_ld():
    """LD=ld.lld from inherited env is also unset in subshell functions."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        p.write_text("_build_wimboot() (\n  make\n)\n")
        count = patch_subshell_env_reset(
            p, {"CC": "clang"}, inherited_env={"LD": "ld.lld"}
        )
        assert count == 1
        text = p.read_text()
        assert "CC" in text
        assert "LD" in text


def test_subshell_env_reset_inherited_ld_only():
    """LD from inherited env triggers reset even when profile CC is default."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        p.write_text("_helper() (\n  echo hi\n)\n")
        count = patch_subshell_env_reset(
            p, {"CC": "gcc"}, inherited_env={"LD": "ld.lld"}
        )
        assert count == 1
        text = p.read_text()
        assert "LD" in text
        # CC is default, so should not be in unset
        assert "CC" not in text.split("unset")[1]


def test_subshell_env_reset_inherited_default_ld():
    """LD=ld (the default) should NOT trigger a reset."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "PKGBUILD.sysforge"
        original = "_helper() (\n  echo hi\n)\n"
        p.write_text(original)
        count = patch_subshell_env_reset(
            p, {"CC": "gcc"}, inherited_env={"LD": "ld"}
        )
        assert count == 0
        assert p.read_text() == original


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
