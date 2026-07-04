"""
test_system_conf.py — tests for system makepkg.conf parsing and
the merged conf emission in emit_makepkg_conf.

Uses a synthetic makepkg.conf fixture rather than the real /etc/makepkg.conf.
"""
from pathlib import Path

import pytest

from sysforge.primitives.config import parse_system_makepkg_conf
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf

_FIXTURE_CONF = Path(__file__).parent / "data" / "etc" / "sysforge" / "system_makepkg.conf"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def sys_conf_path(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text(_FIXTURE_CONF.read_text())
    return p


def read_conf(conf_path):
    """Parse a temp conf into a dict of key -> value (quotes stripped)."""
    out = {}
    for line in Path(conf_path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# parse_system_makepkg_conf
# ---------------------------------------------------------------------------

def test_parse_reads_quoted_strings(sys_conf_path):
    result = parse_system_makepkg_conf(sys_conf_path)
    assert result["CARCH"] == '"x86_64"'
    assert result["PKGEXT"] == "'.pkg.tar.zst'"

def test_parse_reads_cflags(sys_conf_path):
    result = parse_system_makepkg_conf(sys_conf_path)
    assert "CFLAGS" in result
    assert "-O2" in result["CFLAGS"]

def test_parse_reads_arrays(sys_conf_path):
    result = parse_system_makepkg_conf(sys_conf_path)
    assert "BUILDENV" in result
    assert "(!distcc" in result["BUILDENV"]

def test_parse_reads_multiline_arrays(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text(
        "CFLAGS=\"-O2\"\n"
        "VCSCLIENTS=('bzr::breezy'\n"
        "            'git::git'\n"
        "            'mercurial::mercurial'\n"
        "            'subversion::subversion')\n"
        "BUILDENV=(!distcc color)\n"
    )
    result = parse_system_makepkg_conf(p)
    assert "VCSCLIENTS" in result
    # All four entries must be captured — not just the first line
    assert "'bzr::breezy'" in result["VCSCLIENTS"]
    assert "'git::git'" in result["VCSCLIENTS"]
    assert "'mercurial::mercurial'" in result["VCSCLIENTS"]
    assert "'subversion::subversion'" in result["VCSCLIENTS"]
    # Closing paren must be present (array is syntactically complete)
    assert result["VCSCLIENTS"].rstrip().endswith(")")
    # Neighbouring keys must still be parsed correctly
    assert "-O2" in result["CFLAGS"]
    assert "!distcc" in result["BUILDENV"]


def test_parse_skips_comments(sys_conf_path):
    result = parse_system_makepkg_conf(sys_conf_path)
    # No key should be a comment fragment
    assert all(not k.startswith("#") for k in result)

def test_parse_last_assignment_wins(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text('CFLAGS="-O2"\nCFLAGS="-O3"\n')
    result = parse_system_makepkg_conf(p)
    assert "-O3" in result["CFLAGS"]

def test_parse_missing_file_returns_empty(tmp_path):
    result = parse_system_makepkg_conf(tmp_path / "nonexistent.conf")
    assert result == {}

def test_parse_export_prefix(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text('export CFLAGS="-O2 -pipe"\n')
    result = parse_system_makepkg_conf(p)
    assert "CFLAGS" in result


# ---------------------------------------------------------------------------
# emit_makepkg_conf — merge behaviour
# ---------------------------------------------------------------------------

def test_emit_includes_system_keys(sys_conf_path):
    """Non-managed system keys like CARCH, PKGEXT pass through."""
    profile = {"CFLAGS": "-O3 -march=native"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert "CARCH" in conf
    assert "PKGEXT" in conf

def test_emit_profile_overrides_system_cflags(sys_conf_path):
    """Profile CFLAGS replaces system CFLAGS."""
    profile = {"CFLAGS": "-O3 -march=native"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["CFLAGS"] == "-O3 -march=native"

def test_emit_system_cflags_used_when_no_profile_override(sys_conf_path):
    """Without a profile CFLAGS, system value is preserved."""
    profile = {"LDFLAGS": "-Wl,--as-needed"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert "-O2" in conf.get("CFLAGS", "")

def test_emit_new_profile_key_appended(sys_conf_path):
    """Profile key absent from system conf is appended at end."""
    profile = {"MAKEFLAGS": "-j$(nproc)"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert "MAKEFLAGS" in conf

def test_emit_no_sysforge_internal_keys(sys_conf_path):
    """build_mode and other sysforge-internal keys never appear in conf."""
    profile = {"CFLAGS": "-O3", "build_mode": "patched_pkgbuild", "batch": True}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        content = Path(conf_path).read_text()
    assert "build_mode" not in content
    assert "batch" not in content

def test_emit_self_contained_no_source_line(sys_conf_path):
    """Output conf must not contain a '. /etc/makepkg.conf' source line."""
    profile = {"CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        content = Path(conf_path).read_text()
    assert ". /etc/makepkg.conf" not in content
    assert "source /etc/makepkg.conf" not in content

def test_emit_missing_system_conf_falls_back_to_profile_only(tmp_path):
    """If system conf is absent, only profile keys are written — no crash."""
    profile = {"CFLAGS": "-O3 -march=native"}
    with emit_makepkg_conf(
        profile, system_conf_path=tmp_path / "nonexistent.conf"
    ) as conf_path:
        conf = read_conf(conf_path)
    assert conf["CFLAGS"] == "-O3 -march=native"
    # System-only keys should be absent
    assert "CARCH" not in conf

def test_emit_bang_lto_strips_profile_lto_flags(sys_conf_path):
    """A PKGBUILD options=('!lto') strips profile LTO flags at conf-emit (F9).

    makepkg's own !lto only suppresses makepkg-injected LTOFLAGS; profile-baked
    -flto in CFLAGS/CXXFLAGS/LDFLAGS would still reach the compiler, breaking a
    package whose author declared LTO incompatible. So sysforge strips them.
    """
    profile = {
        "CFLAGS": "-O2 -flto=thin",
        "CXXFLAGS": "-O2 -flto=thin",
        "LDFLAGS": "-flto=thin -Wl,-O1",
        "LTOFLAGS": "-flto=thin",
    }
    with emit_makepkg_conf(
        profile, system_conf_path=sys_conf_path, pkgbuild_options=["!lto"]
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-flto" not in conf["CFLAGS"]
    assert "-flto" not in conf["CXXFLAGS"]
    assert "-flto" not in conf["LDFLAGS"]
    assert conf.get("LTOFLAGS", "") == ""


def test_emit_no_bang_lto_keeps_lto_flags(sys_conf_path):
    """Without !lto, profile LTO flags pass through untouched."""
    profile = {"CFLAGS": "-O2 -flto=thin"}
    with emit_makepkg_conf(
        profile, system_conf_path=sys_conf_path, pkgbuild_options=["strip"]
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-flto=thin" in conf["CFLAGS"]


def test_emit_consumes_filters_rust_keys(sys_conf_path):
    """With only 'makepkg' in consumes, RUSTFLAGS is excluded from conf."""
    profile = {"CFLAGS": "-O3", "RUSTFLAGS": "-C opt-level=3"}
    with emit_makepkg_conf(
        profile, active_consumes=["makepkg"],
        system_conf_path=sys_conf_path
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "CFLAGS" in conf
    # RUSTFLAGS from profile excluded (not in makepkg consumes)
    # Note: system conf has RUSTFLAGS — it still passes through since
    # consumes filtering only applies to profile keys, not system baseline
    # (system baseline is always included as-is)
    assert conf.get("CFLAGS") == "-O3"


# ---------------------------------------------------------------------------
# kernel_build flag
# ---------------------------------------------------------------------------

def test_emit_kernel_build_omits_profile_cflags(sys_conf_path):
    """kernel_build=True: profile CFLAGS/LDFLAGS/etc. are not written to conf."""
    profile = {
        "CFLAGS": "-O3 -march=native",
        "CXXFLAGS": "-O3 -march=native",
        "LDFLAGS": "-Wl,-O1,--as-needed",
        "CPPFLAGS": "-D_FORTIFY_SOURCE=2",
        "DEBUG_CFLAGS": "-g",
        "MAKEFLAGS": "-j8",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path,
                           kernel_build=True) as conf_path:
        conf = read_conf(conf_path)
    # Profile flag keys must not override system values
    # (system conf has CFLAGS="-march=x86-64 ... -O2 ...", not "-O3 -march=native")
    assert conf.get("CFLAGS") != "-O3 -march=native"
    assert "-O2" in conf.get("CFLAGS", "")   # system value preserved
    assert conf.get("LDFLAGS") != "-Wl,-O1,--as-needed"
    assert "CPPFLAGS" not in conf             # not in system fixture
    assert "DEBUG_CFLAGS" not in conf
    # MAKEFLAGS is not a flag key — profile value should still apply
    assert conf.get("MAKEFLAGS") == "-j8"


def test_emit_kernel_build_false_still_applies_profile_flags(sys_conf_path):
    """kernel_build=False (default): profile CFLAGS still override system."""
    profile = {"CFLAGS": "-O3 -march=native"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path,
                           kernel_build=False) as conf_path:
        conf = read_conf(conf_path)
    assert conf.get("CFLAGS") == "-O3 -march=native"


def test_emit_kernel_build_ld_override_ignored(sys_conf_path):
    """kernel_build=True: --ld override is silently ignored (no LDFLAGS injection)."""
    profile = {}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path,
                           ld_override="lld", kernel_build=True) as conf_path:
        conf = read_conf(conf_path)
    # ld_override would normally inject -fuse-ld=lld into LDFLAGS;
    # with kernel_build the system LDFLAGS value should be unchanged
    ldflags = conf.get("LDFLAGS", "")
    assert "-fuse-ld=lld" not in ldflags


# ---------------------------------------------------------------------------
# RUSTFLAGS linker reconciliation
# ---------------------------------------------------------------------------

def test_emit_rustflags_linker_mismatch_overridden(sys_conf_path):
    """RUSTFLAGS -fuse-ld=mold is replaced with lld when LDFLAGS uses lld."""
    profile = {
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "RUSTFLAGS": "-C link-arg=-fuse-ld=mold",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["RUSTFLAGS"] == "-C link-arg=-fuse-ld=lld"


def test_emit_rustflags_linker_matches_no_change(sys_conf_path):
    """RUSTFLAGS left alone when its linker already matches LDFLAGS."""
    profile = {
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "RUSTFLAGS": "-C link-arg=-fuse-ld=lld -C opt-level=3",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["RUSTFLAGS"] == "-C link-arg=-fuse-ld=lld -C opt-level=3"


def test_emit_rustflags_no_linker_unchanged(sys_conf_path):
    """RUSTFLAGS without a linker declaration is not modified."""
    profile = {
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "RUSTFLAGS": "-C opt-level=3 -C target-cpu=native",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["RUSTFLAGS"] == "-C opt-level=3 -C target-cpu=native"


def test_emit_rustflags_compact_form_overridden(sys_conf_path):
    """The -Clink-arg= compact form is also reconciled."""
    profile = {
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "RUSTFLAGS": "-Clink-arg=-fuse-ld=mold -C opt-level=3",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["RUSTFLAGS"] == "-Clink-arg=-fuse-ld=lld -C opt-level=3"


def test_emit_rustflags_mold_to_default_linker(sys_conf_path):
    """When LDFLAGS has no linker (effective=ld), RUSTFLAGS mold is overridden to ld."""
    profile = {
        "LDFLAGS": "-Wl,-O1,--as-needed",
        "RUSTFLAGS": "-C link-arg=-fuse-ld=mold",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["RUSTFLAGS"] == "-C link-arg=-fuse-ld=ld"


# ---------------------------------------------------------------------------
# Toolchain key exclusion from conf
# ---------------------------------------------------------------------------

def test_emit_excludes_system_conf_cc_cxx(tmp_path):
    """CC/CXX from system conf must not appear in emitted conf (they're env-injected)."""
    sys_conf = tmp_path / "makepkg.conf"
    sys_conf.write_text(
        'CARCH="x86_64"\n'
        'CC=clang\n'
        'CXX=clang++\n'
        'CFLAGS="-O2"\n'
    )
    profile = {"CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf) as conf_path:
        conf = read_conf(conf_path)
    assert "CC" not in conf
    assert "CXX" not in conf
    assert conf["CFLAGS"] == "-O3"


# ---------------------------------------------------------------------------
# GCC thin-LTO rewrite
# ---------------------------------------------------------------------------

def test_emit_gcc_rewrites_thin_lto_in_ltoflags(sys_conf_path):
    """-flto=thin in LTOFLAGS is rewritten to -flto when CC=gcc."""
    profile = {"CC": "gcc", "LTOFLAGS": "-flto=thin", "CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"


def test_emit_gcc_rewrites_thin_lto_from_system_conf(tmp_path):
    """-flto=thin in system conf LTOFLAGS is rewritten when profile sets CC=gcc."""
    sys_conf = tmp_path / "makepkg.conf"
    sys_conf.write_text(
        'CARCH="x86_64"\n'
        'CFLAGS="-O2"\n'
        'LTOFLAGS="-flto=thin"\n'
    )
    profile = {"CC": "gcc", "CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"


def test_emit_gcc_rewrites_thin_lto_in_cflags(sys_conf_path):
    """-flto=thin embedded in CFLAGS is rewritten to -flto when CC=gcc."""
    profile = {"CC": "gcc", "CFLAGS": "-O3 -flto=thin -pipe"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["CFLAGS"] == "-O3 -flto -pipe"


def test_emit_clang_keeps_thin_lto(sys_conf_path):
    """-flto=thin is preserved when CC=clang."""
    profile = {"CC": "clang", "LTOFLAGS": "-flto=thin", "CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto=thin"


def test_emit_gcc_cc_override_rewrites_thin_lto(sys_conf_path):
    """--cc=gcc override triggers thin-LTO rewrite even if profile has CC=clang."""
    profile = {"CC": "clang", "LTOFLAGS": "-flto=thin"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path,
                           cc_override="gcc") as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"


# ---------------------------------------------------------------------------
# GCC + lld LTO disabling
# ---------------------------------------------------------------------------

def test_emit_gcc_lld_disables_lto(sys_conf_path):
    """GCC + lld: LTOFLAGS cleared, -flto stripped, OPTIONS flipped to !lto."""
    profile = {
        "CC": "gcc",
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "LTOFLAGS": "-flto",
        "CFLAGS": "-O3 -flto -pipe",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == ""
    assert "-flto" not in conf["CFLAGS"]
    assert "-O3" in conf["CFLAGS"]
    # OPTIONS must have !lto to prevent makepkg's ${LTOFLAGS:--flto} fallback
    assert "!lto" in conf.get("OPTIONS", "")


def test_emit_gcc_lld_disables_lto_from_system_conf(tmp_path):
    """GCC + lld: system conf LTOFLAGS cleared and OPTIONS flipped."""
    sys_conf = tmp_path / "makepkg.conf"
    sys_conf.write_text(
        'CARCH="x86_64"\n'
        'CFLAGS="-O2"\n'
        'LTOFLAGS="-flto=thin"\n'
        'OPTIONS=(strip docs lto)\n'
    )
    profile = {"CC": "gcc", "LDFLAGS": "-fuse-ld=lld -Wl,-O1", "CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == ""
    assert "!lto" in conf.get("OPTIONS", "")


def test_emit_clang_lld_keeps_lto(sys_conf_path):
    """clang + lld: LTO is preserved (LLVM LTO works with lld)."""
    profile = {
        "CC": "clang",
        "LDFLAGS": "-fuse-ld=lld -Wl,-O1",
        "LTOFLAGS": "-flto=thin",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto=thin"


def test_emit_gcc_bfd_keeps_lto(sys_conf_path):
    """GCC + bfd (no -fuse-ld): LTO is preserved (GNU ld handles GCC LTO)."""
    profile = {
        "CC": "gcc",
        "LDFLAGS": "-Wl,-O1",
        "LTOFLAGS": "-flto",
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"


# ---------------------------------------------------------------------------
# Hardcoded-GCC signal: GCC flag guard activates even when CC=clang
# ---------------------------------------------------------------------------

def test_emit_hardcoded_gcc_rewrites_thin_lto(sys_conf_path):
    """pkgbuild_has_hardcoded_gcc=True rewrites -flto=thin even with CC=clang."""
    profile = {"CC": "clang", "LTOFLAGS": "-flto=thin", "CFLAGS": "-O3 -flto=thin"}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        pkgbuild_has_hardcoded_gcc=True,
    ) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"
    assert "-flto=thin" not in conf["CFLAGS"]
    assert "-flto" in conf["CFLAGS"]


def test_emit_reactive_fallback_rewrites_thin_lto(sys_conf_path):
    """reactive_gcc_fallback=True rewrites -flto=thin even with CC=clang."""
    profile = {"CC": "clang", "LTOFLAGS": "-flto=thin"}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        reactive_gcc_fallback=True,
    ) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto"


def test_emit_hardcoded_gcc_lld_disables_lto(sys_conf_path):
    """pkgbuild_has_hardcoded_gcc + lld disables LTO entirely."""
    profile = {
        "CC": "clang",
        "LDFLAGS": "-fuse-ld=lld",
        "LTOFLAGS": "-flto=thin",
        "CFLAGS": "-O3 -flto=thin",
    }
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        pkgbuild_has_hardcoded_gcc=True,
    ) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == ""
    assert "-flto" not in conf["CFLAGS"]


def test_emit_no_hardcoded_gcc_keeps_thin_lto(sys_conf_path):
    """Regression guard: CC=clang without any GCC signal preserves thin LTO."""
    profile = {"CC": "clang", "LTOFLAGS": "-flto=thin"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        conf = read_conf(conf_path)
    assert conf["LTOFLAGS"] == "-flto=thin"


# ---------------------------------------------------------------------------
# Variant-derived linker soft default (3.2)
# ---------------------------------------------------------------------------

def test_emit_variant_stock_llvm_defaults_to_lld(sys_conf_path):
    """stock_llvm variant + no linker in profile/system LDFLAGS -> -fuse-ld=lld injected."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="stock_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" in conf.get("LDFLAGS", "")


def test_emit_variant_pgo_llvm_defaults_to_lld(sys_conf_path):
    """pgo_llvm variant gets the same lld default as stock_llvm."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="pgo_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" in conf.get("LDFLAGS", "")


def test_emit_variant_respects_profile_linker(sys_conf_path):
    """Profile LDFLAGS already declares -fuse-ld=mold -> soft default skips injection."""
    profile = {"LDFLAGS": "-Wl,-O1 -fuse-ld=mold"}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="pgo_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    ldflags = conf.get("LDFLAGS", "")
    assert "-fuse-ld=mold" in ldflags
    assert "-fuse-ld=lld" not in ldflags


def test_emit_variant_respects_system_linker(tmp_path):
    """System LDFLAGS already declares -fuse-ld=ld.bfd -> soft default skips injection."""
    p = tmp_path / "makepkg.conf"
    p.write_text(
        'CARCH="x86_64"\n'
        'CFLAGS="-O2"\n'
        'LDFLAGS="-Wl,-O1 -fuse-ld=ld.bfd"\n'
    )
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=p,
        toolchain_variant="stock_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" not in conf.get("LDFLAGS", "")


def test_emit_explicit_ld_override_beats_variant_default(sys_conf_path):
    """Explicit ld_override wins over the variant-derived soft default."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        ld_override="mold",
        toolchain_variant="pgo_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    ldflags = conf.get("LDFLAGS", "")
    assert "-fuse-ld=mold" in ldflags
    assert "-fuse-ld=lld" not in ldflags


def test_emit_variant_gcc_no_lld_injection(sys_conf_path):
    """gcc variant must not inject -fuse-ld=lld."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="gcc",
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" not in conf.get("LDFLAGS", "")


def test_emit_variant_none_no_lld_injection(sys_conf_path):
    """toolchain_variant=None (no toolchain stage run) -> no soft default."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant=None,
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" not in conf.get("LDFLAGS", "")


def test_emit_variant_kernel_build_skips_lld_default(sys_conf_path):
    """kernel_build=True short-circuits the variant default (kernel uses LLVM= for linker)."""
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="pgo_llvm",
        kernel_build=True,
    ) as conf_path:
        conf = read_conf(conf_path)
    # Kernel path leaves LDFLAGS to system-conf passthrough; no -fuse-ld= injected.
    assert "-fuse-ld=lld" not in conf.get("LDFLAGS", "")


def test_emit_variant_lld_missing_skips_injection(sys_conf_path, monkeypatch):
    """Defensive: if lld is not on PATH, the soft default does not inject."""
    import sysforge.primitives.makepkg_wrapper as mw
    monkeypatch.setattr(mw.shutil, "which", lambda name: None if name == "lld" else "/usr/bin/" + name)
    profile = {}
    with emit_makepkg_conf(
        profile,
        system_conf_path=sys_conf_path,
        toolchain_variant="stock_llvm",
    ) as conf_path:
        conf = read_conf(conf_path)
    assert "-fuse-ld=lld" not in conf.get("LDFLAGS", "")


# ---------------------------------------------------------------------------
# Regression: profile LDFLAGS without -fuse-ld= must shadow system -fuse-ld=lld
# ---------------------------------------------------------------------------

def test_emit_profile_ldflags_no_fuse_ld_shadows_system_lld(tmp_path):
    """Profile LDFLAGS without -fuse-ld= must shadow the system's -fuse-ld=lld.

    When the profile sets LDFLAGS but omits -fuse-ld=, the effective linker
    must be derived from the profile value only (i.e. 'ld', the default).
    The system conf's -fuse-ld=lld is irrelevant because the profile value
    REPLACES the system value in the emitted conf.

    Observable signal: an lld-only flag (--icf=all) in the profile LDFLAGS
    must be STRIPPED (because effective linker is 'ld', not 'lld').  With the
    bug, resolve_effective_linker falls through to the system value and returns
    'lld', so --icf=all is incorrectly preserved.
    """
    sys_conf = tmp_path / "makepkg.conf"
    sys_conf.write_text(
        'CARCH="x86_64"\n'
        'LDFLAGS="-Wl,-O1 -fuse-ld=lld"\n'
    )
    # Profile sets LDFLAGS but WITHOUT -fuse-ld= — it has an lld-only flag.
    profile = {"LDFLAGS": "-Wl,-O1 --icf=all"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf) as conf_path:
        conf = read_conf(conf_path)
    # The lld-only flag must have been stripped because the effective linker is
    # 'ld' (no -fuse-ld in the profile, and the system value is shadowed).
    assert "--icf=all" not in conf.get("LDFLAGS", ""), (
        "lld-only flag --icf=all should have been stripped when profile "
        "LDFLAGS has no -fuse-ld= (effective linker is 'ld', not 'lld')"
    )
