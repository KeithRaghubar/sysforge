#!/usr/bin/env python3
"""
Unit tests for sysforge.primitives.build_diag.

Covers:
  - rust E0463 (missing std crate) → exact rustup target add suggestion
  - gstreamer PTP-no-rust → falls back to lib32 i686 target suggestion when
    E0463 is absent; suppressed when E0463 is also present (no dup noise)
  - meson "Unknown options" → stale build/ directory suggestion
  - meson pkg-config version gate → module/floor/found parsed, owning package
    resolved, repo-satisfiability verdict, AUR -git presence, and never a fix_cmd
  - clean logs → no suggestions, no false positives
  - render_suggestions: shape + empty case
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.primitives.build_diag import (
    FixSuggestion,
    diagnose,
    render_suggestions,
)


_E0463_BLOCK = (
    "error[E0463]: can't find crate for `std`\n"
    "  |\n"
    "  = note: the `i686-unknown-linux-gnu` target may not be installed\n"
    "  = help: consider downloading the target with "
    "`rustup target add i686-unknown-linux-gnu`\n"
    "\n"
    "error: aborting due to 1 previous error\n"
)


def _lines(text: str) -> list[str]:
    return text.splitlines()


# ---------------------------------------------------------------------------
# Signature matchers
# ---------------------------------------------------------------------------

def test_rust_e0463_with_target_and_active_toolchain():
    out = diagnose(_lines(_E0463_BLOCK), None, active_rust_toolchain="stable")
    assert len(out) == 1
    s = out[0]
    assert s.signature == "rust:E0463"
    assert s.fix_cmd == "rustup target add --toolchain stable i686-unknown-linux-gnu"


def test_rust_e0463_without_active_toolchain():
    out = diagnose(_lines(_E0463_BLOCK), None, active_rust_toolchain=None)
    assert out[0].fix_cmd == "rustup target add i686-unknown-linux-gnu"


def test_gst_ptp_alone_suggests_lib32_cross():
    """PTP error without E0463 — gstreamer-specific cross-target hint."""
    log = "ERROR: Problem encountered: PTP not supported without Rust compiler\n"
    out = diagnose(_lines(log), None, active_rust_toolchain="stable")
    assert len(out) == 1
    assert out[0].signature == "gst:ptp-no-rust"
    assert "i686-unknown-linux-gnu" in (out[0].fix_cmd or "")


def test_gst_ptp_with_e0463_suppresses_duplicate():
    """When both signatures match, only the E0463 suggestion is emitted —
    the PTP hint is redundant with the rust-missing-std fix."""
    log = (
        _E0463_BLOCK
        + "\nERROR: Problem encountered: PTP not supported without Rust compiler\n"
    )
    out = diagnose(_lines(log), None, active_rust_toolchain="stable")
    assert [s.signature for s in out] == ["rust:E0463"]


def test_meson_unknown_options():
    log = "meson.build:42:4: ERROR: Unknown options: \"gst-plugins-base:tremor\"\n"
    out = diagnose(_lines(log), None)
    assert len(out) == 1
    assert out[0].signature == "meson:unknown-options"
    assert "build" in (out[0].fix_cmd or "")


def test_clean_log_no_suggestions():
    log = (
        "Configuring done\n"
        "Generating done\n"
        "[100%] Built target foo\n"
        "==> Finished making: foo\n"
    )
    assert diagnose(_lines(log), None) == []


def test_empty_input_returns_empty():
    assert diagnose([], None) == []


# ---------------------------------------------------------------------------
# CUDA host-gcc-too-new
# ---------------------------------------------------------------------------

import sysforge.primitives.build_diag as _bd  # noqa: E402

# nvcc's frontend choking on a too-new libstdc++ (the gpu-burn-git failure):
# a .cu compile summary + libstdc++ header errors.
_CUDA_CU_FAIL = (
    "/usr/include/c++/16.1.1/type_traits(3313): error: expected a declaration\n"
    "Error limit reached.\n"
    '100 errors detected in the compilation of "compare.cu".\n'
)


def test_cuda_host_gcc_detects_and_suggests_ccbin(monkeypatch):
    monkeypatch.setattr(_bd, "_cuda_max_gcc_major", lambda: 15)
    monkeypatch.setattr(_bd, "_highest_gpp_upto", lambda n: "/usr/bin/g++-15")
    out = diagnose(_lines(_CUDA_CU_FAIL), None)
    assert [s.signature for s in out] == ["cuda:host-gcc-too-new"]
    s = out[0]
    assert "gcc <= 15" in s.message
    assert "system gcc is 16" in s.message
    assert "-ccbin /usr/bin/g++-15" in (s.fix_cmd or "")


def test_cuda_host_gcc_canonical_unsupported_message(monkeypatch):
    """The clean host_config.h gate message matches even without a .cu line."""
    monkeypatch.setattr(_bd, "_cuda_max_gcc_major", lambda: None)
    monkeypatch.setattr(_bd, "_highest_gpp_upto", lambda n: None)
    log = (
        "#error -- unsupported GNU version! gcc versions later than 15 "
        "are not supported!\n"
    )
    out = diagnose(_lines(log), None)
    assert [s.signature for s in out] == ["cuda:host-gcc-too-new"]


def test_cuda_host_gcc_no_compatible_gpp_installed(monkeypatch):
    monkeypatch.setattr(_bd, "_cuda_max_gcc_major", lambda: 15)
    monkeypatch.setattr(_bd, "_highest_gpp_upto", lambda n: None)
    out = diagnose(_lines(_CUDA_CU_FAIL), None)
    assert (out[0].fix_cmd or "").startswith("install gcc15")


def test_cuda_matcher_no_false_positive_on_flto_thin():
    """The -flto=thin rejection (the other half of the gpu-burn failure) is a
    flag mismatch, NOT a CUDA host-compiler mismatch — must not match here."""
    log = "cc1plus: error: unrecognized argument to '-flto=' option: 'thin'\n"
    out = diagnose(_lines(log), None)
    assert all(s.signature != "cuda:host-gcc-too-new" for s in out)


# ---------------------------------------------------------------------------
# lib32 reduced-target libLLVM link failure
# ---------------------------------------------------------------------------

# The real lib32-clang failure: clang-nvlink-wrapper links against a reduced
# /usr/lib32/libLLVM.so (X86;NVPTX) but references all-target init symbols from
# the shared all-target 64-bit headers.
_LIB32_LLVM_LINK_FAIL = (
    "[1531/1567] Linking CXX executable bin/clang-nvlink-wrapper\n"
    "FAILED: bin/clang-nvlink-wrapper\n"
    ": && /usr/bin/clang++ -O3 -m32 ... -o bin/clang-nvlink-wrapper "
    "lib32/libclangBasic.a /usr/lib32/libLLVM.so.22.1 && :\n"
    "ld.lld: error: undefined symbol: LLVMInitializeAArch64AsmParser\n"
    ">>> referenced by ClangNVLinkWrapper.cpp\n"
    "ld.lld: error: undefined symbol: LLVMInitializeBPFAsmParser\n"
    "clang++: error: linker command failed with exit code 1\n"
)


def test_lib32_reduced_target_llvm_matches():
    out = diagnose(_lines(_LIB32_LLVM_LINK_FAIL), None)
    assert [s.signature for s in out] == ["toolchain:lib32-reduced-target"]
    # The misleading generic "version skew / pacman -Syu" suggestion is suppressed.
    assert all(s.signature != "toolchain:llvm-broken" for s in out)
    assert "lib32" in out[0].message


def test_runtime_llvm_skew_still_generic():
    """A runtime symbol-lookup failure (clang can't start) — NOT a lib32 link
    failure — must still get the generic broken-toolchain suggestion."""
    log = (
        "clang: symbol lookup error: /usr/bin/clang: undefined symbol: "
        "LLVMInitializeX86TargetInfo\n"
    )
    out = diagnose(_lines(log), None)
    assert [s.signature for s in out] == ["toolchain:llvm-broken"]


# ---------------------------------------------------------------------------
# lib32 clang + lld: 32-bit libgcc_s runtime not found (2.1.0-B10)
# ---------------------------------------------------------------------------

# The real lib32-vulkan-icd-loader failure under the clang/lld profile: CMake's
# compiler-sanity link (clang -m32 -fuse-ld=lld) implicitly pulls -lgcc_s, but
# ld.lld can't locate the 32-bit libgcc_s the gcc driver would find — CMake then
# reports the compiler as "broken / not able to compile a simple test program",
# which reads as though clang itself is invalid.
_LIB32_LIBGCC_S_FAIL = (
    "-- Check for working C compiler: /usr/lib/ccache/bin/clang - broken\n"
    "  is not able to compile a simple test program.\n"
    "    [2/2] : && /usr/lib/ccache/bin/clang -m32 -fuse-ld=lld "
    "CMakeFiles/cmTC.dir/testCCompiler.c.o -o cmTC && :\n"
    "    ld.lld: error: unable to find library -lgcc_s\n"
    "    clang: error: linker command failed with exit code 1\n"
)


def test_lib32_clang_libgcc_s_matches():
    out = diagnose(_lines(_LIB32_LIBGCC_S_FAIL), None)
    assert [s.signature for s in out] == ["toolchain:lib32-clang-libgcc"]
    # Names the real cause (lib32/32-bit libgcc_s), not "compiler invalid".
    assert "libgcc_s" in out[0].message
    # Auto-suggests the gcc per-package override the user confirmed works.
    assert out[0].fix_cmd is not None
    assert "gcc" in out[0].fix_cmd and "g++" in out[0].fix_cmd


def test_lib32_clang_libgcc_s_gold_linker_variant():
    # bfd/gold phrase the same miss as "cannot find -lgcc_s"; still lib32-gated.
    log = (
        "clang -m32 conftest.o -o conftest\n"
        "/usr/bin/ld: cannot find -lgcc_s: No such file or directory\n"
    )
    out = diagnose(_lines(log), None)
    assert [s.signature for s in out] == ["toolchain:lib32-clang-libgcc"]


def test_libgcc_s_miss_without_m32_no_match():
    # A 64-bit build missing -lgcc_s is a different (non-lib32) problem — the
    # lib32-gated matcher must not claim it.
    log = (
        "clang conftest.o -o conftest\n"
        "ld.lld: error: unable to find library -lgcc_s\n"
    )
    out = diagnose(_lines(log), None)
    assert all(s.signature != "toolchain:lib32-clang-libgcc" for s in out)


# ---------------------------------------------------------------------------
# Side-car log discovery
# ---------------------------------------------------------------------------

def test_meson_log_discovery(tmp_path):
    """When captured_lines are silent but a meson-log.txt under build_dir
    contains the signature, diagnose still finds it."""
    # Replicates the lib32-gstreamer layout: <srcdir>/<subdir>/meson-logs/...
    meson_dir = tmp_path / "src" / "build" / "meson-logs"
    meson_dir.mkdir(parents=True)
    (meson_dir / "meson-log.txt").write_text(_E0463_BLOCK)

    out = diagnose([], tmp_path / "src", active_rust_toolchain="stable")
    assert len(out) == 1
    assert out[0].signature == "rust:E0463"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def test_render_empty():
    assert render_suggestions([]) == ""


def test_render_with_fix_cmd():
    s = FixSuggestion(
        signature="rust:E0463",
        message="missing std for i686-unknown-linux-gnu",
        fix_cmd="rustup target add --toolchain stable i686-unknown-linux-gnu",
    )
    out = render_suggestions([s])
    assert "possible fixes:" in out
    assert "missing std for i686-unknown-linux-gnu" in out
    assert "$ rustup target add" in out


def test_render_without_fix_cmd():
    s = FixSuggestion(
        signature="x", message="something broke", fix_cmd=None,
    )
    out = render_suggestions([s])
    assert "something broke" in out
    assert "$ " not in out


# ---------------------------------------------------------------------------
# Broken LLVM toolchain (clang cannot run)
# ---------------------------------------------------------------------------

_BROKEN_CLANG_MESON = (
    "Detecting compiler via: `clang --version` -> 127\n"
    "/usr/bin/clang: symbol lookup error: /usr/bin/clang: undefined symbol: "
    "LLVMInitializeBPFTarget, version LLVM_22.1\n"
    "wayland-protocols/meson.build:1:0: ERROR: Unknown compiler(s): [['clang']]\n"
)


def test_toolchain_broken_symbol_error():
    out = diagnose(_lines(_BROKEN_CLANG_MESON), None)
    assert [s.signature for s in out] == ["toolchain:llvm-broken"]
    assert "pacman -Syu" in (out[0].fix_cmd or "")


def test_toolchain_broken_undefined_symbol_only():
    log = "ld: undefined symbol: LLVMInitializeX86TargetInfo\n"
    out = diagnose(_lines(log), None)
    assert any(s.signature == "toolchain:llvm-broken" for s in out)


def test_toolchain_broken_no_false_positive():
    log = "Configuring done\n[100%] Built target foo\n"
    assert all(s.signature != "toolchain:llvm-broken" for s in diagnose(_lines(log), None))


def test_cmake_error_log_sidecar_discovery(tmp_path):
    """The broken-clang error in CMakeError.log is picked up even when stdout
    was not captured (interactive mode)."""
    cmake_dir = tmp_path / "src" / "build" / "CMakeFiles"
    cmake_dir.mkdir(parents=True)
    (cmake_dir / "CMakeError.log").write_text(_BROKEN_CLANG_MESON)
    out = diagnose([], tmp_path / "src")
    assert [s.signature for s in out] == ["toolchain:llvm-broken"]


# ---------------------------------------------------------------------------
# meson/pkg-config version gate (2.6.1-F12)
# ---------------------------------------------------------------------------

_PC_GATE_LOG = (
    "Running compile:\n"
    "Dependency lookup for wayland-client with method 'pkg-config' failed: "
    "Invalid version, need 'wayland-client' ['>= 1.26.0'] found '1.25.0'\n"
    "meson.build:41:0: ERROR: Dependency 'wayland-client' is required but not found.\n"
)


def _stub_gate(monkeypatch, *, owner, repo_ver, aur_git):
    """Pin the three environment probes the pkg-config matcher makes."""
    monkeypatch.setattr(_bd, "_pc_owner", lambda module: owner)
    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_pacman_sync_version", lambda name: repo_ver
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.aur_info",
        lambda names: {names[0]: {}} if aur_git else {},
    )


def test_pkgconfig_gate_repo_below_floor_with_aur_git(monkeypatch):
    _stub_gate(monkeypatch, owner="wayland", repo_ver="1.25.0-1", aur_git=True)
    out = diagnose(_lines(_PC_GATE_LOG), None)
    assert [s.signature for s in out] == ["pkgconfig:version-gate"]
    s = out[0]
    # No fix_cmd: the two routes have materially different costs.
    assert s.fix_cmd is None
    assert "needs >= 1.26.0, found 1.25.0" in s.message
    assert "owned by wayland" in s.message
    assert "still below the floor" in s.message
    assert "wayland-git exists" in s.message


def test_pkgconfig_gate_repo_satisfies_floor(monkeypatch):
    """The repos already caught up — the operator just needs to sync."""
    _stub_gate(monkeypatch, owner="wayland", repo_ver="1.26.0-1", aur_git=False)
    s = diagnose(_lines(_PC_GATE_LOG), None)[0]
    assert "satisfies the floor" in s.message
    assert "no AUR wayland-git variant exists" in s.message
    assert s.fix_cmd is None


def test_pkgconfig_gate_owner_unresolved(monkeypatch):
    """No .pc on disk (or a failed -Qo) still yields the parsed diagnosis."""
    _stub_gate(monkeypatch, owner=None, repo_ver=None, aur_git=False)
    s = diagnose(_lines(_PC_GATE_LOG), None)[0]
    assert "could not resolve which package owns wayland-client.pc" in s.message
    assert s.fix_cmd is None


def test_pkgconfig_gate_probe_failure_does_not_mask_diagnosis(monkeypatch):
    def _boom(module):
        raise RuntimeError("pacman db locked")

    monkeypatch.setattr(_bd, "_pc_owner", _boom)
    s = diagnose(_lines(_PC_GATE_LOG), None)[0]
    assert s.signature == "pkgconfig:version-gate"
    assert "needs >= 1.26.0, found 1.25.0" in s.message


def test_pkgconfig_gate_no_false_positive():
    log = "Dependency wayland-client found: YES 1.26.0\n"
    assert diagnose(_lines(log), None) == []


def test_pc_owner_resolves_via_pacman(monkeypatch):
    """_pc_owner asks pacman -Qo only for .pc paths that actually exist."""
    seen = {}

    monkeypatch.setattr(Path, "exists", lambda self: "lib/pkgconfig" in str(self))

    def _owners(paths):
        seen["paths"] = [str(p) for p in paths]
        return {paths[0]: "wayland"}

    monkeypatch.setattr("sysforge.primitives.pacman.owners_of", _owners)
    assert _bd._pc_owner("wayland-client") == "wayland"
    assert seen["paths"] == ["/usr/lib/pkgconfig/wayland-client.pc"]
