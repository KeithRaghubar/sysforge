#!/usr/bin/env python3
"""
End-to-end tests for the lib32-* + rust build path.

Asserts the chain from PKGBUILD parsing to makepkg.conf emission:

  - parse_pkgbuild merges ``makedepends_x86_64`` into ``makedepends``.
  - resolve_consumes recognises ``lib32-rust`` and yields ``{makepkg,
    rust, env}``.
  - resolve_profile lands on ``bare`` via the ``lib32-*`` rule.
  - collect_required_toolchains emits a pinned cross-probe token from the
    PKGBUILD's ``RUSTUP_TOOLCHAIN`` export.
  - emit_makepkg_conf(is_lib32=True) writes no RUSTFLAGS (bare profile is
    silent on rust keys) and scrubs ``-march=native`` from the system-conf
    CFLAGS passthrough.

These tests guard against silent regressions in any one link; the static
preflight alone passes today even when a downstream stage is broken, so the
chain itself needs explicit coverage.
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.primitives.config import load_consumes_inference
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.profile import merge_extends, resolve_consumes
from sysforge.primitives.toolchain_preflight import collect_required_toolchains

# Built-in defaults match the production inference map. Passing empty paths
# returns the fallback dict.
INFERENCE = load_consumes_inference(paths=[])

TESTS_DIR = Path(__file__).parent
PKGBUILDS_DIR = TESTS_DIR / "data/PKGBUILDs"
LIB32_STUB = PKGBUILDS_DIR / "lib32-rust-stub.PKGBUILD"

# Mirror of the [profiles.*] tables in tests/data/etc/sysforge/profiles.toml,
# narrowed to what this test exercises. The lib32-* rule resolves to bare.
PROFILES = {
    "bare": {
        "BUILDDIR": "$HOME/builds",
    },
    "standard": {
        "extends": "bare",
        "CC": "clang",
        "CXX": "clang++",
        "CFLAGS": "-march=native -O2 -pipe",
        "RUSTFLAGS": "-C opt-level=3 -C target-cpu=native",
    },
}

RUSTUP_PIN_RE = re.compile(r"RUSTUP_TOOLCHAIN[ \t]*=[ \t]*[\"']?([^\s\"';#]+)")


def _read_conf(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        result[key] = val.strip('"')
    return result


# ---------------------------------------------------------------------------
# Step 1: parse merges arch-specific makedepends
# ---------------------------------------------------------------------------

def test_step1_parse_merges_arch_makedepends():
    pkgmeta = parse_pkgbuild(LIB32_STUB)
    md = pkgmeta["globals"].get("makedepends", [])
    # ``lib32-rust`` is declared only via ``makedepends_x86_64`` in the
    # fixture, so seeing it here proves the merge fired.
    assert "lib32-rust" in md
    assert "meson" in md


# ---------------------------------------------------------------------------
# Step 2: consumes inference picks up lib32-rust
# ---------------------------------------------------------------------------

def test_step2_consumes_includes_rust_from_lib32_rust():
    pkgmeta = parse_pkgbuild(LIB32_STUB)
    active = resolve_consumes({}, pkgmeta, INFERENCE)
    assert "rust" in active
    assert "meson" in active
    assert "makepkg" in active
    assert "env" in active


# ---------------------------------------------------------------------------
# Step 3: profile resolution lands on bare
# ---------------------------------------------------------------------------

def test_step3_bare_profile_has_no_rust_or_cflags_keys():
    """``bare`` profile carries only BUILDDIR. If a future change introduces
    flags on it, lib32-* builds inherit them — this asserts the contract."""
    resolved = merge_extends("bare", PROFILES, conflict_groups={})
    assert "RUSTFLAGS" not in resolved
    assert "CFLAGS" not in resolved
    assert resolved.get("BUILDDIR")


# ---------------------------------------------------------------------------
# Step 4: cross-probe token is pinned from the PKGBUILD
# ---------------------------------------------------------------------------

def test_step4_cross_probe_token_includes_pin():
    pkgmeta = parse_pkgbuild(LIB32_STUB)
    active = resolve_consumes({}, pkgmeta, INFERENCE)
    build_body = pkgmeta["functions"].get("build", "")
    pin_match = RUSTUP_PIN_RE.search(build_body)
    assert pin_match, "fixture must declare RUSTUP_TOOLCHAIN in build()"
    pin = pin_match.group(1)

    pkgname = "lib32-rust-stub"
    per_pkg = {pkgname: active}
    pins = {pkgname: pin}
    required = collect_required_toolchains(per_pkg, frozenset({pkgname}), pins)

    assert f"rust:cross:i686-unknown-linux-gnu@{pin}" in required
    assert "rust:cross:i686-unknown-linux-gnu" not in required  # pin wins


# ---------------------------------------------------------------------------
# Step 5: emit_makepkg_conf — no RUSTFLAGS + CFLAGS scrub
# ---------------------------------------------------------------------------

def _write_system_conf(tmp_path: Path, cflags: str) -> Path:
    p = tmp_path / "system-makepkg.conf"
    p.write_text(
        f'CARCH="i686"\n'
        f'CHOST="i686-pc-linux-gnu"\n'
        f'CFLAGS="{cflags}"\n'
        f'CXXFLAGS="{cflags}"\n'
        f'LDFLAGS="-Wl,--as-needed"\n'
    )
    return p


def test_step5_no_rustflags_emitted_when_bare_profile():
    """Even with rust in consumes, bare profile has no RUSTFLAGS to deliver."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-O2 -pipe")
        resolved = merge_extends("bare", PROFILES, conflict_groups={})
        active = frozenset({"makepkg", "rust", "env"})
        with emit_makepkg_conf(
            resolved, active,
            system_conf_path=str(system_conf),
            is_lib32=True,
        ) as conf_path:
            conf = _read_conf(conf_path)
    assert "RUSTFLAGS" not in conf


def test_step5_lib32_scrubs_march_native_from_system_cflags():
    """is_lib32=True strips -march=native from system-conf CFLAGS passthrough."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-march=native -O2 -pipe")
        resolved = merge_extends("bare", PROFILES, conflict_groups={})
        active = frozenset({"makepkg", "rust", "env"})
        with emit_makepkg_conf(
            resolved, active,
            system_conf_path=str(system_conf),
            is_lib32=True,
        ) as conf_path:
            text = Path(conf_path).read_text()
    assert "-march=native" not in text
    # The remaining CFLAGS content survives.
    assert "-O2" in text
    assert "-pipe" in text


def test_step5_lib32_scrubs_x86_64_isa_levels():
    """64-bit ISA microarch levels (x86-64-v3 etc.) are stripped for lib32."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-march=x86-64-v3 -O2")
        resolved = merge_extends("bare", PROFILES, conflict_groups={})
        with emit_makepkg_conf(
            resolved, frozenset({"makepkg", "env"}),
            system_conf_path=str(system_conf),
            is_lib32=True,
        ) as conf_path:
            text = Path(conf_path).read_text()
    assert "-march=x86-64-v3" not in text
    assert "-O2" in text


def test_step5_non_lib32_keeps_march_native():
    """Default is_lib32=False must leave system CFLAGS untouched."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-march=native -O2")
        resolved = merge_extends("bare", PROFILES, conflict_groups={})
        with emit_makepkg_conf(
            resolved, frozenset({"makepkg", "env"}),
            system_conf_path=str(system_conf),
        ) as conf_path:
            text = Path(conf_path).read_text()
    assert "-march=native" in text


def test_step5_lib32_preserves_i686_march():
    """An explicit -march=i686 is not in the scrub set and must pass through."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-march=i686 -O2")
        resolved = merge_extends("bare", PROFILES, conflict_groups={})
        with emit_makepkg_conf(
            resolved, frozenset({"makepkg", "env"}),
            system_conf_path=str(system_conf),
            is_lib32=True,
        ) as conf_path:
            text = Path(conf_path).read_text()
    assert "-march=i686" in text


def test_step5_lib32_scrubs_march_native_from_profile_override():
    """When a profile sets CFLAGS=-march=native (e.g. ``standard`` resolves
    with this value), is_lib32=True scrubs it too."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        system_conf = _write_system_conf(tmp, "-O2")
        # Pretend a rule mistakenly routed lib32-* onto ``standard`` — the
        # scrub must still defend the build.
        resolved = merge_extends("standard", PROFILES, conflict_groups={})
        with emit_makepkg_conf(
            resolved, frozenset({"makepkg", "env"}),
            system_conf_path=str(system_conf),
            is_lib32=True,
        ) as conf_path:
            text = Path(conf_path).read_text()
    # The profile override line was scrubbed.
    cflags_line = next(
        (line for line in text.splitlines() if line.startswith("CFLAGS=")),
        "",
    )
    assert "-march=native" not in cflags_line
    # But other CFLAGS content survives.
    assert "-O2" in cflags_line
    assert "-pipe" in cflags_line
