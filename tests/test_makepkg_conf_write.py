"""
test_makepkg_conf_write.py — makepkg.conf key writing.

Covers the in-place key writer in config.py (set_makepkg_conf_keys) and its
pure transform (_rewrite_makepkg_conf_text): replace an active assignment,
uncomment a commented one, else append.
"""
from sysforge.primitives.config import (
    _rewrite_makepkg_conf_text,
    set_makepkg_conf_keys,
)


# ---------------------------------------------------------------------------
# set_makepkg_conf_keys / _rewrite_makepkg_conf_text
# ---------------------------------------------------------------------------

def test_rewrite_replaces_active_assignment():
    text = 'CARCH="x86_64"\nPACKAGER="Unknown Packager"\nPKGEXT=".pkg.tar.zst"\n'
    out = _rewrite_makepkg_conf_text(text, {"PACKAGER": "Me <me@x>"})
    assert 'PACKAGER="Me <me@x>"' in out
    assert "Unknown Packager" not in out
    assert 'CARCH="x86_64"' in out  # other lines preserved


def test_rewrite_uncomments_commented_assignment():
    text = "#MAKEFLAGS=\"-j2\"\nCARCH=\"x86_64\"\n"
    out = _rewrite_makepkg_conf_text(text, {"MAKEFLAGS": "-j8"})
    assert 'MAKEFLAGS="-j8"' in out
    assert "#MAKEFLAGS" not in out


def test_rewrite_appends_when_absent():
    text = 'CARCH="x86_64"\n'
    out = _rewrite_makepkg_conf_text(text, {"PACKAGER": "Me <me@x>"})
    assert out.rstrip().endswith('PACKAGER="Me <me@x>"')


def test_rewrite_preserves_indented_export():
    text = "  export MAKEFLAGS=\"-j2\"\n"
    out = _rewrite_makepkg_conf_text(text, {"MAKEFLAGS": "-j8"})
    assert out == '  MAKEFLAGS="-j8"\n'


def test_set_makepkg_conf_keys_to_separate_dest(tmp_path):
    src = tmp_path / "makepkg.conf"
    src.write_text('PACKAGER="Unknown Packager"\n')
    dest = tmp_path / "staged.conf"
    set_makepkg_conf_keys(src, {"PACKAGER": "Me <me@x>"}, dest=dest)
    assert src.read_text() == 'PACKAGER="Unknown Packager"\n'  # source untouched
    assert 'PACKAGER="Me <me@x>"' in dest.read_text()


def test_set_makepkg_conf_keys_missing_file(tmp_path):
    dest = tmp_path / "new.conf"
    set_makepkg_conf_keys(dest, {"MAKEFLAGS": "-j4"})
    assert dest.read_text().strip() == 'MAKEFLAGS="-j4"'
