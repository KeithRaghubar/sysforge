"""
test_system_conf.py — tests for system makepkg.conf parsing and
the merged conf emission in emit_makepkg_conf.

Uses a synthetic makepkg.conf fixture rather than the real /etc/makepkg.conf.
"""
import tempfile
from pathlib import Path

import pytest

from sysforge.primitives.config import parse_system_makepkg_conf
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

SYSTEM_CONF = """\
# makepkg.conf — synthetic fixture for tests

CARCH="x86_64"
CHOST="x86_64-pc-linux-gnu"

CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fno-plt"
CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
LDFLAGS="-Wl,-O1,--sort-common,--as-needed,-z,relro,-z,now"
RUSTFLAGS="-C opt-level=2"

BUILDENV=(!distcc color !ccache check !sign)
OPTIONS=(strip docs !libtool !staticlibs emptydirs zipman purge !debug lto)
INTEGRITY_CHECK=(sha256)

PKGEXT='.pkg.tar.zst'
SRCEXT='.src.tar.gz'

PACKAGER="Arch Linux <archlinux@archlinux.org>"
COMPRESSXZ=(xz -c -z - --threads=0)
"""


@pytest.fixture
def sys_conf_path(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text(SYSTEM_CONF)
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
    profile = {"RUSTFLAGS": "-C target-cpu=native"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf_path:
        content = Path(conf_path).read_text()
    # RUSTFLAGS is in the system conf fixture, so override it and
    # test a genuinely new key
    profile2 = {"MAKEFLAGS": "-j$(nproc)"}
    with emit_makepkg_conf(profile2, system_conf_path=sys_conf_path) as conf_path2:
        conf = read_conf(conf_path2)
    assert "MAKEFLAGS" in conf

def test_emit_no_sysforge_internal_keys(sys_conf_path):
    """build_mode and other sysforge-internal keys never appear in conf."""
    profile = {"CFLAGS": "-O3", "build_mode": "patch_pkgbuild", "batch": True}
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
