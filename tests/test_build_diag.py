#!/usr/bin/env python3
"""
Unit tests for sysforge.primitives.build_diag.

Covers:
  - rust E0463 (missing std crate) → exact rustup target add suggestion
  - gstreamer PTP-no-rust → falls back to lib32 i686 target suggestion when
    E0463 is absent; suppressed when E0463 is also present (no dup noise)
  - meson "Unknown options" → stale build/ directory suggestion
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
