# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/bolt.py — BOLT post-link optimization orchestration."""
import subprocess
from pathlib import Path

from sysforge.primitives import bolt


# ---------------------------------------------------------------------------
# Store + flags + build-mode
# ---------------------------------------------------------------------------

def test_store_is_bolt_method_subdir(monkeypatch):
    # FHS default root: opt out of conftest's store isolation (path only, no I/O).
    monkeypatch.delenv("SYSFORGE_PROFILE_STORE", raising=False)
    store = bolt.resolve_store({})
    assert store.name == "bolt"
    assert store.parent.name == "sysforge"


def test_store_honours_profile_store_override(tmp_path):
    assert bolt.resolve_store({"profile_store": str(tmp_path)}) == tmp_path / "bolt"


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

def test_write_default_workload(tmp_path):
    src = bolt.write_default_workload(tmp_path)
    assert src.is_file() and src.suffix == ".cpp"
    text = src.read_text()
    assert "#include" in text and "int main()" in text


# ---------------------------------------------------------------------------
# 3.5.0-F3: BOLT inside the llvm build
# ---------------------------------------------------------------------------

def _tree(root: Path) -> Path:
    build = root / "llvm" / "src" / "llvm-project-23.1.3.src" / "build"
    (build / "lib").mkdir(parents=True)
    (build / "build.ninja").write_text("")
    return build


def test_build_tree_for_finds_the_tree_under_builddir(tmp_path, monkeypatch):
    from sysforge.primitives import pacman
    build = _tree(tmp_path / "builds")
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "builds")
    assert bolt.build_tree_for(tmp_path / "src/llvm/PKGBUILD") == build


def test_build_tree_for_without_builddir_uses_the_pkgbuild_dir(tmp_path, monkeypatch):
    from sysforge.primitives import pacman
    build = _tree(tmp_path / "src")
    monkeypatch.setattr(pacman, "get_builddir", lambda: None)
    assert bolt.build_tree_for(tmp_path / "src/llvm/PKGBUILD") == build


def test_build_tree_for_missing_tree_is_none(tmp_path, monkeypatch):
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path)
    assert bolt.build_tree_for(tmp_path / "src/llvm/PKGBUILD") is None


def test_bolt_targets_are_the_shared_libraries_not_the_driver(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for real in ("libclang-cpp.so.23.1", "libLLVM.so.23.1"):
        (lib / real).write_bytes(b"\x7fELF")
    (lib / "libclang-cpp.so").symlink_to("libclang-cpp.so.23.1")
    (lib / "libLLVM.so").symlink_to("libLLVM.so.23.1")
    assert bolt.bolt_targets(tmp_path, libllvm=False) == [lib / "libclang-cpp.so.23.1"]
    assert bolt.bolt_targets(tmp_path, libllvm=True) == [
        lib / "libclang-cpp.so.23.1", lib / "libLLVM.so.23.1"]


def _probe(monkeypatch, stdout, rc=0):
    calls = []
    monkeypatch.setattr(bolt.run, "probe", lambda argv, **kw: calls.append(argv)
                        or subprocess.CompletedProcess(argv, rc, stdout, ""))
    return calls


def test_has_relocations_reads_the_rela_text_section(monkeypatch):
    _probe(monkeypatch, "  [12] .rela.text RELA 0000 ...\n")
    assert bolt.has_relocations(Path("/x/libclang-cpp.so.23.1"))
    _probe(monkeypatch, "  [12] .rela.dyn RELA 0000 ...\n")
    assert not bolt.has_relocations(Path("/x/libclang-cpp.so.23.1"))


def test_runtime_with_got_calls_is_not_linkable(monkeypatch):
    """A runtime built by GCC with -fno-plt carries GOTPCRELX calls that BOLT's
    JITLink rejects for every input (the packaged llvm-bolt 23.1.3-3 does)."""
    _probe(monkeypatch, "0000 000b0000002a R_X86_64_GOTPCRELX 0 memset - 4\n")
    assert not bolt.runtime_linkable(Path("/x/libbolt_rt_instr.a"))
    _probe(monkeypatch, "0000 000b00000004 R_X86_64_PLT32 0 memset - 4\n")
    assert bolt.runtime_linkable(Path("/x/libbolt_rt_instr.a"))


_NINJA_COMMANDS = "\n".join([
    "/usr/bin/sccache /usr/bin/clang++ -DFOO -I/b/include -O2 -MD -MT tools/x.o"
    " -MF tools/x.o.d -o tools/clang/lib/Sema/Sema.cpp.o -c /s/clang/lib/Sema/Sema.cpp",
    "/usr/bin/clang++ -O2 -MD -MT a.o -MF a.o.d -o lib/Support/APInt.cpp.o"
    " -c /s/llvm/lib/Support/APInt.cpp",
    "/usr/bin/clang++ -O2 -o tools/clang/lib/AST/Decl.cpp.o -c /s/clang/lib/AST/Decl.cpp",
    ": && /usr/bin/clang++ -shared -o lib/libclang-cpp.so.23.1 @rsp && :",
]) + "\n"


def test_training_commands_rewrite_the_compiler_and_outputs(tmp_path, monkeypatch):
    _probe(monkeypatch, _NINJA_COMMANDS)
    cmds = bolt.training_commands(Path("/b"), Path("/b/bin/clang++"), tmp_path,
                                  target="lib/libclang-cpp.so.23.1", limit=10)
    # compile lines only, launcher dropped, front-end (clang/lib) sources first
    assert [c[0] for c in cmds] == ["/b/bin/clang++"] * 3
    assert cmds[0][-1].endswith("Sema.cpp") and cmds[1][-1].endswith("Decl.cpp")
    for c in cmds:
        assert "-MD" not in c and "-MF" not in c and "-MT" not in c
        assert Path(c[c.index("-o") + 1]).parent == tmp_path


def test_training_commands_respect_the_limit(tmp_path, monkeypatch):
    _probe(monkeypatch, _NINJA_COMMANDS)
    assert len(bolt.training_commands(Path("/b"), Path("/b/bin/clang++"), tmp_path,
                                      target="t", limit=2)) == 2


def test_optimize_cmd_uses_the_llvm23_flag_spellings(tmp_path):
    argv = bolt.optimize_cmd(tmp_path / "llvm-bolt", tmp_path / "lib.so",
                             tmp_path / "lib.fdata", tmp_path / "out.so")
    assert "-reorder-functions=cdsort" in argv and "-icf=all" in argv
    assert not any("hfsort" in a for a in argv) and "-icf=1" not in argv


def test_instrument_cmd_appends_pid_and_names_the_runtime(tmp_path):
    argv = bolt.instrument_cmd(tmp_path / "llvm-bolt", tmp_path / "lib.so", tmp_path / "o.so",
                               tmp_path / "lib.fdata", tmp_path / "rt.a")
    assert "-instrument" in argv and "--instrumentation-file-append-pid" in argv
    assert f"--runtime-instrumentation-lib={tmp_path / 'rt.a'}" in argv


def test_replace_keep_mtime_keeps_ninja_from_relinking(tmp_path):
    import os
    dest, new = tmp_path / "libclang-cpp.so.23.1", tmp_path / "bolted"
    dest.write_bytes(b"old")
    os.utime(dest, (1_000_000, 1_000_000))
    new.write_bytes(b"bolted")
    bolt.replace_keep_mtime(new, dest)
    assert dest.read_bytes() == b"bolted"
    assert dest.stat().st_mtime == 1_000_000

