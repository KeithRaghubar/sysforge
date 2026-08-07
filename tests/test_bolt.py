# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/bolt.py — BOLT post-link optimization orchestration."""
import subprocess
from pathlib import Path

import pytest

from sysforge.primitives import bolt


# ---------------------------------------------------------------------------
# Store + flags + build-mode
# ---------------------------------------------------------------------------

def test_store_is_bolt_method_subdir():
    store = bolt.resolve_store({})
    assert store.name == "bolt"
    assert store.parent.name == "sysforge"


def test_store_honours_profile_store_override(tmp_path):
    assert bolt.resolve_store({"profile_store": str(tmp_path)}) == tmp_path / "bolt"


def test_fdata_and_perf_paths_under_store(tmp_path):
    assert bolt.fdata_path(tmp_path).name == bolt.FDATA_NAME
    assert bolt.perf_data_path(tmp_path).name == bolt.PERF_DATA_NAME


def test_emit_relocs_ldflag():
    assert bolt.emit_relocs_ldflag() == "-Wl,--emit-relocs"


def test_build_mode_is_optimized_and_conflict():
    from sysforge.primitives.profile import (
        is_optimized_build_mode,
        rename_mode_for_build_mode,
    )

    assert bolt.BUILD_MODE == "bolt_llvm"
    assert is_optimized_build_mode(bolt.BUILD_MODE)
    # BOLT of the toolchain is a mutually-exclusive drop-in (like the other
    # non-kernel optimizations), not a coexist install.
    assert rename_mode_for_build_mode(bolt.BUILD_MODE) == "conflict"


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def test_perf_record_cmd_has_branch_stack(tmp_path):
    cmd = bolt.perf_record_cmd(tmp_path / "perf.data", ["clang++", "-c", "x.cpp"])
    assert cmd[0] == "perf" and "record" in cmd
    assert "-j" in cmd and "any,u" in cmd  # branch stack (LBR/BRS)
    assert cmd[-3:] == ["clang++", "-c", "x.cpp"]


def test_perf2bolt_cmd_targets_binary(tmp_path):
    cmd = bolt.perf2bolt_cmd(Path("/usr/bin/clang"), tmp_path / "perf.data", tmp_path / "o.fdata")
    assert cmd[0] == "perf2bolt" and "/usr/bin/clang" in cmd
    assert "-p" in cmd and "-o" in cmd


def test_llvm_bolt_cmd_carries_opt_flags(tmp_path):
    cmd = bolt.llvm_bolt_cmd(Path("/usr/bin/clang"), tmp_path / "o.fdata", tmp_path / "clang.bolt")
    assert cmd[0] == "llvm-bolt"
    assert "-reorder-blocks=ext-tsp" in cmd
    assert "-reorder-functions=hfsort+" in cmd
    assert "-split-functions" in cmd


def test_compile_workload_argv():
    argv = bolt.compile_workload_argv("/usr/bin/clang++", Path("/t/w.cpp"), Path("/t/w.o"))
    assert argv[0] == "/usr/bin/clang++"
    assert "-O2" in argv and "-c" in argv


def test_write_default_workload(tmp_path):
    src = bolt.write_default_workload(tmp_path)
    assert src.is_file() and src.suffix == ".cpp"
    text = src.read_text()
    assert "#include" in text and "int main()" in text


# ---------------------------------------------------------------------------
# tools_available
# ---------------------------------------------------------------------------

def test_tools_available_all_present(monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: f"/usr/bin/{t}")
    ok, missing = bolt.tools_available()
    assert ok and missing == []


def test_tools_available_reports_missing(monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which",
                        lambda t: None if t == "perf2bolt" else f"/usr/bin/{t}")
    ok, missing = bolt.tools_available()
    assert not ok and missing == ["perf2bolt"]


def test_tools_available_skips_perf_when_not_needed(monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: None if t == "perf" else f"/usr/bin/{t}")
    ok, missing = bolt.tools_available(need_perf=False)
    assert ok and missing == []


# ---------------------------------------------------------------------------
# standalone_build_viable — BLOCKED guard for dylib-only LLVM installs
# ---------------------------------------------------------------------------

def test_standalone_build_viable_true_with_static_components(tmp_path):
    # A full static LLVM install ships the per-component archives BOLT links.
    for name in bolt._STATIC_COMPONENT_PROBE:
        (tmp_path / name).write_text("")
    assert bolt.standalone_build_viable(tmp_path) is True


def test_standalone_build_viable_false_for_dylib_only(tmp_path):
    # Dylib-only install (PGO toolchain / stock Arch llvm): only libLLVM.so + a few
    # static libs, none of the component archives BOLT's static-linked tools need.
    (tmp_path / "libLLVM.so").write_text("")
    (tmp_path / "libLLVMSupport.a").write_text("")  # shipped static, but not a probe
    assert bolt.standalone_build_viable(tmp_path) is False


def test_standalone_build_viable_false_when_partial(tmp_path):
    # Even one missing probe archive blocks the standalone build.
    (tmp_path / bolt._STATIC_COMPONENT_PROBE[0]).write_text("")
    assert bolt.standalone_build_viable(tmp_path) is False


# ---------------------------------------------------------------------------
# collect_profile + bolt_binary (subprocess mocked)
# ---------------------------------------------------------------------------

def test_collect_profile_missing_tools_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: None)
    with pytest.raises(bolt.BoltError) as exc:
        bolt.collect_profile(Path("/usr/bin/clang"), tmp_path, ["clang++", "-c", "x.cpp"])
    assert "perf" in str(exc.value)


def test_collect_profile_success(tmp_path, monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: f"/usr/bin/{t}")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[0])
        # perf2bolt writes the .fdata
        if argv[0] == "perf2bolt":
            out_idx = argv.index("-o") + 1
            Path(argv[out_idx]).write_text("fdata")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bolt.subprocess, "run", fake_run)
    fdata = bolt.collect_profile(Path("/usr/bin/clang"), tmp_path, ["clang++", "-c", "x.cpp"])
    assert fdata == tmp_path / bolt.FDATA_NAME
    assert fdata.read_text() == "fdata"
    assert calls == ["perf", "perf2bolt"]


def test_collect_profile_tool_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: f"/usr/bin/{t}")
    monkeypatch.setattr(
        bolt.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "perf_event_paranoid"),
    )
    with pytest.raises(bolt.BoltError) as exc:
        bolt.collect_profile(Path("/usr/bin/clang"), tmp_path, ["clang++", "-c", "x.cpp"])
    assert "perf_event_paranoid" in str(exc.value)


def test_bolt_binary_success(tmp_path, monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: f"/usr/bin/{t}")
    fdata = tmp_path / "llvm.fdata"
    fdata.write_text("p")
    binary = tmp_path / "clang"
    binary.write_text("elf")

    def fake_run(argv, **kw):
        out_idx = argv.index("-o") + 1
        Path(argv[out_idx]).write_text("bolted")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bolt.subprocess, "run", fake_run)
    out = bolt.bolt_binary(binary, fdata)
    assert out == binary.with_suffix(binary.suffix + ".bolt")
    assert out.read_text() == "bolted"


def test_bolt_binary_missing_profile_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bolt.shutil, "which", lambda t: f"/usr/bin/{t}")
    with pytest.raises(bolt.BoltError) as exc:
        bolt.bolt_binary(tmp_path / "clang", tmp_path / "absent.fdata")
    assert "no BOLT profile" in str(exc.value)


# ---------------------------------------------------------------------------
# llvm-bolt PKGBUILD generation
# ---------------------------------------------------------------------------

def test_render_pkgbuild_version_locks_to_llvm():
    text = bolt.render_pkgbuild("22.1.8")
    assert "pkgname=llvm-bolt" in text
    assert "pkgver=22.1.8" in text
    # builds the bolt subtree of the same monorepo tarball, standalone
    assert "llvm-project-$pkgver.src/bolt" in text
    assert 'makedepends=("llvm=$pkgver"' in text
    assert "sha256sums=('SKIP')" in text
    # experimental provenance is stated in the generated file
    assert "experimental" in text.lower()


def test_render_pkgbuild_is_valid_bash_syntax():
    import shutil as _sh
    import subprocess as _sp
    if _sh.which("bash") is None:
        pytest.skip("bash not available")
    text = bolt.render_pkgbuild("22.1.8")
    # `bash -n` parses without executing — catches a broken brace/quote in the
    # generated PKGBUILD before makepkg ever sees it.
    res = _sp.run(["bash", "-n"], input=text, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_materialize_pkgbuild_writes_versioned_file(tmp_path):
    pkgbuild = bolt.materialize_pkgbuild(tmp_path, "22.1.8")
    assert pkgbuild == tmp_path / "llvm-bolt" / "PKGBUILD"
    assert pkgbuild.is_file()
    assert "pkgver=22.1.8" in pkgbuild.read_text()


def test_materialize_pkgbuild_overwrites_with_new_version(tmp_path):
    bolt.materialize_pkgbuild(tmp_path, "21.0.0")
    pkgbuild = bolt.materialize_pkgbuild(tmp_path, "22.1.8")
    text = pkgbuild.read_text()
    assert "pkgver=22.1.8" in text and "pkgver=21.0.0" not in text
