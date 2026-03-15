"""
test_kernel_build.py — unit tests for kernel build mode behaviours:
  - emit_makepkg_conf flag key stripping (covered in test_system_conf.py)
  - LLVM env detection and injection via _run_build
  - kconfig patching toggled by interactive flag
"""
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.makepkg_wrapper import _run_build


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_profile():
    return {"batch": True}


def _minimal_pkgmeta():
    return {"globals": {"pkgname": "linux-custom"}}


@contextmanager
def _mock_build_context(tmp_path, profile=None, extra_env_out=None):
    """
    Set up a minimal _run_build call with the build machinery mocked out.
    Writes a PKGBUILD.sysforge into tmp_path.
    Captures the extra_env passed to _invoke_with_retry via extra_env_out list.
    """
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=linux-custom\npkgver=1\npkgrel=1\n")
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text("pkgname=linux-custom\npkgver=1\npkgrel=1\n")

    captured = {}

    @contextmanager
    def fake_emit(*args, **kwargs):
        yield "/tmp/fake_makepkg.conf"

    def fake_invoke(pb, conf, rp, extra_env, extra_flags, interactive):
        captured["extra_env"] = dict(extra_env or {})

    with (
        patch("sysforge.primitives.makepkg_wrapper.apply_patch_pkgbuild",
              return_value=patched),
        patch("sysforge.primitives.makepkg_wrapper.patch_pkgbuild_groups",
              return_value=patched),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig"),
        patch("sysforge.primitives.makepkg_wrapper.emit_makepkg_conf",
              side_effect=fake_emit),
        patch("sysforge.primitives.makepkg_wrapper.resolve_env_vars",
              return_value={}),
        patch("sysforge.primitives.makepkg_wrapper._invoke_with_retry",
              side_effect=fake_invoke),
        patch("sysforge.primitives.makepkg_wrapper.cleanup_patch_artifacts"),
        patch("sysforge.primitives.makepkg_wrapper.handle_failure"),
    ):
        yield pkgbuild, captured


# ---------------------------------------------------------------------------
# LLVM detection
# ---------------------------------------------------------------------------

def test_kernel_clang_cc_override_injects_llvm(tmp_path):
    """cc_override=clang → LLVM=1 and LLVM_IAS=1 injected into env."""
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   cc_override="clang", kernel_build=True)
    assert captured["extra_env"].get("LLVM") == "1"
    assert captured["extra_env"].get("LLVM_IAS") == "1"


def test_kernel_clang_full_path_injects_llvm(tmp_path):
    """/usr/bin/clang in profile CC triggers LLVM injection."""
    profile = {**_minimal_profile(), "CC": "/usr/bin/clang"}
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, profile, {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert captured["extra_env"].get("LLVM") == "1"
    assert captured["extra_env"].get("LLVM_IAS") == "1"


def test_kernel_gcc_no_llvm_injection(tmp_path):
    """GCC toolchain: no LLVM env vars injected."""
    profile = {**_minimal_profile(), "CC": "gcc"}
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, profile, {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert "LLVM" not in captured["extra_env"]
    assert "LLVM_IAS" not in captured["extra_env"]


def test_kernel_no_cc_no_llvm_injection(tmp_path, monkeypatch):
    """No CC anywhere: no LLVM env vars injected."""
    monkeypatch.delenv("CC", raising=False)
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=True)
    assert "LLVM" not in captured["extra_env"]


def test_non_kernel_build_no_llvm_injection(tmp_path):
    """kernel_build=False: LLVM vars never injected even with clang."""
    with _mock_build_context(tmp_path) as (pkgbuild, captured):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   cc_override="clang", kernel_build=False)
    assert "LLVM" not in captured["extra_env"]


# ---------------------------------------------------------------------------
# kconfig patching toggle
# ---------------------------------------------------------------------------

def test_kernel_non_interactive_patches_kconfig(tmp_path):
    """kernel_build=True, interactive=False → patch_noninteractive_kconfig called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   interactive=False, kernel_build=True)
    mock_patch.assert_called_once()


def test_kernel_interactive_skips_kconfig_patch(tmp_path):
    """kernel_build=True, interactive=True → patch_noninteractive_kconfig NOT called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile={}, pkgmeta=_minimal_pkgmeta(),
                   interactive=True, kernel_build=True)
    mock_patch.assert_not_called()


def test_non_kernel_build_never_patches_kconfig(tmp_path):
    """kernel_build=False → patch_noninteractive_kconfig never called."""
    with (
        _mock_build_context(tmp_path) as (pkgbuild, _),
        patch("sysforge.primitives.makepkg_wrapper.patch_noninteractive_kconfig") as mock_patch,
    ):
        _run_build(pkgbuild, _minimal_profile(), {}, [],
                   extracted_profile=None, pkgmeta=_minimal_pkgmeta(),
                   kernel_build=False)
    mock_patch.assert_not_called()
