#!/usr/bin/env python3
"""
Unit tests for consumes filtering.

Covers:
  - load_consumes_inference: fallback defaults, system file, user override,
    user extends_system merge
  - resolve_consumes: explicit profile key, inferred from makedepends,
    baseline "makepkg" always present, version-constraint stripping,
    no-makedepends fallback
  - emit_makepkg_conf: keys filtered to makepkg conf type, RUSTFLAGS excluded
    when rust not in consumes, RUSTFLAGS included when rust IS in consumes,
    fallback (active_consumes=None) writes all non-internal keys
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.makepkg_wrapper import (
    emit_makepkg_conf,
    load_consumes_inference,
    resolve_consumes,
)
from sysforge.primitives.profile import CONF_KEY_MAP, SYSFORGE_KEYS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_toml(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _read_conf(path):
    """Parse a written makepkg.conf temp file into a dict."""
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        key, _, val = line.partition("=")
        result[key] = val.strip('"')
    return result


# ---------------------------------------------------------------------------
# load_consumes_inference
# ---------------------------------------------------------------------------

def test_load_consumes_inference_defaults():
    """Returns built-in defaults when no files exist."""
    result = load_consumes_inference(paths=[Path("/nonexistent/a"), Path("/nonexistent/b")])
    assert "cargo" in result
    assert "makepkg" in result["cargo"]
    assert "rust" in result["cargo"]

def test_load_consumes_inference_system_file():
    with tempfile.TemporaryDirectory() as d:
        system = _write_toml(
            f"{d}/system.toml",
            '[consumes_inference]\ncargo = ["makepkg", "rust"]\n'
        )
        result = load_consumes_inference(paths=[Path(f"{d}/missing.toml"), system])
    assert result == {"cargo": ["makepkg", "rust"]}

def test_load_consumes_inference_user_overrides_system():
    """User file alone wins when extends_system is absent/false."""
    with tempfile.TemporaryDirectory() as d:
        system = _write_toml(f"{d}/sys.toml", '[consumes_inference]\ncargo = ["makepkg"]\n')
        user = _write_toml(
            f"{d}/user.toml",
            '[consumes_inference]\ncargo = ["makepkg", "rust", "env"]\n')
        result = load_consumes_inference(paths=[user, system])
    assert result == {"cargo": ["makepkg", "rust", "env"]}

def test_load_consumes_inference_extends_system_merge():
    with tempfile.TemporaryDirectory() as d:
        system = _write_toml(
            f"{d}/sys.toml",
            '[consumes_inference]\ncargo = ["makepkg", "rust"]\ncmake = ["makepkg"]\n'
        )
        user = _write_toml(
            f"{d}/user.toml",
            'extends_system = true\n[consumes_inference]\ncargo = ["makepkg", "rust", "env"]\n'
        )
        result = load_consumes_inference(paths=[user, system])
    # User wins on cargo; cmake inherited from system
    assert result["cargo"] == ["makepkg", "rust", "env"]
    assert result["cmake"] == ["makepkg"]


# ---------------------------------------------------------------------------
# resolve_consumes
# ---------------------------------------------------------------------------

INFERENCE_MAP = {
    "cargo":  ["makepkg", "rust", "env"],
    "cmake":  ["makepkg", "cmake", "env"],
    "ninja":  ["makepkg", "env"],
    "git":    ["makepkg"],
    "meson":  ["makepkg", "meson", "env"],
}

def _meta(makedepends=None):
    return {"globals": {"pkgname": "testpkg", "makedepends": makedepends or []}}

def test_resolve_consumes_explicit_override():
    profile = {"consumes": ["makepkg", "rust"]}
    result = resolve_consumes(profile, _meta(["cmake", "ninja"]), INFERENCE_MAP)
    # Explicit overrides inference entirely
    assert result == frozenset({"makepkg", "rust"})

def test_resolve_consumes_baseline_always_present():
    result = resolve_consumes({}, _meta([]), INFERENCE_MAP)
    assert "makepkg" in result

def test_resolve_consumes_inferred_cargo():
    result = resolve_consumes({}, _meta(["cargo"]), INFERENCE_MAP)
    assert result == frozenset({"makepkg", "rust", "env"})

def test_resolve_consumes_inferred_cmake_ninja():
    result = resolve_consumes({}, _meta(["cmake", "ninja"]), INFERENCE_MAP)
    assert "makepkg" in result
    assert "cmake" in result
    assert "env" in result

def test_resolve_consumes_unknown_makedep_ignored():
    result = resolve_consumes({}, _meta(["some-obscure-tool"]), INFERENCE_MAP)
    assert result == frozenset({"makepkg"})

def test_resolve_consumes_version_constraint_stripped():
    """makedepends entries like 'cmake>=3.25' should match 'cmake' in the map."""
    result = resolve_consumes({}, _meta(["cmake>=3.25"]), INFERENCE_MAP)
    assert "cmake" in result

def test_resolve_consumes_mixed():
    """git maps to only makepkg; combined with cmake gives makepkg+cmake+env."""
    result = resolve_consumes({}, _meta(["git", "cmake"]), INFERENCE_MAP)
    assert "makepkg" in result
    assert "cmake" in result
    assert "env" in result
    assert "rust" not in result


# ---------------------------------------------------------------------------
# emit_makepkg_conf key filtering
# ---------------------------------------------------------------------------

PROFILE_MIXED = {
    "CFLAGS": "-O3 -march=native",
    "CXXFLAGS": "$CFLAGS",
    "LDFLAGS": "-Wl,--as-needed",
    "RUSTFLAGS": "-C opt-level=3",
    "build_mode": "standard",   # sysforge-internal → always excluded
    "makepkg_flags": ["--noconfirm"],  # sysforge-internal → always excluded
}

def test_emit_conf_makepkg_only_excludes_rustflags():
    """When consumes={"makepkg"}, RUSTFLAGS must not appear in makepkg.conf."""
    active = frozenset({"makepkg"})
    with emit_makepkg_conf(PROFILE_MIXED, active) as path:
        conf = _read_conf(path)
    assert "CFLAGS" in conf
    assert "LDFLAGS" in conf
    assert "RUSTFLAGS" not in conf
    assert "build_mode" not in conf

def test_emit_conf_with_rust_includes_rustflags():
    """When consumes includes 'rust', RUSTFLAGS must appear in makepkg.conf."""
    active = frozenset({"makepkg", "rust"})
    with emit_makepkg_conf(PROFILE_MIXED, active) as path:
        conf = _read_conf(path)
    assert "RUSTFLAGS" in conf
    assert "CFLAGS" in conf

def test_emit_conf_sysforge_keys_always_excluded():
    """Sysforge-internal keys are never written regardless of consumes."""
    active = frozenset({"makepkg", "rust"})
    with emit_makepkg_conf(PROFILE_MIXED, active) as path:
        conf = _read_conf(path)
    for key in SYSFORGE_KEYS:
        assert key not in conf, f"Sysforge key {key!r} leaked into conf"

def test_emit_conf_fallback_none_writes_all_non_internal():
    """active_consumes=None is the backward-compat fallback: write everything non-internal."""
    with emit_makepkg_conf(PROFILE_MIXED, None) as path:
        conf = _read_conf(path)
    assert "CFLAGS" in conf
    assert "RUSTFLAGS" in conf
    assert "build_mode" not in conf

def test_emit_conf_temp_file_cleaned_up():
    """Temp file must not exist after the context manager exits."""
    active = frozenset({"makepkg"})
    with emit_makepkg_conf(PROFILE_MIXED, active) as path:
        assert Path(path).exists()
    assert not Path(path).exists()

def test_emit_conf_values_quoted():
    """Values must be written as KEY="value" (double-quoted)."""
    active = frozenset({"makepkg"})
    with emit_makepkg_conf({"CFLAGS": "-O2 -pipe"}, active) as path:
        raw = Path(path).read_text()
    assert 'CFLAGS="-O2 -pipe"' in raw


# ---------------------------------------------------------------------------
# CONF_KEY_MAP sanity
# ---------------------------------------------------------------------------

def test_conf_key_map_no_overlap_with_sysforge_keys():
    """No conf type key set should overlap with SYSFORGE_KEYS."""
    all_conf_keys = set()
    for keys in CONF_KEY_MAP.values():
        all_conf_keys.update(keys)
    overlap = all_conf_keys & SYSFORGE_KEYS
    assert not overlap, f"CONF_KEY_MAP overlaps SYSFORGE_KEYS: {overlap}"

def test_conf_key_map_makepkg_has_standard_vars():
    mk = CONF_KEY_MAP["makepkg"]
    for expected in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        assert expected in mk
    # CC and CXX live in "toolchain" — injected via subprocess env, not conf file
    assert "CC" not in mk
    assert "CXX" not in mk

def test_conf_key_map_toolchain_has_cc_cxx():
    tc = CONF_KEY_MAP["toolchain"]
    assert "CC" in tc
    assert "CXX" in tc

def test_conf_key_map_rust_has_rustflags():
    assert "RUSTFLAGS" in CONF_KEY_MAP["rust"]


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
            print(f"  FAIL  {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
