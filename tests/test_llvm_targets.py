"""
test_llvm_targets.py — resolution logic for LLVM_TARGETS_TO_BUILD and the
pkgbuild_patcher injection that consumes it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.llvm_targets import resolve_llvm_targets
from sysforge.primitives.pkgbuild_patcher import (
    is_llvm_pkgbase,
    patch_llvm_targets,
)


# ---------------------------------------------------------------------------
# resolve_llvm_targets
# ---------------------------------------------------------------------------

def _write_toolchain(path, body):
    path.write_text(body)


def _write_hardware(path, llvm_targets=None):
    body = '[hardware]\nhost_arch = "x86_64"\n'
    if llvm_targets is not None:
        body += "llvm_targets = [{}]\n".format(
            ", ".join(f'"{t}"' for t in llvm_targets)
        )
    path.write_text(body)


def test_resolve_no_files_returns_none(tmp_path):
    """Both files absent → no filtering."""
    assert resolve_llvm_targets(
        tmp_path / "missing-tc.toml",
        tmp_path / "missing-hw.toml",
    ) is None


def test_resolve_hardware_only(tmp_path):
    """No toolchain override → use hardware autodetect."""
    hw = tmp_path / "hardware_profile.toml"
    _write_hardware(hw, ["X86", "AMDGPU", "NVPTX"])
    result = resolve_llvm_targets(tmp_path / "missing-tc.toml", hw)
    assert result == ["X86", "AMDGPU", "NVPTX"]


def test_resolve_explicit_override_wins(tmp_path):
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, '[llvm]\ntargets = ["X86", "AArch64"]\n')
    _write_hardware(hw, ["X86", "AMDGPU", "NVPTX"])
    result = resolve_llvm_targets(tc, hw)
    assert result == ["X86", "AArch64"]


def test_resolve_empty_override_disables_filtering(tmp_path):
    """[llvm] targets = [] means "force build all targets" → return None."""
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "[llvm]\ntargets = []\n")
    _write_hardware(hw, ["X86", "AMDGPU"])
    assert resolve_llvm_targets(tc, hw) is None


def test_resolve_section_absent_falls_through(tmp_path):
    """toolchain.toml without [llvm] section → fall through to hardware."""
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "enabled = false\n")
    _write_hardware(hw, ["X86"])
    assert resolve_llvm_targets(tc, hw) == ["X86"]


def test_resolve_malformed_toolchain_falls_through(tmp_path):
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "this is not valid TOML [[\n")
    _write_hardware(hw, ["X86"])
    assert resolve_llvm_targets(tc, hw) == ["X86"]


def test_resolve_targets_not_a_list_is_ignored(tmp_path):
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, '[llvm]\ntargets = "X86"\n')
    _write_hardware(hw, ["X86", "AMDGPU"])
    assert resolve_llvm_targets(tc, hw) == ["X86", "AMDGPU"]


# ---------------------------------------------------------------------------
# is_llvm_pkgbase
# ---------------------------------------------------------------------------

def test_is_llvm_pkgbase_matches():
    for name in ("llvm", "llvm-git", "lib32-llvm", "clang", "clang-git",
                "compiler-rt", "lld", "lib32-compiler-rt"):
        assert is_llvm_pkgbase(name), name


def test_is_llvm_pkgbase_rejects():
    for name in ("rust", "ocaml-llvm", "spirv-llvm-translator", "polly",
                 "openmp", "", None):
        assert not is_llvm_pkgbase(name), name


# ---------------------------------------------------------------------------
# patch_llvm_targets
# ---------------------------------------------------------------------------

_PKGBUILD_NO_TARGETS = """\
pkgname=llvm
pkgver=20.0.0
pkgrel=1
build() {
  cd "$srcdir/llvm"
  cmake -B build -S . \\
      -DCMAKE_BUILD_TYPE=Release \\
      -DLLVM_HOST_TRIPLE=x86_64-pc-linux-gnu
  ninja -C build
}
"""

_PKGBUILD_WITH_TARGETS = """\
pkgname=llvm
build() {
  cmake -B build -S . \\
      -DLLVM_TARGETS_TO_BUILD="X86;AArch64;ARM;RISCV;PowerPC" \\
      -DCMAKE_BUILD_TYPE=Release
  ninja -C build
}
"""

_PKGBUILD_NO_CMAKE = """\
pkgname=llvm
build() {
  meson setup build --buildtype=release
  meson compile -C build
}
"""


def test_patch_injects_when_absent(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)
    changed = patch_llvm_targets(p, ["X86", "AMDGPU", "NVPTX"])
    assert changed is True
    new = p.read_text()
    assert '-DLLVM_TARGETS_TO_BUILD="X86;AMDGPU;NVPTX"' in new
    # Existing args preserved.
    assert "-DCMAKE_BUILD_TYPE=Release" in new
    assert "-DLLVM_HOST_TRIPLE=x86_64-pc-linux-gnu" in new


def test_patch_replaces_when_present(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_WITH_TARGETS)
    changed = patch_llvm_targets(p, ["X86", "AMDGPU", "NVPTX"])
    assert changed is True
    new = p.read_text()
    assert '-DLLVM_TARGETS_TO_BUILD="X86;AMDGPU;NVPTX"' in new
    # Old value gone.
    assert "AArch64;ARM;RISCV" not in new


def test_patch_idempotent(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_WITH_TARGETS)
    patch_llvm_targets(p, ["X86", "AMDGPU", "NVPTX"])
    snapshot = p.read_text()
    second = patch_llvm_targets(p, ["X86", "AMDGPU", "NVPTX"])
    assert second is False
    assert p.read_text() == snapshot


def test_patch_no_cmake_logs_warn_and_skips(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_CMAKE)
    changed = patch_llvm_targets(p, ["X86"])
    assert changed is False
    assert p.read_text() == _PKGBUILD_NO_CMAKE


def test_patch_empty_targets_is_noop(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)
    changed = patch_llvm_targets(p, [])
    assert changed is False
    assert p.read_text() == _PKGBUILD_NO_TARGETS
