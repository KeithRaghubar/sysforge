"""config.set_toml_key — the one runtime TOML key writer (3.1.0-F4)."""
import pytest

from sysforge.primitives import config


def rw(text, section="build", key="review", value=True):
    return config._rewrite_toml_key(text, section, key, value)


def test_replaces_active_key_in_section():
    out = rw('[build]\nreview = false\n[other]\nreview = false\n', value="accept")
    assert out == '[build]\nreview = "accept"\n[other]\nreview = false\n'


def test_uncomments_pure_commented_assignment():
    text = '[build]\n# review: the gate. review = true asks first.\n#review = true\n'
    out = rw(text, value=False)
    assert out == '[build]\n# review: the gate. review = true asks first.\nreview = false\n'


def test_prose_comment_never_matched():
    text = '[build]\n#   review = "accept" builds unreviewed code\nx = 1\n'
    out = rw(text, value=False)
    assert '#   review = "accept" builds unreviewed code' in out
    assert out.splitlines()[1] == "review = false"   # inserted after header


def test_subtable_header_is_not_the_section():
    text = '[build.extra]\nreview = true\n'
    out = rw(text, value=False)
    assert out == '[build.extra]\nreview = true\n\n[build]\nreview = false\n'


def test_section_ends_at_next_table_including_arrays():
    text = '[build]\nx = 1\n[[package]]\nname = "a"\nreview = true\n'
    out = rw(text, value=False)
    assert out.splitlines()[:3] == ["[build]", "review = false", "x = 1"]
    assert out.splitlines()[-1] == "review = true"


def test_missing_file_text_creates_section():
    assert rw("", value=True) == "[build]\nreview = true\n"


def test_trailing_newline_preserved():
    assert rw("[build]\nreview = true", value=False) == "[build]\nreview = false"


def test_literals():
    assert config.toml_literal(True) == "true"
    assert config.toml_literal('a "b" ü') == '"a \\"b\\" \\u00fc"'
    with pytest.raises(TypeError):
        config.toml_literal(3)


def test_set_toml_key_goes_through_atomic_write(tmp_path, monkeypatch):
    from sysforge.primitives import atomic_write
    seen = {}

    def _replace(path, text, *, tag, default_mode=0o644):
        seen["args"] = (path, text, tag)
    monkeypatch.setattr(atomic_write, "replace_file", _replace)
    p = tmp_path / "packages.toml"
    p.write_text("[build]\n#review = true\n")
    config.set_toml_key(p, "build", "review", "accept")
    assert seen["args"] == (p, '[build]\nreview = "accept"\n', "CONFIG")


def test_set_default_toolchain_uses_atomic_write(tmp_path, monkeypatch):
    """3.5.0-B6: it wrote with path.write_text, which cannot escalate for a
    root-owned profiles.toml and is not atomic."""
    from sysforge.primitives import atomic_write
    calls = []
    monkeypatch.setattr(atomic_write, "replace_file",
                        lambda path, text, *, tag, default_mode=0o644: calls.append(path))
    p = tmp_path / "profiles.toml"
    p.write_text('[defaults]\ntoolchain = "gcc"\n')
    config.set_default_toolchain("llvm", path=p)
    assert calls == [p]


def test_top_level_replaces_active_key_before_first_header():
    out = rw('hardening = false\n[fdo]\nhardening = false\n', section=None,
             key="hardening", value=True)
    assert out == 'hardening = true\n[fdo]\nhardening = false\n'


def test_top_level_uncomments_commented_example():
    out = rw('# hardening: doc\n#hardening = false\n\n[fdo]\n', section=None,
             key="hardening", value=True)
    assert out == '# hardening: doc\nhardening = true\n\n[fdo]\n'


def test_top_level_inserts_above_first_header():
    out = rw('[fdo]\nmode = "x"\n', section=None, key="hardening", value=True)
    assert out == 'hardening = true\n\n[fdo]\nmode = "x"\n'


def test_top_level_ignores_key_inside_a_table():
    out = rw('[fdo]\n#hardening = false\n', section=None, key="hardening", value=True)
    assert out.startswith('hardening = true\n')
    assert '[fdo]\n#hardening = false' in out


def test_top_level_empty_text():
    assert rw('', section=None, key="hardening", value=True) == 'hardening = true\n'


def test_toml_location():
    assert config.toml_location("build", "review") == "[build] review"
    assert config.toml_location(None, "hardening") == "hardening"
