# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
bolt.py — BOLT post-link optimization orchestration (toolchain Pass 4)

The one home for sysforge's BOLT (Binary Optimization and Layout Tool) support —
the fourth optimization method on the shared profile-store rails, and the only
one that is **post-link** rather than compile- or sample-driven.

PGO, mesa PGO and kernel AutoFDO are all *flag*-driven: you inject a compiler
flag (`-fprofile-use`, `-fprofile-sample-use`) and the compiler does the rest.
BOLT is different — there is no "BOLT flag". You link the binary with
`-Wl,--emit-relocs` to retain relocations, then run a *separate tool*
(`llvm-bolt`) that rewrites the finished binary using a profile that `perf2bolt`
converts from a `perf record` run. The canonical use is the **PGO→BOLT** "fast
clang" stack: BOLT the already-PGO-optimized clang/libLLVM, where it delivers the
most. That is why sysforge wires BOLT as a Pass 4 in the toolchain stage, after
the PGO passes have produced an optimized toolchain.

Flow (orchestrated by the toolchain stage; this module is the facts + the
subprocess seams):

  1. The PGO Pass 3 links with :func:`emit_relocs_ldflag` so the shipped
     binaries are BOLT-able.
  2. :func:`collect_profile` profiles the freshly-built clang on a representative
     compile workload (`perf record` → `perf2bolt`) into the ``bolt`` store.
  3. :func:`bolt_binary` runs ``llvm-bolt`` to rewrite the binary with the
     collected profile.

LLVM-only — `llvm-bolt`/`perf2bolt` ship with LLVM, and the whole feature is
gated on the LLVM toolchain upstream. Pure except for the subprocess seams
(:func:`collect_profile`, :func:`bolt_binary`, :func:`tools_available`).
"""
import shutil
import subprocess
import tomllib
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_pgo import resolve_method_store
from sysforge.primitives.paths import TOOLCHAIN_PATH

_log = log.get_logger("BOLT")

# The optimization build_mode this flow records (see profile.is_optimized_build_mode
# / profile.rename_mode_for_build_mode → "conflict"). The toolchain stage stamps it
# for provenance on the BOLT-optimized members.
BUILD_MODE = "bolt_llvm"

# Linker flag that retains relocations in the final binary — BOLT needs them to
# safely rewrite. Injected into the PGO Pass-3 link (LDFLAGS) when BOLT is enabled.
EMIT_RELOCS_LDFLAG = "-Wl,--emit-relocs"

# Artifact names inside the per-method store.
FDATA_NAME = "llvm.fdata"       # perf2bolt-converted BOLT profile
PERF_DATA_NAME = "perf.data"    # raw perf sample buffer

# External tools the cycle needs. perf collects, perf2bolt converts, llvm-bolt
# rewrites. All optional at import — checked at run via tools_available().
PERF_TOOL = "perf"
PERF2BOLT_TOOL = "perf2bolt"
LLVM_BOLT_TOOL = "llvm-bolt"

# Standard llvm-bolt optimization set for a PGO'd clang/libLLVM. ext-tsp block
# layout + hfsort+ function reordering + function/cold/eh splitting are the
# canonical "fast clang" options from the BOLT docs; -icf=1 folds identical code.
_BOLT_OPT_FLAGS = (
    "-reorder-blocks=ext-tsp",
    "-reorder-functions=hfsort+",
    "-split-functions",
    "-split-all-cold",
    "-split-eh",
    "-icf=1",
    "-dyno-stats",
)

# perf event for BOLT sampling. cycles with a branch stack (LBR on Intel, BRS on
# AMD Zen3+) gives perf2bolt the taken-branch data it wants; userspace-only (:u)
# since we profile clang, not the kernel.
_PERF_EVENT = ("-e", "cycles:u", "-j", "any,u")


class BoltError(Exception):
    """A BOLT step could not complete (missing tool, perf/perf2bolt/llvm-bolt
    failure). Raised so the caller aborts the BOLT pass cleanly — the underlying
    PGO toolchain stays installed — rather than shipping a half-rewritten binary."""


def _load_tcfg() -> dict | None:
    """Best-effort load of toolchain.toml for store-path overrides (pure)."""
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with open(TOOLCHAIN_PATH, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def resolve_store(tcfg: dict | None = None) -> Path:
    """Resolve BOLT's profile store dir (``<profile_store_root>/bolt``)."""
    return resolve_method_store(tcfg if tcfg is not None else _load_tcfg(), "bolt")


def fdata_path(store: Path) -> Path:
    """The perf2bolt-converted BOLT profile inside the store."""
    return store / FDATA_NAME


def perf_data_path(store: Path) -> Path:
    """The raw ``perf.data`` the profiling step writes."""
    return store / PERF_DATA_NAME


def emit_relocs_ldflag() -> str:
    """The linker flag that keeps the PGO build BOLT-able (retains relocations)."""
    return EMIT_RELOCS_LDFLAG


def tools_available(*, need_perf: bool = True) -> tuple[bool, list[str]]:
    """Return ``(ok, missing)`` for the BOLT toolchain.

    ``llvm-bolt`` + ``perf2bolt`` are always required; ``perf`` is required for
    the collection step (``need_perf``) but not when consuming a pre-collected
    profile. Lets the caller skip the BOLT pass with an actionable warning rather
    than crashing mid-build.
    """
    required = [LLVM_BOLT_TOOL, PERF2BOLT_TOOL]
    if need_perf:
        required.append(PERF_TOOL)
    missing = [t for t in required if shutil.which(t) is None]
    return (not missing, missing)


def perf_record_cmd(perf_data: Path, workload_argv: list[str]) -> list[str]:
    """The ``perf record`` command line that samples ``workload_argv``."""
    return [
        PERF_TOOL, "record", *_PERF_EVENT, "-o", str(perf_data), "--", *workload_argv,
    ]


def perf2bolt_cmd(binary: Path, perf_data: Path, fdata: Path) -> list[str]:
    """The ``perf2bolt`` command that converts ``perf.data`` → BOLT ``.fdata``."""
    return [
        PERF2BOLT_TOOL, str(binary), "-p", str(perf_data), "-o", str(fdata),
    ]


def llvm_bolt_cmd(binary: Path, fdata: Path, out: Path) -> list[str]:
    """The ``llvm-bolt`` command that rewrites ``binary`` → ``out`` using ``fdata``."""
    return [
        LLVM_BOLT_TOOL, str(binary), "-data", str(fdata), "-o", str(out),
        *_BOLT_OPT_FLAGS,
    ]


def _run(argv: list[str], *, what: str) -> subprocess.CompletedProcess:
    """Run a BOLT-cycle subprocess, raising :class:`BoltError` on failure."""
    _log.info(f"{what}: {' '.join(argv)}")
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise BoltError(
            f"{what} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def collect_profile(
    binary: Path, store: Path, workload_argv: list[str]
) -> Path:
    """Profile ``binary`` running ``workload_argv`` and convert to a BOLT ``.fdata``.

    Runs ``perf record`` over the workload, then ``perf2bolt`` against ``binary``.
    Returns the ``.fdata`` path. Raises :class:`BoltError` if a tool is missing or
    fails (e.g. ``perf_event_paranoid`` too restrictive). The store dir is
    provisioned by the caller.
    """
    ok, missing = tools_available(need_perf=True)
    if not ok:
        raise BoltError(
            f"BOLT collection needs {', '.join(missing)} on PATH "
            "(llvm-bolt/perf2bolt ship with llvm; perf is the linux-tools package)."
        )
    perf_data = perf_data_path(store)
    fdata = fdata_path(store)
    _run(perf_record_cmd(perf_data, workload_argv), what="perf record")
    _run(perf2bolt_cmd(binary, perf_data, fdata), what="perf2bolt")
    if not fdata.is_file():
        raise BoltError(f"perf2bolt produced no profile at {fdata}")
    _log.info(f"Collected BOLT profile: {fdata} ({fdata.stat().st_size} bytes)")
    return fdata


def bolt_binary(binary: Path, fdata: Path, *, out: Path | None = None) -> Path:
    """Rewrite ``binary`` with ``llvm-bolt`` using ``fdata``; return the output path.

    ``out`` defaults to ``<binary>.bolt``. Does not move the result into place —
    the caller decides whether to atomically replace ``binary`` (and is
    responsible for the smoke-test + rollback that guards a system compiler).
    Raises :class:`BoltError` on a missing tool or a non-zero ``llvm-bolt``.
    """
    ok, missing = tools_available(need_perf=False)
    if not ok:
        raise BoltError(f"BOLT optimization needs {', '.join(missing)} on PATH.")
    if not fdata.is_file():
        raise BoltError(
            f"no BOLT profile at {fdata} — run the collection step first."
        )
    out = out if out is not None else binary.with_suffix(binary.suffix + ".bolt")
    _run(llvm_bolt_cmd(binary, fdata, out), what="llvm-bolt")
    if not out.is_file():
        raise BoltError(f"llvm-bolt produced no output at {out}")
    _log.info(f"BOLT-optimized {binary.name} → {out}")
    return out


# A small but front-end-/optimizer-heavy C++ translation unit. Compiling it with
# -O2 exercises clang's parser, template instantiation, the middle-end and
# codegen — a representative "compile job" profile for BOLT without needing a
# multi-minute build of real source. Used when [bolt] training_workload is unset.
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
    """Write the default profiling translation unit into ``dest_dir`` and return it."""
    src = dest_dir / "bolt_workload.cpp"
    src.write_text(_DEFAULT_WORKLOAD_TU, encoding="utf-8")
    return src


def compile_workload_argv(clang_cxx: str, source: Path, out_obj: Path) -> list[str]:
    """The clang++ invocation that BOLT profiles (compile a TU at ``-O2``).

    This is the ``workload_argv`` handed to :func:`perf_record_cmd`: a single
    optimizing compile of ``source`` to an object file, exercising the whole
    front-end → middle-end → codegen pipeline of the binary being profiled.
    """
    return [
        clang_cxx, "-std=c++17", "-O2", "-c", str(source), "-o", str(out_obj),
    ]


# ---------------------------------------------------------------------------
# llvm-bolt package — sysforge builds the BOLT tools itself
#
# BOLT (llvm-bolt/perf2bolt/merge-fdata) is NOT in the official Arch repos and is
# NOT built by the stock `llvm` package. But the `bolt/` subtree ships *inside*
# the `llvm-project-$pkgver.src.tar.xz` monorepo tarball the toolchain's `llvm`
# PKGBUILD already downloads, and BOLT supports a standalone build
# (`bolt/CMakeLists.txt` sets `BOLT_BUILT_STANDALONE` and runs `find_package(LLVM)`)
# — exactly like Arch builds `clang`/`lld` as separate packages from the same
# tarball. So sysforge generates an `llvm-bolt` PKGBUILD modeled on those
# components and builds it against the just-installed PGO libLLVM. This is the one
# home for that PKGBUILD; it is version-locked to the LLVM it links (BOLT uses
# libLLVM internals — a mismatch won't even configure). EXPERIMENTAL: there is no
# official Arch package, so this is a sysforge-provided build.
# ---------------------------------------------------------------------------

PKG_NAME = "llvm-bolt"

# `sha256sums=SKIP`: the tarball is byte-identical to (and shares makepkg's
# SRCDEST cache with) the one the `llvm` build PGP-verified moments earlier, so we
# don't re-pin a per-version checksum. {pkgver} is substituted by
# materialize_pkgbuild from the installed llvm version.
_PKGBUILD_TEMPLATE = """\
# Generated by SysForge — BOLT post-link optimizer (experimental).
# No official Arch package exists; built from the llvm-project monorepo tarball,
# version-locked to the installed llvm. Do not edit — regenerated each run.
pkgname=llvm-bolt
pkgver={pkgver}
pkgrel=1
pkgdesc="LLVM BOLT post-link optimizer (sysforge-built; experimental)"
arch=('x86_64')
url="https://github.com/llvm/llvm-project/tree/main/bolt"
license=('Apache-2.0 WITH LLVM-exception')
depends=('llvm-libs')
makedepends=("llvm=$pkgver" 'cmake' 'ninja' 'python')
options=(!lto)
_source_base=https://github.com/llvm/llvm-project/releases/download/llvmorg-$pkgver
source=("$_source_base/llvm-project-$pkgver.src.tar.xz")
sha256sums=('SKIP')

build() {{
  cd "llvm-project-$pkgver.src/bolt"
  cmake -B build -G Ninja \\
    -DCMAKE_BUILD_TYPE=Release \\
    -DCMAKE_INSTALL_PREFIX=/usr \\
    -DCMAKE_SKIP_RPATH=ON \\
    -DLLVM_LINK_LLVM_DYLIB=ON \\
    -DLLVM_ENABLE_RTTI=ON
  ninja -C build
}}

package() {{
  cd "llvm-project-$pkgver.src/bolt"
  DESTDIR="$pkgdir" ninja -C build install
  install -Dm644 ../llvm/LICENSE.TXT \\
    "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}}
"""


def render_pkgbuild(llvm_pkgver: str) -> str:
    """Render the ``llvm-bolt`` PKGBUILD text version-locked to ``llvm_pkgver``."""
    return _PKGBUILD_TEMPLATE.format(pkgver=llvm_pkgver)


def materialize_pkgbuild(pkgbuild_src_dir: Path, llvm_pkgver: str) -> Path:
    """Write the version-locked ``llvm-bolt`` PKGBUILD into the source tree.

    Returns the PKGBUILD path (``<pkgbuild_src_dir>/llvm-bolt/PKGBUILD``). The
    dir is created if absent; the file is overwritten each run so the pkgver
    always tracks the installed llvm. Caller builds it via the toolchain stage's
    normal build path.
    """
    dest_dir = Path(pkgbuild_src_dir).expanduser() / PKG_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    pkgbuild = dest_dir / "PKGBUILD"
    pkgbuild.write_text(render_pkgbuild(llvm_pkgver), encoding="utf-8")
    return pkgbuild
