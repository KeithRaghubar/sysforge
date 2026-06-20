"""
test_mesa_drivers.py — hardware-filtering of mesa gallium/vulkan drivers: the
meson analogue of test_llvm_targets.py.

Covers derivation (hardware.derive_mesa_drivers), resolution
(mesa_drivers.resolve_*), the meson patcher (pkgbuild_patcher.patch_mesa_drivers
+ validate_patched_meson_pkgbuild) and the makepkg_wrapper wiring
(_maybe_patch_mesa_drivers). The load-bearing invariant throughout is the
*inverse* of the LLVM AMDGPU one: the mandatory software baseline
(gallium llvmpipe/softpipe/zink, vulkan swrast) is never dropped.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.pipeline.stages.hardware import derive_mesa_drivers
from sysforge.primitives.makepkg_wrapper import _maybe_patch_mesa_drivers
from sysforge.primitives.mesa_drivers import (
    resolve_mesa_drivers,
    resolve_or_detect_mesa_drivers,
)
from sysforge.primitives.pkgbuild_patcher import (
    PkgbuildPatchError,
    patch_mesa_drivers,
    validate_patched_meson_pkgbuild,
)

# Mandatory software baselines (kept in sync with hardware.py constants).
_BASE_GALLIUM = ["llvmpipe", "softpipe", "zink"]
_BASE_VULKAN = ["swrast"]


# ---------------------------------------------------------------------------
# derive_mesa_drivers — autodetected set incl. the mandatory software baseline.
#
# The baseline (gallium llvmpipe/softpipe/zink, vulkan swrast=lavapipe) must
# appear on EVERY host, even one with no GPU detected — dropping the software
# renderer bricks headless/VM/recovery. This is the inverse of derive_llvm's
# AMDGPU invariant (reduce-too-much instead of reduce-too-little).
# ---------------------------------------------------------------------------

def test_derive_amd_host():
    assert derive_mesa_drivers(["amd"]) == {
        "gallium": ["radeonsi", *_BASE_GALLIUM],
        "vulkan": ["amd", *_BASE_VULKAN],
    }


def test_derive_nvidia_host():
    assert derive_mesa_drivers(["nvidia"]) == {
        "gallium": ["nouveau", *_BASE_GALLIUM],
        "vulkan": ["nouveau", *_BASE_VULKAN],
    }


def test_derive_intel_host():
    assert derive_mesa_drivers(["intel"]) == {
        "gallium": ["iris", "crocus", *_BASE_GALLIUM],
        "vulkan": ["intel", "intel_hasvk", *_BASE_VULKAN],
    }


def test_derive_amd_nvidia_multi_gpu():
    assert derive_mesa_drivers(["amd", "nvidia"]) == {
        "gallium": ["radeonsi", "nouveau", *_BASE_GALLIUM],
        "vulkan": ["amd", "nouveau", *_BASE_VULKAN],
    }


def test_derive_headless_host_is_baseline_only():
    """No GPU detected → software baseline only (the correct minimum)."""
    assert derive_mesa_drivers([]) == {
        "gallium": _BASE_GALLIUM,
        "vulkan": _BASE_VULKAN,
    }


def test_derive_unknown_vendor_ignored_still_baseline():
    assert derive_mesa_drivers(["other"]) == {
        "gallium": _BASE_GALLIUM,
        "vulkan": _BASE_VULKAN,
    }


def test_derive_no_duplicate_baseline():
    """A vendor driver that's also a baseline driver isn't duplicated (none of
    the vendor drivers overlap the baseline today, but the dedup must hold)."""
    result = derive_mesa_drivers(["amd", "amd"])
    assert result["gallium"].count("radeonsi") == 1
    assert result["gallium"].count("llvmpipe") == 1


# ---------------------------------------------------------------------------
# resolve_mesa_drivers — opt-in switch + per-axis precedence + baseline.
# ---------------------------------------------------------------------------

def _write_sysforge(path, *, filter_drivers=None, gallium=None, vulkan=None):
    body = "[mesa]\n"
    if filter_drivers is not None:
        body += f"filter_drivers = {'true' if filter_drivers else 'false'}\n"
    if gallium is not None:
        body += "gallium = [{}]\n".format(", ".join(f'"{d}"' for d in gallium))
    if vulkan is not None:
        body += "vulkan = [{}]\n".format(", ".join(f'"{d}"' for d in vulkan))
    path.write_text(body)


def _write_hardware(path, *, gallium=None, vulkan=None):
    body = '[hardware]\nhost_arch = "x86_64"\n'
    if gallium is not None:
        body += "mesa_gallium_drivers = [{}]\n".format(
            ", ".join(f'"{d}"' for d in gallium)
        )
    if vulkan is not None:
        body += "mesa_vulkan_drivers = [{}]\n".format(
            ", ".join(f'"{d}"' for d in vulkan)
        )
    path.write_text(body)


def test_resolve_switch_off_returns_none(tmp_path):
    """Default (no [mesa] / filter_drivers absent) → no filtering."""
    assert resolve_mesa_drivers(
        tmp_path / "missing.toml", tmp_path / "missing-hw.toml"
    ) is None


def test_resolve_switch_explicitly_false_returns_none(tmp_path):
    sf = tmp_path / "sysforge.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_sysforge(sf, filter_drivers=False)
    _write_hardware(hw, gallium=["radeonsi"], vulkan=["amd"])
    assert resolve_mesa_drivers(sf, hw) is None


def test_resolve_switch_on_uses_hardware_profile(tmp_path):
    sf = tmp_path / "sysforge.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_sysforge(sf, filter_drivers=True)
    _write_hardware(hw, gallium=["radeonsi"], vulkan=["amd"])
    assert resolve_mesa_drivers(sf, hw) == {
        "gallium": ["radeonsi", *_BASE_GALLIUM],
        "vulkan": ["amd", *_BASE_VULKAN],
    }


def test_resolve_override_wins_over_profile(tmp_path):
    """Explicit [mesa] gallium/vulkan beats the autodetected profile, still
    baseline-enforced."""
    sf = tmp_path / "sysforge.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_sysforge(sf, filter_drivers=True, gallium=["radeonsi"], vulkan=["amd"])
    _write_hardware(hw, gallium=["nouveau"], vulkan=["nouveau"])
    assert resolve_mesa_drivers(sf, hw) == {
        "gallium": ["radeonsi", *_BASE_GALLIUM],
        "vulkan": ["amd", *_BASE_VULKAN],
    }


def test_resolve_override_missing_baseline_gets_it_appended(tmp_path):
    """An override that omits the software drivers is still augmented — the
    inverse-AMDGPU invariant is not user-overridable away."""
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True, gallium=["radeonsi"], vulkan=["amd"])
    result = resolve_mesa_drivers(sf, tmp_path / "missing-hw.toml")
    assert result is not None
    for d in _BASE_GALLIUM:
        assert d in result["gallium"]
    assert "swrast" in result["vulkan"]


def test_resolve_on_but_no_data_returns_none(tmp_path):
    """Switch on but neither override nor profile resolves an axis → None (the
    caller's resolve_or_detect then tries live detection)."""
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True)
    assert resolve_mesa_drivers(sf, tmp_path / "missing-hw.toml") is None


def test_resolve_malformed_sysforge_toml_is_off(tmp_path):
    sf = tmp_path / "sysforge.toml"
    sf.write_text("this is not valid TOML [[\n")
    assert resolve_mesa_drivers(sf, tmp_path / "missing-hw.toml") is None


def test_resolve_partial_profile_one_axis_missing_returns_none(tmp_path):
    """Only one axis in the profile → defer to live detection (return None)
    rather than ship a half-resolved reduction."""
    sf = tmp_path / "sysforge.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_sysforge(sf, filter_drivers=True)
    _write_hardware(hw, gallium=["radeonsi"])  # no vulkan axis
    assert resolve_mesa_drivers(sf, hw) is None


# ---------------------------------------------------------------------------
# resolve_or_detect_mesa_drivers — file-first, live lspci fallback.
# ---------------------------------------------------------------------------

def test_resolve_or_detect_off_short_circuits_no_subprocess(tmp_path):
    """Switch off → None and lspci is never run."""
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=False)
    with patch("sysforge.primitives.mesa_drivers.subprocess.run") as mock_run:
        assert resolve_or_detect_mesa_drivers(
            sf, tmp_path / "missing-hw.toml"
        ) is None
    mock_run.assert_not_called()


def test_resolve_or_detect_prefers_profile(tmp_path):
    sf = tmp_path / "sysforge.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_sysforge(sf, filter_drivers=True)
    _write_hardware(hw, gallium=["radeonsi"], vulkan=["amd"])
    with patch("sysforge.primitives.mesa_drivers.subprocess.run") as mock_run:
        result = resolve_or_detect_mesa_drivers(sf, hw)
    assert result == {
        "gallium": ["radeonsi", *_BASE_GALLIUM],
        "vulkan": ["amd", *_BASE_VULKAN],
    }
    mock_run.assert_not_called()


def test_resolve_or_detect_falls_back_to_live(tmp_path):
    """Switch on + no profile → live lspci detection drives the driver set."""
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True)
    fake_lspci = SimpleNamespace(
        returncode=0,
        stdout="01:00.0 VGA compatible controller: NVIDIA Corporation Foo\n",
    )
    with patch(
        "sysforge.primitives.mesa_drivers.subprocess.run",
        return_value=fake_lspci,
    ):
        result = resolve_or_detect_mesa_drivers(sf, tmp_path / "missing-hw.toml")
    assert result == {
        "gallium": ["nouveau", *_BASE_GALLIUM],
        "vulkan": ["nouveau", *_BASE_VULKAN],
    }


def test_resolve_or_detect_override_rides_on_top_of_live(tmp_path):
    """A per-axis override is honoured even when the other axis comes from live
    detection."""
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True, gallium=["radeonsi"])
    fake_lspci = SimpleNamespace(
        returncode=0,
        stdout="01:00.0 VGA compatible controller: NVIDIA Corporation Foo\n",
    )
    with patch(
        "sysforge.primitives.mesa_drivers.subprocess.run",
        return_value=fake_lspci,
    ):
        result = resolve_or_detect_mesa_drivers(sf, tmp_path / "missing-hw.toml")
    # gallium pinned by override; vulkan from live (nvidia → nouveau).
    assert result["gallium"] == ["radeonsi", *_BASE_GALLIUM]
    assert result["vulkan"] == ["nouveau", *_BASE_VULKAN]


def test_resolve_or_detect_lspci_failure_is_non_fatal(tmp_path):
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True)
    fake_lspci = SimpleNamespace(returncode=1, stdout="")
    with patch(
        "sysforge.primitives.mesa_drivers.subprocess.run",
        return_value=fake_lspci,
    ):
        result = resolve_or_detect_mesa_drivers(sf, tmp_path / "missing-hw.toml")
    # No GPU detected, but the software baseline is still produced.
    assert result == {"gallium": _BASE_GALLIUM, "vulkan": _BASE_VULKAN}


# ---------------------------------------------------------------------------
# patch_mesa_drivers — meson-array rewrite (gallium/vulkan + rusticl subset).
# ---------------------------------------------------------------------------

_MESA_PKGBUILD = """\
pkgname=mesa
pkgver=25.0.0
pkgrel=1
build() {
  local meson_options=(
    --optimization 2
    -D gallium-drivers=all
    -D gallium-rusticl-enable-drivers=asahi,freedreno,radeonsi
    -D gallium-rusticl=true
    -D vulkan-drivers=amd,intel,intel_hasvk,swrast,freedreno,panfrost,nouveau,asahi
    -D video-codecs=all
  )
  arch-meson mesa-$pkgver build "${meson_options[@]}"
  meson compile -C build
}
"""


def test_patch_rewrites_gallium_and_vulkan(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    changed = patch_mesa_drivers(
        p, ["radeonsi", *_BASE_GALLIUM], ["amd", *_BASE_VULKAN]
    )
    assert changed is True
    new = p.read_text()
    assert "-D gallium-drivers=radeonsi,llvmpipe,softpipe,zink" in new
    assert "-D vulkan-drivers=amd,swrast" in new
    # Untouched options preserved.
    assert "-D video-codecs=all" in new
    assert "gallium-drivers=all" not in new


def test_patch_rusticl_intersected_with_new_gallium(tmp_path):
    """rusticl drivers must be a subset of built gallium — AMD host keeps
    radeonsi, drops asahi/freedreno (not built)."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    patch_mesa_drivers(p, ["radeonsi", *_BASE_GALLIUM], ["amd", *_BASE_VULKAN])
    assert "-D gallium-rusticl-enable-drivers=radeonsi" in p.read_text()


def test_patch_rusticl_empty_intersection_falls_back_to_llvmpipe(tmp_path):
    """Nvidia host: none of asahi/freedreno/radeonsi are built → rusticl falls
    back to llvmpipe (always in the baseline, a valid rusticl driver) so
    gallium-rusticl=true stays satisfiable."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    patch_mesa_drivers(p, ["nouveau", *_BASE_GALLIUM], ["nouveau", *_BASE_VULKAN])
    assert "-D gallium-rusticl-enable-drivers=llvmpipe" in p.read_text()


def test_patch_idempotent(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    patch_mesa_drivers(p, ["radeonsi", *_BASE_GALLIUM], ["amd", *_BASE_VULKAN])
    snapshot = p.read_text()
    second = patch_mesa_drivers(p, ["radeonsi", *_BASE_GALLIUM], ["amd", *_BASE_VULKAN])
    assert second is False
    assert p.read_text() == snapshot


def test_patch_single_axis_only(tmp_path):
    """Passing only gallium leaves vulkan-drivers untouched."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    changed = patch_mesa_drivers(p, ["radeonsi", *_BASE_GALLIUM], None)
    assert changed is True
    new = p.read_text()
    assert "-D gallium-drivers=radeonsi,llvmpipe,softpipe,zink" in new
    # vulkan line unchanged (still the upstream long list).
    assert "-D vulkan-drivers=amd,intel,intel_hasvk,swrast" in new


def test_patch_both_none_is_noop(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    assert patch_mesa_drivers(p, None, None) is False
    assert p.read_text() == _MESA_PKGBUILD


def test_patch_bad_token_skips_axis(tmp_path):
    """An unrecognised driver token (typo'd override) skips that axis's rewrite
    rather than injecting a value that aborts arch-meson."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_MESA_PKGBUILD)
    changed = patch_mesa_drivers(
        p, ["radeonsi", "bogus-driver", *_BASE_GALLIUM], ["amd", *_BASE_VULKAN]
    )
    # vulkan still rewrites; gallium is skipped (still 'all').
    assert changed is True
    new = p.read_text()
    assert "-D gallium-drivers=all" in new
    assert "-D vulkan-drivers=amd,swrast" in new


def test_patch_missing_option_warns_and_skips(tmp_path):
    """No gallium-drivers option present → that axis is a no-op (upstream may
    have renamed it)."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(
        "pkgname=mesa\nbuild() {\n  local meson_options=(\n"
        "    -D vulkan-drivers=amd,intel\n  )\n}\n"
    )
    changed = patch_mesa_drivers(p, ["radeonsi", *_BASE_GALLIUM], None)
    assert changed is False


# ---------------------------------------------------------------------------
# validate_patched_meson_pkgbuild — post-rewrite structural gate.
# ---------------------------------------------------------------------------

def test_validate_passes_on_clean_rewrite(tmp_path):
    original = tmp_path / "PKGBUILD"
    original.write_text(_MESA_PKGBUILD)
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(_MESA_PKGBUILD)
    gallium = ["radeonsi", *_BASE_GALLIUM]
    vulkan = ["amd", *_BASE_VULKAN]
    assert patch_mesa_drivers(patched, gallium, vulkan) is True
    validate_patched_meson_pkgbuild(original, patched, gallium, vulkan)  # no raise


def test_validate_rejects_dropped_software_baseline(tmp_path):
    """A patched file whose gallium line lost the software drivers must be
    rejected — the inverse-AMDGPU brick."""
    original = tmp_path / "PKGBUILD"
    original.write_text(_MESA_PKGBUILD)
    patched = tmp_path / "PKGBUILD.sysforge"
    # Hand-craft a gallium line with NO software driver.
    patched.write_text(
        _MESA_PKGBUILD.replace(
            "-D gallium-drivers=all", "-D gallium-drivers=radeonsi"
        )
    )
    with pytest.raises(PkgbuildPatchError, match="mandatory software"):
        validate_patched_meson_pkgbuild(
            original, patched, ["radeonsi", *_BASE_GALLIUM], None
        )


def test_validate_rejects_all_sentinel_left(tmp_path):
    """If the rewrite somehow left `all`, that's a failed reduction → reject."""
    original = tmp_path / "PKGBUILD"
    original.write_text(_MESA_PKGBUILD)
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(_MESA_PKGBUILD)  # untouched: gallium-drivers=all
    with pytest.raises(PkgbuildPatchError, match="all"):
        validate_patched_meson_pkgbuild(
            original, patched, ["radeonsi", *_BASE_GALLIUM], None
        )


def test_validate_globals_unchanged_reused_g1(tmp_path):
    """G1 (reused from validate_patched_pkgbuild) catches a rewrite that mangled
    a global like pkgname."""
    original = tmp_path / "PKGBUILD"
    original.write_text(_MESA_PKGBUILD)
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(_MESA_PKGBUILD.replace("pkgname=mesa", "pkgname=mesa-evil"))
    with pytest.raises(PkgbuildPatchError, match="pkgname"):
        validate_patched_meson_pkgbuild(original, patched, ["radeonsi"], None)


# ---------------------------------------------------------------------------
# _maybe_patch_mesa_drivers — makepkg_wrapper wiring (opt-in, pkgbase-gated).
# ---------------------------------------------------------------------------

def _mesa_path_with_profile(tmp_path, *, filter_drivers, gallium, vulkan):
    pkgbuild = tmp_path / "PKGBUILD.sysforge"
    pkgbuild.write_text(_MESA_PKGBUILD)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_hardware(
        state_dir / "hardware_profile.toml", gallium=gallium, vulkan=vulkan
    )
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=filter_drivers)
    return pkgbuild, state_dir, sf


def test_maybe_patch_mesa_filters_when_on(tmp_path):
    pkgbuild, state_dir, sf = _mesa_path_with_profile(
        tmp_path, filter_drivers=True, gallium=["radeonsi"], vulkan=["amd"]
    )
    pkgmeta = {"globals": {"pkgname": ["mesa", "lib32-mesa"]}}
    with patch("sysforge.primitives.makepkg_wrapper.SYSFORGE_TOML_PATH", sf):
        result = _maybe_patch_mesa_drivers(
            pkgbuild, pkgmeta, state_dir_override=state_dir
        )
    assert result is not None
    new = pkgbuild.read_text()
    assert "-D gallium-drivers=radeonsi,llvmpipe,softpipe,zink" in new
    assert "-D vulkan-drivers=amd,swrast" in new


def test_maybe_patch_mesa_noop_when_off(tmp_path):
    """Opt-in default: switch off → no patch at all."""
    pkgbuild, state_dir, sf = _mesa_path_with_profile(
        tmp_path, filter_drivers=False, gallium=["radeonsi"], vulkan=["amd"]
    )
    pkgmeta = {"globals": {"pkgname": "mesa"}}
    with patch("sysforge.primitives.makepkg_wrapper.SYSFORGE_TOML_PATH", sf):
        result = _maybe_patch_mesa_drivers(
            pkgbuild, pkgmeta, state_dir_override=state_dir
        )
    assert result is None
    assert pkgbuild.read_text() == _MESA_PKGBUILD


def test_maybe_patch_mesa_skips_non_mesa(tmp_path):
    pkgbuild, state_dir, sf = _mesa_path_with_profile(
        tmp_path, filter_drivers=True, gallium=["radeonsi"], vulkan=["amd"]
    )
    pkgmeta = {"globals": {"pkgname": "htop"}}
    with patch("sysforge.primitives.makepkg_wrapper.SYSFORGE_TOML_PATH", sf):
        result = _maybe_patch_mesa_drivers(
            pkgbuild, pkgmeta, state_dir_override=state_dir
        )
    assert result is None
    assert pkgbuild.read_text() == _MESA_PKGBUILD


def test_maybe_patch_mesa_includes_lib32(tmp_path):
    """Unlike lib32-llvm, lib32-mesa IS filtered (vendor- not arch-coupled)."""
    pkgbuild, state_dir, sf = _mesa_path_with_profile(
        tmp_path, filter_drivers=True, gallium=["radeonsi"], vulkan=["amd"]
    )
    pkgmeta = {"globals": {"pkgname": "lib32-mesa"}}
    with patch("sysforge.primitives.makepkg_wrapper.SYSFORGE_TOML_PATH", sf):
        result = _maybe_patch_mesa_drivers(
            pkgbuild, pkgmeta, state_dir_override=state_dir
        )
    assert result is not None
    assert "-D gallium-drivers=radeonsi,llvmpipe,softpipe,zink" in pkgbuild.read_text()


def test_maybe_patch_mesa_live_detect_fallback(tmp_path):
    """mesa pkgbase + switch on + no profile → live lspci detection patches."""
    pkgbuild = tmp_path / "PKGBUILD.sysforge"
    pkgbuild.write_text(_MESA_PKGBUILD)
    state_dir = tmp_path / "state"
    state_dir.mkdir()  # no hardware_profile.toml
    sf = tmp_path / "sysforge.toml"
    _write_sysforge(sf, filter_drivers=True)
    pkgmeta = {"globals": {"pkgname": "mesa"}}
    fake_lspci = SimpleNamespace(
        returncode=0,
        stdout="0d:00.0 VGA compatible controller: Advanced Micro Devices AMD Radeon\n",
    )
    with patch(
        "sysforge.primitives.makepkg_wrapper.SYSFORGE_TOML_PATH", sf
    ), patch(
        "sysforge.primitives.mesa_drivers.subprocess.run", return_value=fake_lspci
    ):
        result = _maybe_patch_mesa_drivers(
            pkgbuild, pkgmeta, state_dir_override=state_dir
        )
    assert result is not None
    assert "-D gallium-drivers=radeonsi,llvmpipe,softpipe,zink" in pkgbuild.read_text()
