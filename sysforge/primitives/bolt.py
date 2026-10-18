# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bolt.py — BOLT post-link optimization of the PGO toolchain (3.5.0-F3)

The one home for sysforge's BOLT (Binary Optimization and Layout Tool) facts and
subprocess seams — the fourth optimization method on the shared profile-store
rails, and the only one that is **post-link** rather than compile- or
sample-driven.

PGO, mesa PGO and kernel AutoFDO are all *flag*-driven: you inject a compiler
flag (`-fprofile-use`, `-fprofile-sample-use`) and the compiler does the rest.
BOLT is different — there is no "BOLT flag". You link with `-Wl,--emit-relocs`
to keep relocations, then a separate tool (`llvm-bolt`) rewrites the finished
binary using a profile. sysforge applies it to the PGO toolchain (the
canonical PGO→BOLT "fast clang" stack), and where it runs is dictated by four
facts a 2026-10-17 spike established:

  * **Target the shared libraries.** With ``LLVM_LINK_LLVM_DYLIB=ON`` the
    ``clang`` executable is a ~100 KB driver; the compiler's code is in
    ``libclang-cpp.so`` and ``libLLVM.so``.
  * **Run inside the build.** BOLT refuses stripped input and instrumentation
    needs relocations, but makepkg strips while packaging. makepkg only strips
    the copies in ``$pkgdir``, so the *build tree* still holds unstripped,
    relocation-carrying libraries: BOLT them there, then ``makepkg --repackage``
    reruns ``package()`` from the tree. The replacement keeps the original
    mtime (:func:`replace_keep_mtime`), or ninja relinks the library during
    ``package()`` and silently undoes the rewrite.
  * **Profile by instrumentation.** ``perf record -j any`` needs branch-stack
    sampling, which the tested Zen 3 hardware does not have.
    ``llvm-bolt -instrument`` is hardware-independent.
  * **Use the in-tree tools, and check the runtime.** Upstream builds ``bolt``
    in the same tree, so ``llvm-bolt``/``merge-fdata``/``libbolt_rt_instr.a``
    sit in the build directory. A runtime built by GCC with ``-fno-plt`` (Arch's
    packaged one) routes ``memset`` through the GOT, which BOLT's JITLink
    rejects for every input; :func:`runtime_linkable` detects that up front.

The orchestration lives in the toolchain stage (``toolchain/bolt.py``); this
module is the facts plus the subprocess seams.
"""
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_pgo import resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives import run

_log = log.get_logger("BOLT")

# The optimization build_mode this flow records (see profile.is_optimized_build_mode
# / profile.rename_mode_for_build_mode → "conflict"). The toolchain stage stamps it
# for provenance on the BOLT-optimized members.
BUILD_MODE = "bolt_llvm"

# Linker flag that retains relocations in the final binary — BOLT needs them to
# rewrite safely, and instrumentation refuses to run without them. Injected into
# the Pass-4a link (LDFLAGS) when BOLT is enabled.
EMIT_RELOCS_LDFLAG = "-Wl,--emit-relocs"

# llvm-bolt optimization set for a PGO'd clang/libLLVM, in LLVM 23 spellings
# (`hfsort+` and a numeric `-icf` are deprecated there): ext-tsp block layout,
# cdsort function ordering, function/cold/EH splitting, identical-code folding.
_BOLT_OPT_FLAGS = (
    "-reorder-blocks=ext-tsp",
    "-reorder-functions=cdsort",
    "-split-functions",
    "-split-all-cold",
    "-split-eh",
    "-icf=all",
    "-dyno-stats",
)

# Shared-library basenames BOLT rewrites, by role. libclang-cpp is always the
# target; libLLVM only behind `[bolt] libllvm`, since rewriting the library every
# libLLVM consumer on the system loads is the more fragile half.
_CLANG_LIB = "libclang-cpp.so"
_LLVM_LIB = "libLLVM.so"

# A real versioned soname file (libfoo.so.23.1), never the unversioned symlink.
_RE_VERSIONED = re.compile(r"\.so\.\d+(\.\d+)*$")

# Launchers a build may prefix to the compiler in `ninja -t commands` output.
_LAUNCHERS = {"ccache", "sccache"}

# Dependency-file flags in a compile command: they would write into the build
# tree, so the training replay drops them.
_DEPFILE_FLAG = {"-MD", "-MMD"}
_DEPFILE_ARG = {"-MF", "-MT", "-MQ"}


class BoltError(Exception):
    """A BOLT step could not complete. Raised so the caller restores the
    original libraries and keeps the un-BOLTed packages, rather than shipping a
    half-rewritten toolchain."""


def _load_tcfg() -> dict | None:
    """Best-effort load of toolchain.toml for store-path overrides (pure)."""
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with TOOLCHAIN_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def resolve_store(tcfg: dict | None = None) -> Path:
    """Resolve BOLT's profile store dir (``<profile_store_root>/bolt``)."""
    return resolve_method_store(tcfg if tcfg is not None else _load_tcfg(), "bolt")


def emit_relocs_ldflag() -> str:
    """The linker flag that keeps the PGO build BOLT-able (retains relocations)."""
    return EMIT_RELOCS_LDFLAG


# ---------------------------------------------------------------------------
# The build tree and what is in it
# ---------------------------------------------------------------------------

def build_tree_for(pkgbuild_path: Path) -> Path | None:
    """The CMake build directory the last ``makepkg`` of ``pkgbuild_path`` left.

    ``<BUILDDIR>/<pkgbase>/src/llvm-project-*/build`` (makepkg builds in the
    PKGBUILD's own directory when ``BUILDDIR`` is unset). ``--cleanbuild`` clears
    ``src`` *before* a build, so after Pass 4a the tree is that pass's. None when
    no tree with a ``build.ninja`` exists.
    """
    from sysforge.primitives import pacman

    builddir = pacman.get_builddir()
    base = builddir / pkgbuild_path.parent.name if builddir else pkgbuild_path.parent
    for tree in sorted((base / "src").glob("llvm-project-*/build")):
        if (tree / "build.ninja").is_file():
            return tree
    return None


def bolt_targets(build: Path, *, libllvm: bool) -> list[Path]:
    """The real (non-symlink) versioned shared libraries to rewrite, in order."""
    names = [_CLANG_LIB, *([_LLVM_LIB] if libllvm else [])]
    out: list[Path] = []
    for name in names:
        out.extend(
            p for p in sorted((build / "lib").glob(f"{name}.*"))
            if _RE_VERSIONED.search(p.name) and p.is_file() and not p.is_symlink()
        )
    return out


def tree_tool(build: Path, name: str) -> Path:
    """Path of an in-tree BOLT tool or runtime (``bin/llvm-bolt``, ``lib/…``)."""
    return build / ("lib" if name.endswith(".a") else "bin") / name


def has_relocations(lib: Path) -> bool:
    """True when ``lib`` was linked with ``--emit-relocs`` (a ``.rela.text``)."""
    result = run.probe(["readelf", "-SW", str(lib)], stdin=subprocess.DEVNULL)
    return result.returncode == 0 and ".rela.text" in result.stdout


def runtime_linkable(runtime: Path) -> bool:
    """False when the instrumentation runtime carries GOT-relative calls.

    BOLT links ``libbolt_rt_instr.a`` into the instrumented binary with JITLink,
    which rejects ``R_X86_64_GOTPCRELX`` ("unsupported edge kind
    RequestGOTAndTransformToPCRel32GOTLoadRelaxable"). GCC emits those for its
    own ``memset`` calls under ``-fno-plt``; clang does not.
    """
    result = run.probe(["readelf", "-rW", str(runtime)], stdin=subprocess.DEVNULL)
    return result.returncode == 0 and "GOTPCRELX" not in result.stdout


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------

def instrument_cmd(
    llvm_bolt: Path, lib: Path, out: Path, fdata_prefix: Path, runtime: Path,
) -> list[str]:
    """``llvm-bolt -instrument``: each process that loads ``out`` writes
    ``<fdata_prefix>.<pid>.fdata`` when it exits."""
    return [
        str(llvm_bolt), str(lib), "-instrument", "-o", str(out),
        f"--instrumentation-file={fdata_prefix}",
        "--instrumentation-file-append-pid",
        f"--runtime-instrumentation-lib={runtime}",
    ]


def merge_cmd(merge_fdata: Path, files: list[Path], out: Path) -> list[str]:
    """``merge-fdata``: fold the per-process profiles into one."""
    return [str(merge_fdata), *map(str, files), "-o", str(out)]


def optimize_cmd(llvm_bolt: Path, lib: Path, fdata: Path, out: Path) -> list[str]:
    """``llvm-bolt`` rewriting ``lib`` into ``out`` with the merged profile."""
    return [str(llvm_bolt), str(lib), "-data", str(fdata), "-o", str(out), *_BOLT_OPT_FLAGS]


def run_step(argv: list[str], *, what: str, env: dict | None = None,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a BOLT-cycle subprocess, raising :class:`BoltError` on failure."""
    _log.info(f"{what}: {' '.join(argv)}")
    result = run.probe(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise BoltError(
            f"{what} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[-400:]}"
        )
    return result


# ---------------------------------------------------------------------------
# Training workload
# ---------------------------------------------------------------------------

def training_commands(
    build: Path, clangxx: Path, out_dir: Path, *, target: str, limit: int,
) -> list[list[str]]:
    """Compile commands from the tree's own build, replayed with the new clang.

    ``ninja -t commands <target>`` lists every command that builds ``target``.
    The C/C++ compiles among them are rewritten to run ``clangxx``, with any
    launcher dropped, the object written to ``out_dir``, and dependency-file
    flags removed so nothing lands in the build tree. clang's own front-end
    sources (``/clang/lib/``) come first: they are the template-heavy C++ that
    dominates real compile time, and the code ``libclang-cpp`` exists to run.
    An even sample of at most ``limit`` commands, run with ``cwd=build``.
    """
    result = run.probe(["ninja", "-C", str(build), "-t", "commands", target],
                       stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        return []
    compiles: list[list[str]] = []
    for line in result.stdout.splitlines():
        try:
            argv = shlex.split(line)
        except ValueError:
            continue
        while argv and Path(argv[0]).name in _LAUNCHERS:
            argv = argv[1:]
        if not argv or "-c" not in argv or "-o" not in argv:
            continue
        if not argv[-1].endswith((".cpp", ".cc", ".c")):
            continue
        compiles.append(argv)
    compiles.sort(key=lambda a: "/clang/lib/" not in a[-1])
    if len(compiles) > limit:
        step = len(compiles) / limit
        compiles = [compiles[int(i * step)] for i in range(limit)]
    out: list[list[str]] = []
    for n, argv in enumerate(compiles):
        rewritten = [str(clangxx)]
        args = iter(argv[1:])
        for a in args:
            if a in _DEPFILE_FLAG:
                continue
            if a in _DEPFILE_ARG:
                next(args, None)
                continue
            if a == "-o":
                next(args, None)
                rewritten += ["-o", str(out_dir / f"{n}.o")]
                continue
            rewritten.append(a)
        out.append(rewritten)
    return out


# A small but front-end-/optimizer-heavy C++ translation unit: the fallback
# training input when the tree yields no compile commands and no
# `[bolt] training_workload` is set. Kept distinct from the bench's built-in
# workload, which must stay a held-out measurement.
_DEFAULT_WORKLOAD_TU = """\
#include <algorithm>
#include <functional>
#include <map>
#include <memory>
#include <numeric>
#include <string>
#include <unordered_map>
#include <vector>

template <typename T>
struct Node { T value; std::unique_ptr<Node<T>> next; };

template <typename T>
T sum_list(const Node<T>* n) {
    T acc{};
    for (; n; n = n->next.get()) acc += n->value;
    return acc;
}

template <typename K, typename V>
std::map<K, V> invert(const std::unordered_map<V, K>& in) {
    std::map<K, V> out;
    for (const auto& [k, v] : in) out.emplace(v, k);
    return out;
}

int main() {
    std::vector<int> v(1024);
    std::iota(v.begin(), v.end(), 0);
    std::sort(v.begin(), v.end(), std::greater<int>{});
    std::unordered_map<std::string, int> m;
    for (int x : v) m[std::to_string(x)] = x;
    auto inv = invert<int, std::string>(m);
    return static_cast<int>(inv.size() + std::accumulate(v.begin(), v.end(), 0));
}
"""


def write_default_workload(dest_dir: Path) -> Path:
    """Write the fallback training translation unit into ``dest_dir``."""
    src = dest_dir / "bolt_workload.cpp"
    src.write_text(_DEFAULT_WORKLOAD_TU, encoding="utf-8")
    return src


def compile_workload_argv(clang_cxx: Path, source: Path, out_obj: Path) -> list[str]:
    """One optimizing compile of ``source`` (a configured or fallback workload)."""
    return [str(clang_cxx), "-std=c++17", "-O2", "-c", str(source), "-o", str(out_obj)]


# ---------------------------------------------------------------------------
# Putting the result in place
# ---------------------------------------------------------------------------

def replace_keep_mtime(new: Path, dest: Path) -> None:
    """Copy ``new`` over ``dest`` and give it ``dest``'s old mtime.

    ninja records each output's mtime in ``.ninja_log`` and treats a changed one
    as dirty, so ``package()``'s ``ninja install-…`` would relink the library
    and quietly put the un-BOLTed one back. Keeping the mtime leaves the output
    clean and the rewritten file in place.
    """
    st = dest.stat()
    shutil.copy2(new, dest)
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
