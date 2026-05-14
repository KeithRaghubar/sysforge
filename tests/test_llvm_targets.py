"""
test_llvm_targets.py — resolution logic for LLVM_TARGETS_TO_BUILD and the
pkgbuild_patcher injection that consumes it.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


from sysforge.primitives.llvm_targets import resolve_llvm_targets
from sysforge.primitives.makepkg_wrapper import _maybe_patch_llvm_targets
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


# ---------------------------------------------------------------------------
# _maybe_patch_llvm_targets — wiring used by both `update --patch-pkgbuild`
# and `run toolchain` paths in makepkg_wrapper._run_build.
# ---------------------------------------------------------------------------

def _llvm_path_with_hw(tmp_path, llvm_targets):
    """Build a PKGBUILD.sysforge + matching state_dir/hardware_profile.toml."""
    pkgbuild = tmp_path / "PKGBUILD.sysforge"
    pkgbuild.write_text(_PKGBUILD_NO_TARGETS)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_hardware(state_dir / "hardware_profile.toml", llvm_targets)
    return pkgbuild, state_dir


def test_maybe_patch_llvm_targets_injects_for_llvm(tmp_path):
    """Explicit state_dir_override → patcher reads hardware_profile.toml from
    the caller-supplied path (round-2 fix: caller pins the state dir, so the
    patcher and the writer can't disagree even when env vars differ)."""
    pkgbuild, state_dir = _llvm_path_with_hw(tmp_path, ["X86", "NVPTX"])
    pkgmeta = {"globals": {"pkgname": ["llvm", "llvm-libs"]}}
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH",
        tmp_path / "missing-toolchain.toml",
    ):
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX"' in pkgbuild.read_text()


def test_maybe_patch_llvm_targets_skips_non_llvm(tmp_path):
    pkgbuild, state_dir = _llvm_path_with_hw(tmp_path, ["X86", "NVPTX"])
    pkgmeta = {"globals": {"pkgname": "htop"}}
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH",
        tmp_path / "missing-toolchain.toml",
    ):
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert "LLVM_TARGETS_TO_BUILD" not in pkgbuild.read_text()


def test_maybe_patch_llvm_targets_falls_back_to_live_detect(tmp_path):
    """Round-3: LLVM pkgbase + missing hardware_profile.toml → live detection
    (uname/lspci) is used and the patch IS applied. This is the round-2 silent
    failure mode — the env-var-missing-shell case where there's simply no
    profile to read; the patcher must still work."""
    pkgbuild = tmp_path / "PKGBUILD.sysforge"
    pkgbuild.write_text(_PKGBUILD_NO_TARGETS)
    state_dir = tmp_path / "state"
    state_dir.mkdir()  # Empty — no hardware_profile.toml.
    pkgmeta = {"globals": {"pkgname": ["llvm", "llvm-libs"]}}
    fake_lspci_stdout = (
        "00:00.0 Host bridge: AMD Starship/Matisse Root Complex\n"
        "01:00.0 VGA compatible controller: NVIDIA Corporation Foo [GeForce RTX 5070]\n"
    )
    fake_lspci = SimpleNamespace(returncode=0, stdout=fake_lspci_stdout)
    fake_uname = SimpleNamespace(machine="x86_64")
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH",
        tmp_path / "missing-toolchain.toml",
    ), patch(
        "sysforge.primitives.llvm_targets.subprocess.run",
        return_value=fake_lspci,
    ), patch("os.uname", return_value=fake_uname):
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX"' in pkgbuild.read_text()


def test_maybe_patch_llvm_targets_force_all_short_circuits_live(tmp_path):
    """Round-3: explicit ``[llvm] targets = []`` (force-all override) must NOT
    fall through to live detection — the user said "build all targets," and
    live detection on this box would yield only the autodetected set, which
    is the opposite of what they asked for."""
    pkgbuild = tmp_path / "PKGBUILD.sysforge"
    pkgbuild.write_text(_PKGBUILD_NO_TARGETS)
    tc = tmp_path / "toolchain.toml"
    _write_toolchain(tc, "[llvm]\ntargets = []\n")
    pkgmeta = {"globals": {"pkgname": ["llvm", "llvm-libs"]}}
    state_dir = tmp_path / "state"
    state_dir.mkdir()  # No hardware_profile.toml; live would otherwise fire.
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH", tc,
    ), patch("sysforge.primitives.llvm_targets.subprocess.run") as mock_run:
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert "LLVM_TARGETS_TO_BUILD" not in pkgbuild.read_text()
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_or_detect_llvm_targets — composed file-based + live detection
# ---------------------------------------------------------------------------

def test_resolve_or_detect_prefers_hardware_profile(tmp_path):
    """When hardware_profile.toml has llvm_targets, live detection isn't
    invoked even though it would also succeed."""
    from sysforge.primitives.llvm_targets import resolve_or_detect_llvm_targets
    hw = tmp_path / "hardware_profile.toml"
    _write_hardware(hw, ["X86", "AMDGPU"])
    with patch("sysforge.primitives.llvm_targets.subprocess.run") as mock_run:
        result = resolve_or_detect_llvm_targets(tmp_path / "missing-tc.toml", hw)
    assert result == ["X86", "AMDGPU"]
    mock_run.assert_not_called()


def test_resolve_or_detect_falls_back_to_live(tmp_path):
    from sysforge.primitives.llvm_targets import resolve_or_detect_llvm_targets
    fake_lspci = SimpleNamespace(
        returncode=0,
        stdout="01:00.0 VGA compatible controller: NVIDIA Corporation Foo\n",
    )
    fake_uname = SimpleNamespace(machine="x86_64")
    with patch(
        "sysforge.primitives.llvm_targets.subprocess.run",
        return_value=fake_lspci,
    ), patch("os.uname", return_value=fake_uname):
        result = resolve_or_detect_llvm_targets(
            tmp_path / "missing-tc.toml", tmp_path / "missing-hw.toml",
        )
    assert result == ["X86", "NVPTX"]


def test_resolve_or_detect_lspci_failure_is_non_fatal(tmp_path):
    """lspci missing/erroring → CPU-only target list (still useful)."""
    from sysforge.primitives.llvm_targets import resolve_or_detect_llvm_targets
    fake_lspci = SimpleNamespace(returncode=1, stdout="")
    fake_uname = SimpleNamespace(machine="x86_64")
    with patch(
        "sysforge.primitives.llvm_targets.subprocess.run",
        return_value=fake_lspci,
    ), patch("os.uname", return_value=fake_uname):
        result = resolve_or_detect_llvm_targets(
            tmp_path / "missing-tc.toml", tmp_path / "missing-hw.toml",
        )
    assert result == ["X86"]
