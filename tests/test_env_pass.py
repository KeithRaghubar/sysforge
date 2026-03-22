#!/usr/bin/env python3
"""
Unit tests for the env pass: resolve_env_vars and emit_makepkg_conf.

Covers:
  resolve_env_vars:
    - "env" type key collected when "env" in active_consumes
    - "env" type key NOT collected when "env" absent from active_consumes
    - "env" type key collected in fallback mode (active_consumes=None)
    - Unknown key always collected + warning logged
    - _SYSFORGE_KEYS never collected
    - known non-env conf key (CFLAGS) never collected
    - multiple env keys in one call
    - empty profile returns {}

  emit_makepkg_conf:
    - "env" type keys NOT written to conf file
    - unknown keys NOT written to conf file
    - rust keys written when "rust" in active_consumes
    - rust keys NOT written when "rust" absent from active_consumes
    - makepkg keys always written (when "makepkg" in active_consumes)
    - fallback mode (None) writes all non-env, non-sysforge keys
    - temp file cleaned up after context exit
    - conf line format is KEY="VALUE"

  integration:
    - RUSTC_WRAPPER in profile + "env" consumes → goes to env, not conf
    - RUSTFLAGS in profile + "rust" consumes → goes to conf, not env
    - RUSTFLAGS in profile, "rust" NOT in consumes → goes to neither (filtered)
"""
import sys
import os
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.makepkg_wrapper import (
    resolve_env_vars,
    emit_makepkg_conf,
)
from sysforge.primitives.profile import _CONF_KEY_MAP, _SYSFORGE_KEYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def captured(fn, *args, **kwargs):
    """Call fn and return (return_value, stderr_text)."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def read_conf(conf_path):
    """Parse a temp makepkg.conf into a dict."""
    out = {}
    for line in Path(conf_path).read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key] = val.strip('"')
    return out


# ---------------------------------------------------------------------------
# resolve_env_vars
# ---------------------------------------------------------------------------

def test_env_key_collected_when_env_in_consumes():
    profile = {"RUSTC_WRAPPER": "sccache"}
    result, _ = captured(resolve_env_vars, profile, ["makepkg", "rust", "env"])
    assert result == {"RUSTC_WRAPPER": "sccache"}

def test_env_key_not_collected_when_env_absent():
    profile = {"RUSTC_WRAPPER": "sccache"}
    result, _ = captured(resolve_env_vars, profile, ["makepkg", "rust"])
    assert "RUSTC_WRAPPER" not in result

def test_env_key_collected_in_fallback_mode():
    profile = {"RUSTC_WRAPPER": "sccache"}
    result, _ = captured(resolve_env_vars, profile, None)
    assert result == {"RUSTC_WRAPPER": "sccache"}

def test_unknown_key_always_collected():
    profile = {"MY_CUSTOM_VAR": "foo"}
    result, log = captured(resolve_env_vars, profile, ["makepkg"])
    assert result == {"MY_CUSTOM_VAR": "foo"}
    assert "Unclassified" in log

def test_sysforge_keys_never_collected():
    for key in _SYSFORGE_KEYS:
        profile = {key: "something"}
        result, _ = captured(resolve_env_vars, profile, None)
        assert key not in result, f"{key} should not appear in env result"

def test_known_conf_key_not_collected():
    # CFLAGS is a "makepkg" type key — should never be in env result
    profile = {"CFLAGS": "-O2 -march=native"}
    result, _ = captured(resolve_env_vars, profile, None)
    assert "CFLAGS" not in result

def test_multiple_env_keys():
    profile = {
        "RUSTC_WRAPPER": "sccache",
        "CCACHE_DIR": "/tmp/ccache",
        "CFLAGS": "-O2",
    }
    result, _ = captured(resolve_env_vars, profile, ["makepkg", "env"])
    assert result == {"RUSTC_WRAPPER": "sccache", "CCACHE_DIR": "/tmp/ccache"}
    assert "CFLAGS" not in result

def test_empty_profile_returns_empty():
    result, _ = captured(resolve_env_vars, {}, ["makepkg", "rust", "env"])
    assert result == {}

def test_unknown_key_logs_warning():
    profile = {"WEIRD_FLAG": "x"}
    _, log = captured(resolve_env_vars, profile, ["makepkg"])
    assert "Unclassified" in log
    assert "WEIRD_FLAG" in log

def test_env_key_log_message():
    profile = {"RUSTC_WRAPPER": "sccache"}
    _, log = captured(resolve_env_vars, profile, ["env"])
    assert "RUSTC_WRAPPER" in log


# ---------------------------------------------------------------------------
# emit_makepkg_conf
# ---------------------------------------------------------------------------

def test_env_keys_not_in_conf():
    profile = {"CFLAGS": "-O2", "RUSTC_WRAPPER": "sccache"}
    with emit_makepkg_conf(profile, ["makepkg", "env"]) as conf_path:
        conf = read_conf(conf_path)
    assert "CFLAGS" in conf
    assert "RUSTC_WRAPPER" not in conf

def test_unknown_key_not_in_conf():
    profile = {"CFLAGS": "-O2", "MY_WEIRD_KEY": "val"}
    with emit_makepkg_conf(profile, ["makepkg"]) as conf_path:
        conf = read_conf(conf_path)
    assert "CFLAGS" in conf
    assert "MY_WEIRD_KEY" not in conf

def test_rust_keys_written_when_rust_in_consumes():
    profile = {"RUSTFLAGS": "-C opt-level=3", "CFLAGS": "-O2"}
    with emit_makepkg_conf(profile, ["makepkg", "rust"]) as conf_path:
        conf = read_conf(conf_path)
    assert "RUSTFLAGS" in conf
    assert conf["RUSTFLAGS"] == "-C opt-level=3"

def test_rust_keys_not_written_when_rust_absent():
    profile = {"RUSTFLAGS": "-C opt-level=3", "CFLAGS": "-O2"}
    with emit_makepkg_conf(profile, ["makepkg"]) as conf_path:
        conf = read_conf(conf_path)
    assert "RUSTFLAGS" not in conf
    assert "CFLAGS" in conf

def test_makepkg_keys_written():
    profile = {"CFLAGS": "-O2 -march=native", "LDFLAGS": "-Wl,--as-needed"}
    with emit_makepkg_conf(profile, ["makepkg"]) as conf_path:
        conf = read_conf(conf_path)
    assert conf["CFLAGS"] == "-O2 -march=native"
    assert conf["LDFLAGS"] == "-Wl,--as-needed"

def test_fallback_mode_writes_all_non_env_non_sysforge():
    profile = {
        "CFLAGS": "-O2",
        "RUSTFLAGS": "-C opt-level=3",
        "RUSTC_WRAPPER": "sccache",   # env type — must NOT appear
        "build_mode": "patched_pkgbuild",  # sysforge key — must NOT appear
    }
    with emit_makepkg_conf(profile, None) as conf_path:
        conf = read_conf(conf_path)
    assert "CFLAGS" in conf
    assert "RUSTFLAGS" in conf
    assert "RUSTC_WRAPPER" not in conf
    assert "build_mode" not in conf

def test_conf_line_format():
    profile = {"CFLAGS": "-O2 -pipe"}
    with emit_makepkg_conf(profile, ["makepkg"]) as conf_path:
        raw = Path(conf_path).read_text()
    assert 'CFLAGS="-O2 -pipe"' in raw

def test_temp_file_cleaned_up():
    profile = {"CFLAGS": "-O2"}
    with emit_makepkg_conf(profile, ["makepkg"]) as conf_path:
        p = Path(conf_path)
        assert p.exists()
    assert not p.exists()

def test_sysforge_keys_not_in_conf():
    for key in _SYSFORGE_KEYS:
        profile = {key: "something", "CFLAGS": "-O2"}
        with emit_makepkg_conf(profile, None) as conf_path:
            conf = read_conf(conf_path)
        assert key not in conf, f"Sysforge key {key!r} leaked into conf"


# ---------------------------------------------------------------------------
# Integration: routing correctness
# ---------------------------------------------------------------------------

def test_rustc_wrapper_routes_to_env_not_conf():
    """RUSTC_WRAPPER with 'env' in consumes → env only, not conf."""
    profile = {"RUSTC_WRAPPER": "sccache", "CFLAGS": "-O2"}
    consumes = ["makepkg", "rust", "env"]

    env_result, _ = captured(resolve_env_vars, profile, consumes)
    with emit_makepkg_conf(profile, consumes) as conf_path:
        conf = read_conf(conf_path)

    assert "RUSTC_WRAPPER" in env_result
    assert "RUSTC_WRAPPER" not in conf

def test_rustflags_routes_to_conf_not_env():
    """RUSTFLAGS with 'rust' in consumes → conf only, not env."""
    profile = {"RUSTFLAGS": "-C opt-level=3", "CFLAGS": "-O2"}
    consumes = ["makepkg", "rust", "env"]

    env_result, _ = captured(resolve_env_vars, profile, consumes)
    with emit_makepkg_conf(profile, consumes) as conf_path:
        conf = read_conf(conf_path)

    assert "RUSTFLAGS" not in env_result
    assert "RUSTFLAGS" in conf

def test_rustflags_filtered_entirely_when_rust_not_in_consumes():
    """RUSTFLAGS with 'rust' NOT in consumes → neither conf nor env."""
    profile = {"RUSTFLAGS": "-C opt-level=3", "CFLAGS": "-O2"}
    consumes = ["makepkg"]   # no "rust"

    env_result, _ = captured(resolve_env_vars, profile, consumes)
    with emit_makepkg_conf(profile, consumes) as conf_path:
        conf = read_conf(conf_path)

    assert "RUSTFLAGS" not in env_result
    assert "RUSTFLAGS" not in conf

def test_cargo_package_full_routing():
    """Full cargo package: CFLAGS→conf, RUSTFLAGS→conf, RUSTC_WRAPPER→env."""
    profile = {
        "CFLAGS": "-O3 -march=native",
        "RUSTFLAGS": "-C target-cpu=native",
        "RUSTC_WRAPPER": "sccache",
        "CARGO_INCREMENTAL": "0",
        "build_mode": "patched_pkgbuild",
    }
    consumes = ["makepkg", "rust", "env"]

    env_result, _ = captured(resolve_env_vars, profile, consumes)
    with emit_makepkg_conf(profile, consumes) as conf_path:
        conf = read_conf(conf_path)

    assert conf["CFLAGS"] == "-O3 -march=native"
    assert conf["RUSTFLAGS"] == "-C target-cpu=native"
    assert conf["CARGO_INCREMENTAL"] == "0"
    assert "RUSTC_WRAPPER" not in conf
    assert "build_mode" not in conf

    assert env_result["RUSTC_WRAPPER"] == "sccache"
    assert "CFLAGS" not in env_result
    assert "RUSTFLAGS" not in env_result
    assert "build_mode" not in env_result


# ---------------------------------------------------------------------------
# compiler_flags_extra — PGO flag injection
# ---------------------------------------------------------------------------

def test_compiler_flags_extra_appended_to_profile_flags():
    """compiler_flags_extra is appended to existing profile CFLAGS/CXXFLAGS/LDFLAGS."""
    profile = {"CFLAGS": "-O3", "CXXFLAGS": "-O3", "LDFLAGS": "-fuse-ld=lld"}
    extra = "-fprofile-generate=/tmp/pgo"
    with emit_makepkg_conf(profile, None, compiler_flags_extra=extra) as conf_path:
        conf = read_conf(conf_path)
    assert "-O3 -fprofile-generate=/tmp/pgo" in conf["CFLAGS"]
    assert "-O3 -fprofile-generate=/tmp/pgo" in conf["CXXFLAGS"]
    assert "-fuse-ld=lld -fprofile-generate=/tmp/pgo" in conf["LDFLAGS"]


def test_compiler_flags_extra_skips_cxxflags_when_delegates_to_cflags():
    """When CXXFLAGS references $CFLAGS, compiler_flags_extra is not injected into
    CXXFLAGS separately — the CFLAGS injection is inherited through shell expansion."""
    profile = {"CFLAGS": "-O3", "CXXFLAGS": "$CFLAGS", "LDFLAGS": "-fuse-ld=lld"}
    extra = "-fprofile-use=/tmp/pgo/clang.profdata"
    with emit_makepkg_conf(profile, None, compiler_flags_extra=extra) as conf_path:
        conf = read_conf(conf_path)
    # CFLAGS and LDFLAGS must have the extra flag
    assert "-fprofile-use=" in conf["CFLAGS"]
    assert "-fprofile-use=" in conf["LDFLAGS"]
    # CXXFLAGS must NOT have the extra flag injected directly (keeps $CFLAGS reference)
    assert "-fprofile-use=" not in conf["CXXFLAGS"]
    assert "$CFLAGS" in conf["CXXFLAGS"]


def test_strip_full_lto_clears_ltoflags():
    """strip_full_lto=True sets LTOFLAGS to empty to prevent makepkg's lto option
    from re-injecting ThinLTO flags at build time."""
    profile = {"CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, None, strip_full_lto=True) as conf_path:
        conf = read_conf(conf_path)
    assert conf.get("LTOFLAGS") == ""


def test_compiler_flags_extra_no_base_creates_entry():
    """compiler_flags_extra with no existing value creates the key."""
    profile = {}
    extra = "-fprofile-instr-use=/tmp/pgo/clang.profdata -fprofile-correction"
    with emit_makepkg_conf(profile, None, compiler_flags_extra=extra) as conf_path:
        conf = read_conf(conf_path)
    assert "-fprofile-instr-use=" in conf.get("CFLAGS", "")
    assert "-fprofile-correction" in conf.get("CFLAGS", "")


def test_compiler_flags_extra_none_leaves_flags_unchanged():
    """compiler_flags_extra=None (default) does not modify any flags."""
    profile = {"CFLAGS": "-O3"}
    with emit_makepkg_conf(profile, None, compiler_flags_extra=None) as conf_path:
        conf = read_conf(conf_path)
    assert conf["CFLAGS"] == "-O3"


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
