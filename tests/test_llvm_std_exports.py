# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""3.5.0-F4: libLLVM std:: export parity thunks."""

import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives import llvm_std_exports as lse
from sysforge.primitives import pkgbuild_patcher

_NODE = "LLVM_23.1"
_REPLACE = next(e for e in lse.STD_EXPORTS if "10_M_replaceEmmPKcm" in e.symbol)

# A stock-like libLLVM: exports two members @@LLVM_23.1, as stock's `global: *` does.
_STOCK_LIKE = """#include <string>
using S = std::__cxx11::basic_string<char>;
template S& S::_M_replace(S::size_type, S::size_type, const char*, S::size_type);
template void S::_M_assign(const S&);
"""
# A consumer that imports _M_replace and _M_assign. Built as C++17, as LLVM and
# mesa are: from C++20 the members are constexpr, so a consumer emits its own.
_CONSUMER_STD = ("-std=gnu++17",)
_APP = """#include <string>
#include <cstdio>
int main(int argc, char**) { std::string a = "hello", b(argc + 2, 'x');
  a.replace(1, 2, "ZZZ"); b = a; std::puts(b.c_str()); }
"""


def test_table_is_eleven_unique_string_members():
    syms = [e.symbol for e in lse.STD_EXPORTS]
    assert len(syms) == len(set(syms)) == 11
    assert all(s.startswith("_ZNSt7__cxx1112basic_string") for s in syms)
    assert all(e.libstdcxx_version.startswith("GLIBCXX_3.4.") for e in lse.STD_EXPORTS)


def test_render_tu_has_one_thunk_per_entry():
    tu = lse.render_tu()
    for n, e in enumerate(lse.STD_EXPORTS):
        assert tu.count(f"{e.symbol}@{e.libstdcxx_version}") == 1
        assert (f'".symver __sysforge_std_compat_{n}, {e.symbol}@" '
                "SF_STR(SYSFORGE_SHLIB_NODE)") in tu
        assert f".globl __sysforge_std_compat_{n}" in tu
    assert ("#if defined(__x86_64__) && defined(__GLIBCXX__) "
            "&& defined(SYSFORGE_SHLIB_NODE)") in tu


def test_rendered_text_is_brace_free_for_prepare():
    """Pasted into prepare(), whose span the patcher finds by counting braces;
    the hook's one balanced ${LLVM_SHLIB_SYMBOL_VERSION} is the only exception."""
    assert "{" not in lse.render_tu() and "}" not in lse.render_tu()
    hook = lse.render_cmake_hook().replace("${LLVM_SHLIB_SYMBOL_VERSION}", "")
    assert "{" not in hook and "}" not in hook
    block = lse.render_prepare_block()
    assert block.count("{") == block.count("}") == 1


def test_cmake_hook_condition():
    hook = lse.render_cmake_hook()
    cond = hook[hook.index("if("):hook.index("target_sources")]
    for term in ("TARGET LLVM", "LLVM_LINKER_IS_LLD", "LLVM_SHLIB_SYMBOL_VERSION",
                 'CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|AMD64)$"'):
        assert term in cond
    assert f"target_sources(LLVM PRIVATE {lse.TU_RELPATH})" in hook
    assert 'COMPILE_DEFINITIONS "SYSFORGE_SHLIB_NODE=${LLVM_SHLIB_SYMBOL_VERSION}"' in hook
    assert lse.SENTINEL in hook


def test_table_versions_exist_in_host_libstdcxx():
    lib = Path("/usr/lib/libstdc++.so.6")
    if not lib.exists() or shutil.which("nm") is None:
        pytest.skip("no host libstdc++ or nm")
    exported = {s.replace("@@", "@") for s in _dynsyms(lib, undefined=False)}
    for e in lse.STD_EXPORTS:
        assert f"{e.symbol}@{e.libstdcxx_version}" in exported


def _toolchain(cxx: str, ld: str) -> list[str]:
    if shutil.which(cxx) is None or shutil.which("nm") is None:
        pytest.skip(f"{cxx} or nm not installed")
    return [cxx, f"-fuse-ld={ld}"]


def _shared(tc, tmp: Path, srcs: list[Path], out: Path, defines=()) -> None:
    out.parent.mkdir(exist_ok=True)
    subprocess.run(
        [*tc, "-O2", "-fPIC", *(f"-D{d}" for d in defines), *map(str, srcs),
         "-shared", f"-Wl,--version-script={tmp / 'v.map'}",
         "-Wl,-Bsymbolic-functions", "-Wl,-soname,libL.so", "-o", str(out)],
        check=True, capture_output=True)


def _dynsyms(path: Path, *, undefined: bool) -> set[str]:
    flag = "--undefined-only" if undefined else "--defined-only"
    out = subprocess.run(["nm", "-D", flag, str(path)], capture_output=True,
                         text=True, check=True).stdout
    return {line.split()[-1] for line in out.splitlines() if line.split()}


@pytest.mark.parametrize(("cxx", "ld"), [("clang++", "lld"), ("g++", "bfd")])
def test_thunks_serve_old_consumers_without_binding_new_links(tmp_path, cxx, ld):
    tc = _toolchain(cxx, ld)
    (tmp_path / "v.map").write_text(f"{_NODE} {{ global: *; }};\n")
    tu, stock, app = (tmp_path / n for n in (lse.TU_FILENAME, "stock.cpp", "app.cpp"))
    tu.write_text(lse.render_tu())
    stock.write_text(_STOCK_LIKE)
    app.write_text(_APP)
    _shared(tc, tmp_path, [stock], tmp_path / "old/libL.so")
    _shared(tc, tmp_path, [tu], tmp_path / "new/libL.so",
            defines=[f"SYSFORGE_SHLIB_NODE={_NODE}"])

    new_defs = _dynsyms(tmp_path / "new/libL.so", undefined=False)
    for e in lse.STD_EXPORTS:
        assert f"{e.symbol}@{_NODE}" in new_defs          # non-default
        assert f"{e.symbol}@@{_NODE}" not in new_defs     # never default
    new_und = {s for s in _dynsyms(tmp_path / "new/libL.so", undefined=True)
               if s.startswith("_ZNSt")}
    assert new_und == {f"{e.symbol}@{e.libstdcxx_version}" for e in lse.STD_EXPORTS}

    # A consumer linked against stock binds @LLVM_23.1 and runs against the thunks.
    subprocess.run([*tc, *_CONSUMER_STD, "-O2", str(app), f"-L{tmp_path / 'old'}", "-lL",
                    "-o", str(tmp_path / "app_old")], check=True, capture_output=True)
    assert f"{_REPLACE.symbol}@{_NODE}" in _dynsyms(tmp_path / "app_old", undefined=True)
    ran = subprocess.run([str(tmp_path / "app_old")], capture_output=True, text=True,
                         env={"LD_LIBRARY_PATH": str(tmp_path / "new")}, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == "hZZZlo\n"

    # A fresh link against the thunk library binds libstdc++, never the LLVM node.
    subprocess.run([*tc, *_CONSUMER_STD, "-O2", str(app), f"-L{tmp_path / 'new'}", "-lL",
                    "-o", str(tmp_path / "app_new")], check=True, capture_output=True)
    assert not any("@LLVM_" in s for s in _dynsyms(tmp_path / "app_new", undefined=True))


def test_tu_without_node_defines_nothing(tmp_path):
    """No SYSFORGE_SHLIB_NODE (the hook configured out, or an empty node):
    the TU compiles to no symbols instead of an assembler error."""
    cxx = next((c for c in ("clang++", "g++") if shutil.which(c)), None)
    if cxx is None or shutil.which("nm") is None:
        pytest.skip("no C++ compiler or nm")
    tu = tmp_path / lse.TU_FILENAME
    tu.write_text(lse.render_tu())
    obj = tmp_path / "tu.o"
    subprocess.run([cxx, "-O2", "-fPIC", "-c", str(tu), "-o", str(obj)],
                   check=True, capture_output=True)
    out = subprocess.run(["nm", str(obj)], capture_output=True, text=True).stdout
    assert "__sysforge" not in out and "_ZNSt" not in out


_PKGBUILD = """pkgbase=llvm
pkgname=('llvm' 'llvm-libs')
pkgver=23.1.3
pkgrel=3
arch=('x86_64')
makedepends=('cmake' 'ninja')
source=("llvm-project-$pkgver.src.tar.xz")
sha256sums=('SKIP')

prepare() {
  cd llvm-project-$pkgver.src
  rm -f llvm/cmake/modules/Findzstd.cmake
}

build() {
  cmake -S llvm -B build -DLLVM_BUILD_LLVM_DYLIB=ON
}

package_llvm() {
  true
}

package_llvm-libs() {
  true
}
"""


def _patched(tmp_path) -> Path:
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD)
    assert pkgbuild_patcher.patch_llvm_std_exports(p) is True
    return p


def test_patch_appends_block_inside_prepare(tmp_path):
    text = _patched(tmp_path).read_text()
    o, c = pkgbuild_patcher._find_func_body_span(text, "prepare")
    body = text[o:c]
    assert lse.SENTINEL in body and body.rstrip().endswith("done")
    assert "rm -f llvm/cmake/modules/Findzstd.cmake" in body
    ob, cb = pkgbuild_patcher._find_func_body_span(text, "build")
    assert lse.SENTINEL not in text[ob:cb]


def test_patch_is_idempotent(tmp_path):
    p = _patched(tmp_path)
    before = p.read_text()
    assert pkgbuild_patcher.patch_llvm_std_exports(p) is False
    assert p.read_text() == before


def test_patch_without_prepare_is_a_noop(tmp_path):
    p = tmp_path / "PKGBUILD.sysforge"
    text = _PKGBUILD.replace("prepare() {", "unused() {")
    p.write_text(text)
    assert pkgbuild_patcher.patch_llvm_std_exports(p) is False
    assert p.read_text() == text


def test_patched_pkgbuild_validates(tmp_path):
    orig = tmp_path / "PKGBUILD"
    orig.write_text(_PKGBUILD)
    pkgbuild_patcher.validate_patched_pkgbuild(orig, _patched(tmp_path))


def _run_prepare(pkgbuild: Path, srcdir: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    text = pkgbuild.read_text()
    o, c = pkgbuild_patcher._find_func_body_span(text, "prepare")
    script = (f"set -euo pipefail\nsrcdir={shlex.quote(str(srcdir))}\npkgver=23.1.3\n"
              f"prepare() {{{text[o + 1:c]}}}\ncd \"$srcdir\"\nprepare\n")
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True)


def _shlib(srcdir: Path, tree: str) -> Path:
    d = srcdir / tree / "llvm/tools/llvm-shlib"
    d.mkdir(parents=True)
    (d / "CMakeLists.txt").write_text("# stock\n")
    return d


def test_prepare_block_writes_tu_and_hook(tmp_path):
    src = tmp_path / "src"
    shlib = _shlib(src, "llvm-project-23.1.3.src")
    _run_prepare(_patched(tmp_path), src)
    assert (shlib / lse.TU_RELPATH).read_text() == lse.render_tu()
    assert (shlib / "CMakeLists.txt").read_text() == "# stock\n" + lse.render_cmake_hook()


def test_prepare_block_is_idempotent_at_runtime(tmp_path):
    src = tmp_path / "src"
    shlib = _shlib(src, "llvm-project-23.1.3.src")
    p = _patched(tmp_path)
    _run_prepare(p, src)
    _run_prepare(p, src)
    assert (shlib / "CMakeLists.txt").read_text().count(lse.SENTINEL) == 1


def test_prepare_block_patches_git_tree_name(tmp_path):
    """A -git checkout's tree is llvm-project; the .src dir prepare() cd's into
    has no llvm-shlib here and is left alone."""
    src = tmp_path / "src"
    (src / "llvm-project-23.1.3.src").mkdir(parents=True)
    shlib = _shlib(src, "llvm-project")
    _run_prepare(_patched(tmp_path), src)
    assert (shlib / lse.TU_RELPATH).is_file()
    assert not (src / "llvm-project-23.1.3.src" / "llvm").exists()


def test_prepare_block_skips_tree_without_shlib(tmp_path):
    src = tmp_path / "src"
    (src / "llvm-project-23.1.3.src").mkdir(parents=True)
    _run_prepare(_patched(tmp_path), src)
    assert not list(src.rglob(lse.TU_FILENAME))


def test_wrapper_patches_llvm_pkgbase(tmp_path):
    from sysforge.build.makepkg_wrapper import _maybe_patch_llvm_std_exports
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD)
    assert _maybe_patch_llvm_std_exports(p, {"globals": {"pkgname": ["llvm", "llvm-libs"]}})
    assert lse.SENTINEL in p.read_text()


@pytest.mark.parametrize("names", [["lib32-llvm", "lib32-llvm-libs"], "htop"])
def test_wrapper_skips_lib32_and_non_llvm(tmp_path, names):
    from sysforge.build.makepkg_wrapper import _maybe_patch_llvm_std_exports
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(_PKGBUILD)
    assert _maybe_patch_llvm_std_exports(p, {"globals": {"pkgname": names}}) is False
    assert p.read_text() == _PKGBUILD


def test_run_build_validates_after_std_export_patch(tmp_path):
    """The patch's result ORs into the flag that runs validate_patched_pkgbuild."""
    from sysforge.build import makepkg_wrapper as mw
    from sysforge.primitives.makepkg_invoke import AlreadyBuilt

    pb = tmp_path / "PKGBUILD"
    pb.write_text(_PKGBUILD)
    seen = {}

    @contextmanager
    def fake_emit(*a, **kw):
        yield "/tmp/fake_makepkg.conf"

    def stop(*a, **kw):
        raise AlreadyBuilt(pb)

    with (
        patch.object(mw, "patch_pkgbuild_groups", return_value=pb),
        patch.object(mw, "_maybe_patch_llvm_targets", return_value=False),
        patch.object(mw, "_apply_staged_llvm_patches", return_value=False),
        patch.object(mw, "_maybe_patch_mesa_drivers", return_value=None),
        patch.object(mw, "_maybe_patch_llvm_std_exports",
                     side_effect=lambda path, meta: seen.setdefault("args", (path, meta)) and True),
        patch.object(mw, "validate_patched_pkgbuild",
                     side_effect=lambda o, p, **kw: seen.setdefault("validated", (o, p))),
        patch.object(mw, "emit_makepkg_conf", side_effect=fake_emit),
        patch.object(mw, "resolve_env_vars", return_value={}),
        patch.object(mw, "_invoke_with_retry", side_effect=stop),
    ):
        meta = {"globals": {"pkgname": ["llvm", "llvm-libs"]}}
        with pytest.raises(AlreadyBuilt):
            mw._run_build(pb, {"batch": True}, {}, [], extracted_profile=None, pkgmeta=meta)
    assert seen["args"] == (pb, meta)
    assert seen["validated"] == (pb, pb)


def test_fingerprint_schema_covers_std_export_patch():
    """3.5.0-F4: the fingerprint hashes the upstream PKGBUILD, which never shows
    the parity block, so the schema bump is what keeps --reuse-built from reusing
    a libLLVM built without the thunks."""
    from sysforge.primitives import build_fingerprint
    assert build_fingerprint._SCHEMA >= 4


# ---------------------------------------------------------------------------
#  Final-review fixes: LLVM's own configure, and a re-extracted srcdir
# ---------------------------------------------------------------------------

_LLVM_CMAKE = Path("/usr/lib/cmake/llvm")


def _toy_llvm_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose llvm-shlib/ mirrors LLVM's: llvm_process_sources (the
    check that rejects any *.cpp in the directory it was not given), then the
    LLVM target, linked with a version script as libLLVM is."""
    src = tmp_path / "src"
    shlib = src / "llvm-project-23.1.3.src/llvm/tools/llvm-shlib"
    shlib.mkdir(parents=True)
    (shlib / "libllvm.cpp").write_text("int sysforge_toy_libllvm() { return 0; }\n")
    (shlib / "v.map").write_text(f"{_NODE} {{ global: *; }};\n")
    (shlib / "CMakeLists.txt").write_text(
        "llvm_process_sources(SOURCES libllvm.cpp)\n"
        "add_library(LLVM SHARED ${SOURCES})\n"
        "target_link_options(LLVM PRIVATE"
        ' "-Wl,--version-script=${CMAKE_CURRENT_SOURCE_DIR}/v.map")\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(toy CXX)\n"
        f'list(APPEND CMAKE_MODULE_PATH "{_LLVM_CMAKE}")\ninclude(LLVMProcessSources)\n'
        f'add_subdirectory("{shlib}" shlib)\n'
        'get_target_property(_sf_srcs LLVM SOURCES)\nmessage(STATUS "SF_SOURCES=${_sf_srcs}")\n')
    return src, proj


@pytest.mark.parametrize(("lld", "node", "wired"), [
    ("ON", _NODE, True), ("ON", "", False), ("OFF", _NODE, False)])
def test_hook_passes_llvm_source_check_and_self_gates(tmp_path, lld, node, wired):
    import platform
    if (platform.machine() != "x86_64" or shutil.which("cmake") is None
            or shutil.which("clang++") is None or shutil.which("ld.lld") is None
            or not (_LLVM_CMAKE / "LLVMProcessSources.cmake").is_file()):
        pytest.skip("needs x86-64, cmake, clang++, lld and LLVM's cmake modules")
    src, proj = _toy_llvm_tree(tmp_path)
    _run_prepare(_patched(tmp_path), src)
    build = tmp_path / "build"
    cfg = subprocess.run(
        ["cmake", "-S", str(proj), "-B", str(build), "-DCMAKE_CXX_COMPILER=clang++",
         "-DCMAKE_SHARED_LINKER_FLAGS=-fuse-ld=lld", f"-DLLVM_LINKER_IS_LLD={lld}",
         f"-DLLVM_SHLIB_SYMBOL_VERSION={node}"], capture_output=True, text=True)
    assert cfg.returncode == 0, cfg.stdout + cfg.stderr
    sources = next(ln for ln in cfg.stdout.splitlines() if "SF_SOURCES=" in ln)
    assert (lse.TU_FILENAME in sources) is wired
    if wired:
        subprocess.run(["cmake", "--build", str(build)], check=True, capture_output=True)
        lib = next(build.rglob("libLLVM.so"))
        assert f"{_REPLACE.symbol}@{_NODE}" in _dynsyms(lib, undefined=False)


def test_prepare_block_rehooks_after_reextract(tmp_path):
    """makepkg re-extracts over a kept srcdir (no -C): CMakeLists.txt reverts to
    stock while the untracked TU survives, and the hook must come back."""
    src = tmp_path / "src"
    shlib = _shlib(src, "llvm-project-23.1.3.src")
    p = _patched(tmp_path)
    _run_prepare(p, src)
    (shlib / "CMakeLists.txt").write_text("# stock\n")
    _run_prepare(p, src)
    assert (shlib / "CMakeLists.txt").read_text() == "# stock\n" + lse.render_cmake_hook()
