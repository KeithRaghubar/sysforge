"""
test_kernel_build.py — unit tests for kernel build mode behaviours:
  - emit_makepkg_conf flag key stripping (covered in test_system_conf.py)
  - LLVM env detection and injection via _run_build
  - kconfig patching toggled by interactive flag
  - _invoke_with_retry sudo timeout recovery
"""
import subprocess
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from sysforge.primitives.makepkg_wrapper import _find_built_packages, _invoke_with_retry, _run_build


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

    def fake_invoke(pb, conf, rp, extra_env, extra_flags, interactive, strip_flags=None):
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


# ---------------------------------------------------------------------------
# _find_built_packages
# ---------------------------------------------------------------------------

def test_find_built_packages_empty(tmp_path):
    assert _find_built_packages(tmp_path) == []


def test_find_built_packages_finds_pkg_files(tmp_path):
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()
    (tmp_path / "foo-debug-1.0-1-x86_64.pkg.tar.zst").touch()
    result = _find_built_packages(tmp_path)
    names = {p.name for p in result}
    assert "foo-1.0-1-x86_64.pkg.tar.zst" in names
    assert "foo-debug-1.0-1-x86_64.pkg.tar.zst" in names


def test_find_built_packages_excludes_sig(tmp_path):
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst.sig").touch()
    result = _find_built_packages(tmp_path)
    assert len(result) == 1
    assert result[0].name == "foo-1.0-1-x86_64.pkg.tar.zst"


def test_find_built_packages_ignores_unrelated(tmp_path):
    (tmp_path / "PKGBUILD").touch()
    (tmp_path / "src").mkdir()
    result = _find_built_packages(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# _invoke_with_retry — sudo timeout recovery
# ---------------------------------------------------------------------------

def _make_pkgbuild(tmp_path):
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=foo\npkgver=1\npkgrel=1\n")
    return pkgbuild


def test_invoke_retry_sudo_reauth_and_install(tmp_path):
    """'s' response with built packages → sudo -v + pacman -U, no rebuild."""
    pkgbuild = _make_pkgbuild(tmp_path)
    pkg_file = tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst"
    pkg_file.touch()

    profile = {}
    fail = subprocess.CalledProcessError(1, "makepkg")
    sudo_v_result = MagicMock(returncode=0)
    pacman_result = MagicMock(returncode=0)

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", side_effect=fail),
        patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
              side_effect=[sudo_v_result, pacman_result]) as mock_run,
        patch("builtins.input", return_value="s"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", profile,
                           extra_flags=["--install"])

    calls = mock_run.call_args_list
    assert calls[0] == call(["sudo", "-v"])
    assert calls[1][0][0][0:3] == ["sudo", "pacman", "-U"]
    assert str(pkg_file) in calls[1][0][0]


def test_invoke_retry_sudo_install_fails_then_abort(tmp_path):
    """pacman -U fails → inner loop prompts; 'abort' raises RuntimeError."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")
    pacman_fail = MagicMock(returncode=1)

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", side_effect=fail),
        patch("sysforge.primitives.makepkg_wrapper.subprocess.run",
              side_effect=[MagicMock(returncode=0), pacman_fail]),
        patch("builtins.input", side_effect=["s", "abort"]),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])


def test_invoke_retry_abort_with_built_packages(tmp_path):
    """'abort' response when packages are present → RuntimeError."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", side_effect=fail),
        patch("builtins.input", return_value="abort"),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])


def test_invoke_retry_enter_retries_build_with_packages(tmp_path):
    """Enter (empty) with built packages → full rebuild retry."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")
    invoke = MagicMock(side_effect=[fail, None])

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", invoke),
        patch("builtins.input", return_value=""),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {}, extra_flags=["--install"])

    assert invoke.call_count == 2


def test_invoke_retry_no_packages_prompts_fix_pkgbuild(tmp_path):
    """No built packages → existing 'fix PKGBUILD' prompt, no sudo option."""
    pkgbuild = _make_pkgbuild(tmp_path)

    fail = subprocess.CalledProcessError(1, "makepkg")
    invoke = MagicMock(side_effect=[fail, None])

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", invoke),
        patch("builtins.input", return_value="") as mock_input,
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {})

    assert invoke.call_count == 2
    prompt_text = mock_input.call_args[0][0]
    assert "sudo" not in prompt_text.lower() or "PKGBUILD" in prompt_text


def test_invoke_retry_batch_mode_ignores_packages(tmp_path):
    """batch=True → fails immediately even with built packages present."""
    pkgbuild = _make_pkgbuild(tmp_path)
    (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()

    fail = subprocess.CalledProcessError(1, "makepkg")

    with (
        patch("sysforge.primitives.makepkg_wrapper.invoke_makepkg", side_effect=fail),
        pytest.raises(RuntimeError, match="build_failed"),
    ):
        _invoke_with_retry(pkgbuild, "/tmp/fake.conf", {"batch": True})
