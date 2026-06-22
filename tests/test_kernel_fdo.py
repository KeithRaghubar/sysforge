# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/kernel_fdo.py — kernel AutoFDO / Propeller orchestration."""
from pathlib import Path

import pytest

from sysforge.primitives import kernel_fdo


# ---------------------------------------------------------------------------
# Store resolution + build-mode
# ---------------------------------------------------------------------------

def test_store_is_method_subdir_namespaced_by_pkgname():
    autofdo = kernel_fdo.resolve_store("linux-custom", propeller=False, tcfg={})
    assert autofdo.name == "linux-custom"
    assert autofdo.parent.name == "autofdo"
    prop = kernel_fdo.resolve_store("linux-custom", propeller=True, tcfg={})
    assert prop.parent.name == "propeller"


def test_store_honours_profile_store_override(tmp_path):
    store = kernel_fdo.resolve_store(
        "linux", propeller=False, tcfg={"profile_store": str(tmp_path)}
    )
    assert store == tmp_path / "autofdo" / "linux"


def test_build_mode_maps_propeller():
    assert kernel_fdo.build_mode(propeller=False) == "autofdo_kernel"
    assert kernel_fdo.build_mode(propeller=True) == "propeller_kernel"


def test_build_modes_are_optimized_and_coexist():
    from sysforge.primitives.profile import (
        is_optimized_build_mode,
        rename_mode_for_build_mode,
    )

    for mode in (kernel_fdo.BUILD_MODE_AUTOFDO, kernel_fdo.BUILD_MODE_PROPELLER):
        assert is_optimized_build_mode(mode)
        # Kernel FDO installs alongside the stock kernel for bootloader fallback.
        assert rename_mode_for_build_mode(mode) == "coexist"


# ---------------------------------------------------------------------------
# kconfig + use-env
# ---------------------------------------------------------------------------

def test_fdo_kconfig_base_and_propeller():
    assert kernel_fdo.fdo_kconfig(propeller=False) == {"CONFIG_AUTOFDO_CLANG": "y"}
    assert kernel_fdo.fdo_kconfig(propeller=True) == {
        "CONFIG_AUTOFDO_CLANG": "y",
        "CONFIG_PROPELLER_CLANG": "y",
    }


def test_use_env_autofdo_only(tmp_path):
    env = kernel_fdo.use_env(tmp_path, propeller=False)
    assert env == {"CLANG_AUTOFDO_PROFILE": str(tmp_path / "kernel.afdo")}


def test_use_env_propeller_adds_prefix(tmp_path):
    env = kernel_fdo.use_env(tmp_path, propeller=True)
    assert env["CLANG_AUTOFDO_PROFILE"].endswith("kernel.afdo")
    assert env["CLANG_PROPELLER_PROFILE_PREFIX"] == str(tmp_path / "propeller")


# ---------------------------------------------------------------------------
# require_profile — the fail-fast guard for `--autofdo=use`
# ---------------------------------------------------------------------------

def test_require_profile_missing_autofdo_raises(tmp_path):
    with pytest.raises(kernel_fdo.KernelFdoError) as exc:
        kernel_fdo.require_profile(tmp_path, propeller=False)
    assert "--autofdo=record" in str(exc.value)


def test_require_profile_present_autofdo_ok(tmp_path):
    (tmp_path / "kernel.afdo").write_text("x")
    kernel_fdo.require_profile(tmp_path, propeller=False)  # no raise


def test_require_profile_propeller_needs_both_files(tmp_path):
    (tmp_path / "kernel.afdo").write_text("x")
    # afdo present but propeller pair missing → still raises
    with pytest.raises(kernel_fdo.KernelFdoError) as exc:
        kernel_fdo.require_profile(tmp_path, propeller=True)
    assert "Propeller" in str(exc.value)
    # complete the pair → ok
    (tmp_path / "propeller_cc_profile.txt").write_text("c")
    (tmp_path / "propeller_ld_profile.txt").write_text("l")
    kernel_fdo.require_profile(tmp_path, propeller=True)


# ---------------------------------------------------------------------------
# detect_branch_sampling — uarch event resolution (BRS vs LBR)
# ---------------------------------------------------------------------------

def test_detect_amd_zen3_brs_supported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: AuthenticAMD\ncpu family\t: 25\n"
    )
    assert bs.vendor == "amd" and bs.supported
    assert "pfm" in bs.perf_event_args.lower()
    assert "experimental" in bs.note.lower()


def test_detect_amd_pre_zen3_unsupported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: AuthenticAMD\ncpu family\t: 23\n"  # Zen/Zen2 → no BRS
    )
    assert bs.vendor == "amd" and not bs.supported


def test_detect_intel_lbr_supported():
    bs = kernel_fdo.detect_branch_sampling(
        "vendor_id\t: GenuineIntel\ncpu family\t: 6\n"
    )
    assert bs.vendor == "intel" and bs.supported
    assert "BR_INST_RETIRED" in bs.perf_event_args


def test_detect_unknown_vendor_unsupported():
    bs = kernel_fdo.detect_branch_sampling("model name\t: Something\n")
    assert bs.vendor == "unknown" and not bs.supported


# ---------------------------------------------------------------------------
# resolve_vmlinux
# ---------------------------------------------------------------------------

def test_resolve_vmlinux_from_build_tree(tmp_path):
    builddir = tmp_path / "builds"
    src = builddir / "linux-custom" / "src" / "linux-6.x"
    src.mkdir(parents=True)
    vmlinux = src / "vmlinux"
    vmlinux.write_text("ELF")
    found = kernel_fdo.resolve_vmlinux("linux-custom", builddir=builddir)
    assert found == vmlinux


def test_resolve_vmlinux_none_when_absent(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    # Empty builddir + force the running-kernel fallback to a release whose
    # /usr/lib/modules/<rel>/build/vmlinux does not exist → None.
    monkeypatch.setattr(
        os, "uname", lambda: SimpleNamespace(release="bogus-kernel-xyz-9999")
    )
    assert kernel_fdo.resolve_vmlinux("nonexistent-kernel-xyz", builddir=tmp_path) is None


# ---------------------------------------------------------------------------
# capture_commands — printed perf + create_llvm_prof block
# ---------------------------------------------------------------------------

def test_capture_commands_autofdo_only(tmp_path):
    bs = kernel_fdo.detect_branch_sampling("vendor_id\t: GenuineIntel\ncpu family\t: 6\n")
    lines = kernel_fdo.capture_commands(
        tmp_path, sampling=bs, vmlinux=Path("/b/vmlinux"), propeller=False
    )
    joined = "\n".join(lines)
    assert "perf record" in joined
    assert bs.perf_event_args in joined
    assert "create_llvm_prof" in joined and "--format=extbinary" in joined
    assert str(tmp_path / "kernel.afdo") in joined
    # No Propeller invocation when not requested.
    assert "--format=propeller" not in joined


def test_capture_commands_propeller_adds_second_conversion(tmp_path):
    bs = kernel_fdo.detect_branch_sampling("vendor_id\t: AuthenticAMD\ncpu family\t: 25\n")
    lines = kernel_fdo.capture_commands(
        tmp_path, sampling=bs, vmlinux=None, propeller=True
    )
    joined = "\n".join(lines)
    assert "--format=propeller" in joined
    assert "propeller_cc_profile.txt" in joined
    assert "propeller_ld_profile.txt" in joined
    # vmlinux=None → placeholder, not a crash.
    assert "<path-to-vmlinux>" in joined
