"""
test_llvm_targets.py — resolution logic for LLVM_TARGETS_TO_BUILD and the
pkgbuild_patcher injection that consumes it.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


from sysforge.primitives.llvm_targets import resolve_llvm_targets
from sysforge.primitives.makepkg_wrapper import _maybe_patch_llvm_targets
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import (
    PkgbuildPatchError,
    _find_cmake_configure_anchor,
    is_llvm_pkgbase,
    patch_llvm_dir,
    patch_llvm_targets,
    validate_patched_pkgbuild,
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
    """toolchain.toml [llvm] targets wins over hardware autodetect — but the
    mandatory AMDGPU baseline is still appended (the system-mesa invariant
    overrides even an explicit list; only `targets = []` opts out)."""
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, '[llvm]\ntargets = ["X86", "AArch64"]\n')
    _write_hardware(hw, ["X86", "AMDGPU", "NVPTX"])
    result = resolve_llvm_targets(tc, hw)
    assert result == ["X86", "AArch64", "AMDGPU"]


def test_resolve_empty_override_disables_filtering(tmp_path):
    """[llvm] targets = [] means "force build all targets" → return None."""
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "[llvm]\ntargets = []\n")
    _write_hardware(hw, ["X86", "AMDGPU"])
    assert resolve_llvm_targets(tc, hw) is None


def test_resolve_section_absent_falls_through(tmp_path):
    """toolchain.toml without [llvm] section → fall through to hardware (with
    the AMDGPU baseline appended to the autodetected set)."""
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "enabled = false\n")
    _write_hardware(hw, ["X86"])
    assert resolve_llvm_targets(tc, hw) == ["X86", "AMDGPU"]


def test_resolve_malformed_toolchain_falls_through(tmp_path):
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, "this is not valid TOML [[\n")
    _write_hardware(hw, ["X86"])
    assert resolve_llvm_targets(tc, hw) == ["X86", "AMDGPU"]


def test_resolve_targets_not_a_list_is_ignored(tmp_path):
    tc = tmp_path / "toolchain.toml"
    hw = tmp_path / "hardware_profile.toml"
    _write_toolchain(tc, '[llvm]\ntargets = "X86"\n')
    _write_hardware(hw, ["X86", "AMDGPU"])
    assert resolve_llvm_targets(tc, hw) == ["X86", "AMDGPU"]


# ---------------------------------------------------------------------------
# System-consumer baseline enforcement at the RESOLUTION layer — a cached or
# hand-edited hardware_profile.toml that drops AMDGPU (bypassing
# derive_llvm_targets) must still resolve to a set that carries it. This is the
# layer the bricked-desktop bug slipped through: the fix lived in derivation,
# but the build resolves from the cached file.
# ---------------------------------------------------------------------------

def test_resolve_stale_profile_without_amdgpu_gets_it_appended(tmp_path):
    """The exact bug: a profile written before the baseline existed
    (`["X86", "NVPTX"]`) is re-augmented with AMDGPU at resolution time."""
    hw = tmp_path / "hardware_profile.toml"
    _write_hardware(hw, ["X86", "NVPTX"])
    assert resolve_llvm_targets(tmp_path / "missing-tc.toml", hw) == [
        "X86", "NVPTX", "AMDGPU",
    ]


def test_resolve_amdgpu_already_present_not_duplicated(tmp_path):
    hw = tmp_path / "hardware_profile.toml"
    _write_hardware(hw, ["X86", "AMDGPU", "NVPTX"])
    result = resolve_llvm_targets(tmp_path / "missing-tc.toml", hw)
    assert result is not None
    assert result == ["X86", "AMDGPU", "NVPTX"]
    assert result.count("AMDGPU") == 1


def test_resolve_explicit_targets_without_amdgpu_gets_it(tmp_path):
    """An explicit toolchain.toml list that omits AMDGPU is still augmented —
    the system-mesa invariant is not user-overridable except via `[] = all`."""
    tc = tmp_path / "toolchain.toml"
    _write_toolchain(tc, '[llvm]\ntargets = ["X86", "NVPTX"]\n')
    assert resolve_llvm_targets(tc, tmp_path / "missing-hw.toml") == [
        "X86", "NVPTX", "AMDGPU",
    ]


def test_resolve_empty_override_stays_none_no_baseline(tmp_path):
    """`targets = []` (build all) must NOT be turned into ["AMDGPU"] — it stays
    None (no filtering), which already builds every target including AMDGPU."""
    tc = tmp_path / "toolchain.toml"
    _write_toolchain(tc, "[llvm]\ntargets = []\n")
    assert resolve_llvm_targets(tc, tmp_path / "missing-hw.toml") is None


# ---------------------------------------------------------------------------
# derive_llvm_targets — autodetected set, incl. the mandatory AMDGPU baseline
#
# AMDGPU must appear on EVERY recognised-arch host (even nvidia/intel-only),
# because Arch's mesa links the AMDGPU + host-CPU target-init symbols from
# libgallium unconditionally. Dropping AMDGPU from a rebuilt system libLLVM
# bricks the whole EGL/GL desktop — the regression this guards against.
# ---------------------------------------------------------------------------

def test_derive_nvidia_host_appends_amdgpu():
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    assert derive_llvm_targets("x86_64", ["nvidia"]) == ["X86", "NVPTX", "AMDGPU"]


def test_derive_amd_host_keeps_amdgpu_once():
    """AMD GPU already pulls in AMDGPU via the vendor map; the baseline must
    not duplicate it."""
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    result = derive_llvm_targets("x86_64", ["amd"])
    assert result == ["X86", "AMDGPU"]
    assert result.count("AMDGPU") == 1


def test_derive_intel_only_host_still_gets_amdgpu():
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    assert derive_llvm_targets("x86_64", ["intel"]) == ["X86", "AMDGPU"]


def test_derive_no_gpu_host_still_gets_amdgpu():
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    assert derive_llvm_targets("x86_64", []) == ["X86", "AMDGPU"]


def test_derive_multi_gpu_amdgpu_appears_once():
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    result = derive_llvm_targets("x86_64", ["amd", "nvidia"])
    assert result == ["X86", "AMDGPU", "NVPTX"]
    assert result.count("AMDGPU") == 1


def test_derive_aarch64_host_gets_amdgpu():
    """Baseline applies to every recognised arch, not just x86_64."""
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    assert derive_llvm_targets("aarch64", ["nvidia"]) == ["AArch64", "NVPTX", "AMDGPU"]


def test_derive_unrecognised_arch_returns_empty_no_filtering():
    """Unknown arch → [] (no filtering = upstream builds all targets), which
    is also safe for mesa since AMDGPU is then built anyway."""
    from sysforge.pipeline.stages.hardware import derive_llvm_targets
    assert derive_llvm_targets("s390x", ["amd"]) == []


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
# patch_llvm_dir — force find_package(LLVM) at a staged libLLVM prefix
# (toolchain PGO passes 1b/3b/3c). Mirrors patch_llvm_targets.
# ---------------------------------------------------------------------------

_STAGED_LLVM_DIR = "/var/tmp/sysforge-llvm-stage3/usr/lib/cmake/llvm"


def test_patch_llvm_dir_injects_after_cmake(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)
    changed = patch_llvm_dir(p, _STAGED_LLVM_DIR)
    assert changed is True
    new = p.read_text()
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in new
    # Existing cmake args preserved.
    assert "-DCMAKE_BUILD_TYPE=Release" in new


def test_patch_llvm_dir_idempotent(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)
    patch_llvm_dir(p, _STAGED_LLVM_DIR)
    snapshot = p.read_text()
    second = patch_llvm_dir(p, _STAGED_LLVM_DIR)
    assert second is False
    assert p.read_text() == snapshot


def test_patch_llvm_dir_replaces_when_present(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(
        'pkgname=clang\nbuild() {\n'
        '  cmake .. -DLLVM_DIR="/usr/lib/cmake/llvm"\n}\n'
    )
    changed = patch_llvm_dir(p, _STAGED_LLVM_DIR)
    assert changed is True
    new = p.read_text()
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in new
    assert '/usr/lib/cmake/llvm"' not in new.replace(_STAGED_LLVM_DIR, "")


def test_patch_llvm_dir_no_cmake_skips(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_CMAKE)
    changed = patch_llvm_dir(p, _STAGED_LLVM_DIR)
    assert changed is False
    assert p.read_text() == _PKGBUILD_NO_CMAKE


def test_patch_llvm_dir_empty_is_noop(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)
    changed = patch_llvm_dir(p, "")
    assert changed is False
    assert p.read_text() == _PKGBUILD_NO_TARGETS


def test_patch_llvm_dir_not_confused_by_distribution_components(tmp_path):
    """-DLLVM_DISTRIBUTION_COMPONENTS=… must not be mistaken for LLVM_DIR."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(
        'pkgname=clang\nbuild() {\n'
        '  cmake .. -DLLVM_DISTRIBUTION_COMPONENTS="clang;clang-resource-headers"\n}\n'
    )
    changed = patch_llvm_dir(p, _STAGED_LLVM_DIR)
    assert changed is True
    new = p.read_text()
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in new
    # The distribution-components arg is untouched.
    assert '-DLLVM_DISTRIBUTION_COMPONENTS="clang;clang-resource-headers"' in new


# ---------------------------------------------------------------------------
# Composition: targets + dir injected on the same cmake invocation must stay
# on one logical command (both injectors run on the toolchain non-pgo passes).
# ---------------------------------------------------------------------------

def _logical_lines(text: str) -> list[str]:
    """Join bash line-continuations (`\\` + newline) into single logical lines."""
    return text.replace("\\\n", " ").splitlines()


def _assert_no_orphaned_cmake_arg(text: str):
    """Every `-D` arg must ride a `cmake` command, not stand alone (which bash
    would run as a command -> 'command not found', exit 4)."""
    logical = _logical_lines(text)
    orphans = [ln for ln in logical if ln.strip().startswith("-D")]
    assert not orphans, f"orphaned -D arg(s) not attached to cmake: {orphans!r}"


def test_patch_targets_then_dir_compose_single_line_cmake(tmp_path):
    """Regression: applying patch_llvm_targets then patch_llvm_dir to the Arch
    single-line `cmake .. "${cmake_args[@]}"` shape must keep BOTH -D args on the
    one cmake command. The buggy version spliced the 2nd injection into the 1st's
    continuation, orphaning -DLLVM_TARGETS_TO_BUILD (PKGBUILD line: command not
    found, build() exit 4)."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text('pkgname=clang\nbuild() {\n  cmake .. "${cmake_args[@]}"\n  ninja\n}\n')
    # Production order: targets first (via _maybe_patch_llvm_targets), then dir.
    assert patch_llvm_targets(p, ["X86", "NVPTX"]) is True
    assert patch_llvm_dir(p, _STAGED_LLVM_DIR) is True
    text = p.read_text()
    _assert_no_orphaned_cmake_arg(text)
    cmake_logical = [ln for ln in _logical_lines(text) if ln.lstrip().startswith("cmake ")]
    assert len(cmake_logical) == 1
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX"' in cmake_logical[0]
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in cmake_logical[0]


def test_patch_dir_then_targets_compose_order_independent(tmp_path):
    """Composition must hold regardless of injection order."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text('pkgname=clang\nbuild() {\n  cmake .. "${cmake_args[@]}"\n  ninja\n}\n')
    assert patch_llvm_dir(p, _STAGED_LLVM_DIR) is True
    assert patch_llvm_targets(p, ["X86", "NVPTX"]) is True
    text = p.read_text()
    _assert_no_orphaned_cmake_arg(text)
    cmake_logical = [ln for ln in _logical_lines(text) if ln.lstrip().startswith("cmake ")]
    assert len(cmake_logical) == 1
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX"' in cmake_logical[0]
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in cmake_logical[0]


def test_patch_compose_on_multiline_cmake(tmp_path):
    """A `\\`-continued multi-line cmake invocation: both injections append after
    the statement's true end, never splitting an existing continuation."""
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD_NO_TARGETS)   # multi-line `cmake -B build -S . \ ...`
    assert patch_llvm_targets(p, ["X86", "NVPTX"]) is True
    assert patch_llvm_dir(p, _STAGED_LLVM_DIR) is True
    text = p.read_text()
    _assert_no_orphaned_cmake_arg(text)
    cmake_logical = [ln for ln in _logical_lines(text) if ln.lstrip().startswith("cmake ")]
    assert len(cmake_logical) == 1
    # Upstream args preserved alongside both injected ones.
    assert "-DCMAKE_BUILD_TYPE=Release" in cmake_logical[0]
    assert "-DLLVM_HOST_TRIPLE=x86_64-pc-linux-gnu" in cmake_logical[0]
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX"' in cmake_logical[0]
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in cmake_logical[0]


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
    patcher and the writer can't disagree even when env vars differ).

    Regression for the bricked-desktop bug: a cached hardware_profile.toml that
    predates the AMDGPU baseline (`["X86", "NVPTX"]`, an nvidia host) must STILL
    inject AMDGPU — the resolution layer (resolve_or_detect_llvm_targets) re-adds
    it even though the cached file bypasses derive_llvm_targets. Without this the
    rebuilt system libLLVM drops AMDGPU and mesa black-screens the desktop."""
    pkgbuild, state_dir = _llvm_path_with_hw(tmp_path, ["X86", "NVPTX"])
    pkgmeta = {"globals": {"pkgname": ["llvm", "llvm-libs"]}}
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH",
        tmp_path / "missing-toolchain.toml",
    ):
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX;AMDGPU"' in pkgbuild.read_text()


def test_maybe_patch_llvm_targets_skips_non_llvm(tmp_path):
    pkgbuild, state_dir = _llvm_path_with_hw(tmp_path, ["X86", "NVPTX"])
    pkgmeta = {"globals": {"pkgname": "htop"}}
    with patch(
        "sysforge.primitives.makepkg_wrapper.TOOLCHAIN_PATH",
        tmp_path / "missing-toolchain.toml",
    ):
        _maybe_patch_llvm_targets(pkgbuild, pkgmeta, state_dir_override=state_dir)
    assert "LLVM_TARGETS_TO_BUILD" not in pkgbuild.read_text()


def test_maybe_patch_llvm_targets_skips_lib32(tmp_path):
    """lib32-* LLVM packages must NOT have their target set reduced — they share
    the all-target 64-bit headers, so a reduced LLVM_TARGETS_TO_BUILD breaks
    lib32-clang's offload-tool links. The patcher leaves them untouched."""
    pkgbuild, state_dir = _llvm_path_with_hw(tmp_path, ["X86", "NVPTX"])
    pkgmeta = {"globals": {"pkgname": ["lib32-llvm", "lib32-llvm-libs"]}}
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
    # AMDGPU is always appended (system mesa links it regardless of GPU).
    assert '-DLLVM_TARGETS_TO_BUILD="X86;NVPTX;AMDGPU"' in pkgbuild.read_text()


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
    # AMDGPU is always appended — system mesa links it regardless of GPU.
    assert result == ["X86", "NVPTX", "AMDGPU"]


def test_resolve_or_detect_lspci_failure_is_non_fatal(tmp_path):
    """lspci missing/erroring → CPU target + the AMDGPU baseline (still useful)."""
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
    # No GPU detected, but AMDGPU is still mandatory for system mesa.
    assert result == ["X86", "AMDGPU"]


# ---------------------------------------------------------------------------
# _find_cmake_configure_anchor — structurally-correct cmake-command selection
# ---------------------------------------------------------------------------

def test_anchor_skips_bare_dep_array_cmake():
    """A bare `cmake` element in a multi-line `makedepends=(...)` array must NOT
    be chosen as the injection anchor — this is the spirv-llvm-translator
    exit-12 brick (a `-D…` arg spliced into the dependency array)."""
    text = (
        "makedepends=(\n  cmake\n  ninja\n)\n"
        "build() {\n  cmake -S . -B build -G Ninja\n}\n"
    )
    m = _find_cmake_configure_anchor(text)
    assert m is not None
    # The chosen anchor is the real command (it has same-line args), never the
    # bare array element.
    assert "-S . -B build" in m.group("rest")


def test_anchor_skips_action_mode_cmake():
    """`cmake --build` / `cmake --install` ignore -D cache args; the anchor must
    fall through to the configure invocation even when an action-mode call comes
    first in source order."""
    text = (
        "build() {\n"
        "  cmake --build build\n"      # action-mode, first — must be skipped
        "  cmake -S . -B build\n"      # the configure call we want
        "}\n"
    )
    m = _find_cmake_configure_anchor(text)
    assert m is not None
    assert m.group("rest").startswith("-S")


def test_anchor_none_when_no_cmake_command():
    """No cmake *command* (only a build system swap) → no anchor."""
    assert _find_cmake_configure_anchor("build() {\n  meson setup build\n}\n") is None


# ---------------------------------------------------------------------------
# validate_patched_pkgbuild — fast, build-free post-patch structural gate.
# G1: dependency/identity arrays unchanged. G2: managed -D args ride a cmake cmd.
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "data" / "PKGBUILDs"


def test_validate_real_spirv_fixture_round_trip(tmp_path):
    """End-to-end on the real regression shape: injecting -DLLVM_DIR into the
    spirv fixture must leave makedepends intact (anchor lands on the real cmake
    command), and validation passes."""
    original = _FIXTURES / "spirv-llvm-translator.PKGBUILD"
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(original.read_text())
    assert patch_llvm_dir(patched, _STAGED_LLVM_DIR) is True
    validate_patched_pkgbuild(original, patched)  # must not raise
    meta = parse_pkgbuild(patched)["globals"]
    assert meta["makedepends"] == ["cmake", "git", "llvm", "ninja", "spirv-headers"]
    assert f'-DLLVM_DIR="{_STAGED_LLVM_DIR}"' in patched.read_text()


def test_validate_g1_rejects_dep_array_corruption(tmp_path):
    """G1: a -D arg that landed inside makedepends=() (the pre-fix spirv brick)
    changes the parsed dependency array → PkgbuildPatchError."""
    original = tmp_path / "PKGBUILD"
    original.write_text(
        "pkgname=foo\nmakedepends=(\n  cmake\n  ninja\n)\n"
        "build() {\n  cmake -S . -B build\n}\n"
    )
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(
        'pkgname=foo\nmakedepends=(\n  cmake \\\n    -DLLVM_DIR="/x"\n  ninja\n)\n'
        "build() {\n  cmake -S . -B build\n}\n"
    )
    with pytest.raises(PkgbuildPatchError, match="makedepends"):
        validate_patched_pkgbuild(original, patched)


def test_validate_g2_rejects_orphaned_arg(tmp_path):
    """G2: a -D arg orphaned as its own command (the clang composition exit-4
    brick) is caught even though the dependency arrays are untouched."""
    original = tmp_path / "PKGBUILD"
    original.write_text('pkgname=clang\nbuild() {\n  cmake .. "${cmake_args[@]}"\n}\n')
    patched = tmp_path / "PKGBUILD.sysforge"
    # `\ \` is an escaped backslash → the statement ends and -DLLVM_TARGETS_TO_BUILD
    # becomes a standalone command line.
    patched.write_text(
        'pkgname=clang\nbuild() {\n'
        '  cmake .. "${cmake_args[@]}" \\ \\\n'
        '      -DLLVM_DIR="/x"\n'
        '      -DLLVM_TARGETS_TO_BUILD="X86"\n'
        '}\n'
    )
    with pytest.raises(PkgbuildPatchError, match="not attached to a cmake"):
        validate_patched_pkgbuild(original, patched)


def test_validate_passes_on_clean_injection(tmp_path):
    """Both real injectors on the Arch single-line cmake shape produce a file
    that passes both gates."""
    original = tmp_path / "PKGBUILD"
    original.write_text(
        "pkgname=clang\nmakedepends=('llvm' 'cmake')\n"
        'build() {\n  cmake .. "${cmake_args[@]}"\n}\n'
    )
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(original.read_text())
    assert patch_llvm_targets(patched, ["X86", "NVPTX"]) is True
    assert patch_llvm_dir(patched, _STAGED_LLVM_DIR) is True
    validate_patched_pkgbuild(original, patched)  # must not raise


def test_validate_allows_legitimate_dash_d_array_elements(tmp_path):
    """G2 must not false-positive on legitimate `-D …` *array elements*: the
    spirv fixture's `cmake_options=(-D … )` block carries many of them. Only the
    two managed injected tokens are checked, so the array elements are ignored."""
    original = _FIXTURES / "spirv-llvm-translator.PKGBUILD"
    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text(original.read_text())
    assert patch_llvm_dir(patched, _STAGED_LLVM_DIR) is True
    validate_patched_pkgbuild(original, patched)  # must not raise
