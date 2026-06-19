#!/usr/bin/env python3
"""
Unit tests for the single-knob ``toolchain`` profile field and its propagation.

Covers:
  - profile._expand_toolchain / resolve_profile: gcc + llvm bundle expansion,
    explicit-key override precedence, [defaults] fallback, lld injection (and
    preservation of an explicit -fuse-ld=), unknown-value no-op.
  - config._rewrite_profiles_default_toolchain / set_default_toolchain: replace,
    uncomment, insert-under-header, append-section, comment preservation.
  - toolchain stage._propagate_default_toolchain: gcc/llvm parity, dry-run no-op.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import config
from sysforge.primitives.profile import _expand_toolchain, resolve_profile


# ---------------------------------------------------------------------------
# _expand_toolchain (pure)
# ---------------------------------------------------------------------------

def test_expand_gcc_bundle():
    p = _expand_toolchain({"toolchain": "gcc"}, None)
    assert p["CC"] == "gcc"
    assert p["CXX"] == "g++"
    # gcc leaves binutils to the system base-devel defaults.
    assert "AR" not in p
    assert "LDFLAGS" not in p


def test_expand_llvm_bundle_with_linker_injection():
    p = _expand_toolchain({"toolchain": "llvm", "LDFLAGS": "-Wl,-O1"}, None)
    assert p["CC"] == "clang"
    assert p["CXX"] == "clang++"
    assert p["AR"] == "llvm-ar"
    assert p["NM"] == "llvm-nm"
    assert p["RANLIB"] == "llvm-ranlib"
    assert p["STRIP"] == "llvm-strip"
    assert "-fuse-ld=lld" in p["LDFLAGS"]
    assert "-Wl,-O1" in p["LDFLAGS"]


def test_expand_llvm_injects_linker_when_no_ldflags():
    p = _expand_toolchain({"toolchain": "llvm"}, None)
    assert p["LDFLAGS"] == "-fuse-ld=lld"


def test_expand_llvm_preserves_explicit_linker():
    p = _expand_toolchain({"toolchain": "llvm", "LDFLAGS": "-fuse-ld=mold -Wl,-O1"}, None)
    assert "-fuse-ld=mold" in p["LDFLAGS"]
    assert "-fuse-ld=lld" not in p["LDFLAGS"]


def test_explicit_cc_wins_over_bundle():
    p = _expand_toolchain({"toolchain": "llvm", "CC": "gcc", "CXX": "g++"}, None)
    assert p["CC"] == "gcc"
    assert p["CXX"] == "g++"
    # non-overridden bundle keys still fill in.
    assert p["AR"] == "llvm-ar"


def test_defaults_fallback_used_when_profile_has_no_field():
    p = _expand_toolchain({}, "gcc")
    assert p["CC"] == "gcc"


def test_profile_field_overrides_defaults_fallback():
    p = _expand_toolchain({"toolchain": "llvm"}, "gcc")
    assert p["CC"] == "clang"


def test_no_field_and_no_default_is_noop():
    p = _expand_toolchain({"CFLAGS": "-O2"}, None)
    assert "CC" not in p


def test_unknown_value_warns_and_noops():
    p = _expand_toolchain({"toolchain": "rustc"}, None)
    assert "CC" not in p


# ---------------------------------------------------------------------------
# resolve_profile end-to-end (expansion runs after merge_extends)
# ---------------------------------------------------------------------------

def _meta(name="pkg"):
    return {"globals": {"pkgbase": name, "pkgname": name}}


def _config(default_toolchain="gcc"):
    return {
        "profiles": {
            "bare": {"BUILDDIR": "$HOME/builds"},
            "standard": {"extends": "bare", "CFLAGS": "-O2"},
            "llvmprof": {"extends": "standard", "toolchain": "llvm"},
        },
        "defaults": {"profile": "standard", "toolchain": default_toolchain},
    }


def test_resolve_profile_applies_defaults_toolchain():
    result = resolve_profile(_meta(), [], _config("gcc"))
    assert result["CC"] == "gcc"
    assert result["CXX"] == "g++"


def test_resolve_profile_child_toolchain_overrides_default():
    rules = [{"profile": "llvmprof", "priority": 10}]
    result = resolve_profile(_meta(), rules, _config("gcc"))
    assert result["CC"] == "clang"
    assert result["AR"] == "llvm-ar"
    assert "-fuse-ld=lld" in result["LDFLAGS"]


# ---------------------------------------------------------------------------
# _rewrite_profiles_default_toolchain (pure)
# ---------------------------------------------------------------------------

def test_rewrite_replaces_existing_key():
    text = '[defaults]\nprofile = "standard"\ntoolchain = "gcc"\n'
    out = config._rewrite_profiles_default_toolchain(text, "llvm")
    assert 'toolchain = "llvm"' in out
    assert 'toolchain = "gcc"' not in out
    assert 'profile = "standard"' in out


def test_rewrite_uncomments_commented_key():
    text = '[defaults]\nprofile = "standard"\n# toolchain = "gcc"\n'
    out = config._rewrite_profiles_default_toolchain(text, "llvm")
    assert 'toolchain = "llvm"' in out
    assert out.count("toolchain") == 1


def test_rewrite_inserts_when_absent_in_defaults():
    text = '[paths]\nx = 1\n\n[defaults]\nprofile = "standard"\n\n[profiles.bare]\n'
    out = config._rewrite_profiles_default_toolchain(text, "gcc")
    lines = out.splitlines()
    di = lines.index("[defaults]")
    # inserted immediately after the header, inside the section.
    assert lines[di + 1] == 'toolchain = "gcc"'
    assert "[profiles.bare]" in out


def test_rewrite_appends_section_when_no_defaults():
    text = '[paths]\nx = 1\n'
    out = config._rewrite_profiles_default_toolchain(text, "llvm")
    assert "[defaults]" in out
    assert 'toolchain = "llvm"' in out
    assert "[paths]" in out


def test_rewrite_preserves_comments_and_other_sections():
    text = (
        "# header comment\n[defaults]\n"
        '# a note\nprofile = "standard"\ntoolchain = "gcc"\n'
        '\n[profiles.bare]\nBUILDDIR = "x"\n'
    )
    out = config._rewrite_profiles_default_toolchain(text, "llvm")
    assert "# header comment" in out
    assert "# a note" in out
    assert 'BUILDDIR = "x"' in out
    assert 'toolchain = "llvm"' in out


def test_rewrite_only_touches_first_defaults_table():
    # A `toolchain` key in a later [profiles.*] table must be left alone.
    text = (
        '[defaults]\ntoolchain = "gcc"\n'
        '\n[profiles.x]\ntoolchain = "llvm"\n'
    )
    out = config._rewrite_profiles_default_toolchain(text, "llvm")
    # defaults flipped, profile key untouched.
    assert out.index('toolchain = "llvm"\n\n[profiles.x]') >= 0
    assert out.count('toolchain = "llvm"') == 2


# ---------------------------------------------------------------------------
# set_default_toolchain (round-trip to disk)
# ---------------------------------------------------------------------------

def test_set_default_toolchain_writes_explicit_path(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text('[defaults]\nprofile = "standard"\ntoolchain = "gcc"\n', encoding="utf-8")
    config.set_default_toolchain("llvm", path=p)
    assert 'toolchain = "llvm"' in p.read_text(encoding="utf-8")


def test_set_default_toolchain_missing_file_creates_section(tmp_path):
    p = tmp_path / "profiles.toml"
    config.set_default_toolchain("gcc", path=p)
    out = p.read_text(encoding="utf-8")
    assert "[defaults]" in out
    assert 'toolchain = "gcc"' in out


# ---------------------------------------------------------------------------
# toolchain stage propagation (dual gcc/llvm parity)
# ---------------------------------------------------------------------------

def _import_stage():
    from sysforge.pipeline.stages import toolchain as ts
    return ts


def test_propagate_gcc(monkeypatch):
    ts = _import_stage()
    calls = []
    monkeypatch.setattr(ts, "set_default_toolchain", lambda c: calls.append(c))
    ts._propagate_default_toolchain("gcc", types.SimpleNamespace(dry_run=False))
    assert calls == ["gcc"]


def test_propagate_llvm(monkeypatch):
    ts = _import_stage()
    calls = []
    monkeypatch.setattr(ts, "set_default_toolchain", lambda c: calls.append(c))
    ts._propagate_default_toolchain("llvm", types.SimpleNamespace(dry_run=False))
    assert calls == ["llvm"]


def test_propagate_dry_run_is_noop(monkeypatch):
    ts = _import_stage()
    calls = []
    monkeypatch.setattr(ts, "set_default_toolchain", lambda c: calls.append(c))
    ts._propagate_default_toolchain("llvm", types.SimpleNamespace(dry_run=True))
    assert calls == []


def test_propagate_tolerates_unwritable_config(monkeypatch):
    ts = _import_stage()

    def _boom(_c):
        raise OSError("read-only fs")

    monkeypatch.setattr(ts, "set_default_toolchain", _boom)
    # Must not raise — a config write failure is a warning, not a stage failure.
    ts._propagate_default_toolchain("gcc", types.SimpleNamespace(dry_run=False))
