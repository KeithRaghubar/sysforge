# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_diag.py — postflight failure-log diagnostics.

On a non-zero ``makepkg`` exit, scan the captured output + any side-car
build logs (meson, cargo) under the build directory for known failure
signatures and surface an actionable fix block alongside the existing
``[build_failed]`` banner.

This is the long-tail companion to
:mod:`sysforge.primitives.toolchain_preflight`: preflight catches the cases
sysforge can predict from makedepends + pkgname; postflight catches the
ones it can't (e.g. a vendored subproject pulling in rust at meson-time
without listing rust in the parent PKGBUILD's makedepends, which is exactly
how ``lib32-gstreamer`` slips past inference today).

Public API:
    diagnose(captured_lines, build_dir, *, active_rust_toolchain=None)
        -> list[FixSuggestion]
    render_suggestions(suggestions) -> str

The matcher set is deliberately small and conservative — a false positive
that contradicts the real root cause is worse than no hint at all.
"""
from __future__ import annotations

import glob
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_TAG = "DIAG"

# Cap how many bytes of any side-car log we slurp. The error of interest is
# always near the bottom; reading 64 KiB from the tail is more than enough
# for every signature in the table without making this loop expensive when
# pointed at a 200 MB build.log.
_LOG_TAIL_BYTES = 64 * 1024
_MESON_LOG_GLOBS = ("build*/meson-logs/meson-log.txt", "meson-logs/meson-log.txt")
# CMake records the compiler-detection failure (e.g. a clang that can't run) in
# these under the build directory. Same shape as the meson globs so the
# side-car collector can pick them up for interactive failures (where makepkg's
# stdout was not captured).
_CMAKE_LOG_GLOBS = (
    "build*/CMakeFiles/CMakeError.log", "CMakeFiles/CMakeError.log",
    "build*/CMakeFiles/CMakeOutput.log", "CMakeFiles/CMakeOutput.log",
)
_SIDECAR_LOG_GLOBS = _MESON_LOG_GLOBS + _CMAKE_LOG_GLOBS


@dataclass(frozen=True)
class FixSuggestion:
    signature: str       # short matcher id, e.g. "rust:E0463"
    message: str         # one-line human description of what went wrong
    fix_cmd: str | None  # exact remediation, when there is one


# ---------------------------------------------------------------------------
# Side-car log collection
# ---------------------------------------------------------------------------

def _read_tail(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if st.st_size > _LOG_TAIL_BYTES:
                f.seek(-_LOG_TAIL_BYTES, 2)
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _collect_text(captured_lines: list[str], build_dir: Path | None) -> str:
    """Concatenate the captured stdout/stderr with side-car log tails."""
    chunks: list[str] = []
    if captured_lines:
        chunks.append("\n".join(captured_lines))
    if build_dir is not None:
        for sub in build_dir.iterdir() if build_dir.is_dir() else ():
            if not sub.is_dir():
                continue
            for pat in _SIDECAR_LOG_GLOBS:
                for hit in sub.glob(pat):
                    text = _read_tail(hit)
                    if text:
                        chunks.append(f"# {hit}\n{text}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Signature matchers
# ---------------------------------------------------------------------------

_RE_E0463 = re.compile(
    r"error\[E0463\]: can't find crate for [`']std[`']"
)
_RE_E0463_TARGET = re.compile(
    r"the [`']([^`']+)[`'] target may not be installed"
)
_RE_PTP = re.compile(r"PTP not supported without Rust compiler")
_RE_MESON_UNKNOWN_OPT = re.compile(
    r"meson\.build:\d+:\d+: ERROR: Unknown options:"
)


def _suggest_rustup_target(target: str, active: str | None) -> str:
    if active:
        return f"rustup target add --toolchain {active} {target}"
    return f"rustup target add {target}"


def _match_rust_missing_std(text: str, active: str | None) -> FixSuggestion | None:
    if not _RE_E0463.search(text):
        return None
    m = _RE_E0463_TARGET.search(text)
    target = m.group(1) if m else None
    if target:
        return FixSuggestion(
            signature="rust:E0463",
            message=(
                f"rust std crate missing for target {target} — "
                "the active rust toolchain has no std for this target"
            ),
            fix_cmd=_suggest_rustup_target(target, active),
        )
    return FixSuggestion(
        signature="rust:E0463",
        message="rust std crate missing — the active rust toolchain can't find std",
        fix_cmd="rustup target list --installed   # check which targets the active toolchain has",
    )


def _match_gst_ptp(text: str, active: str | None) -> FixSuggestion | None:
    if not _RE_PTP.search(text):
        return None
    # The PTP error is downstream of either a missing rustc or a missing
    # cross target. If we also see E0463, the rust-missing-std matcher will
    # have already produced the precise fix — suppress this one to avoid
    # duplicate noise.
    if _RE_E0463.search(text):
        return None
    return FixSuggestion(
        signature="gst:ptp-no-rust",
        message=(
            "gstreamer's PTP helper requires a rust compiler for the host arch; "
            "for lib32-* this is the i686-unknown-linux-gnu target"
        ),
        fix_cmd=_suggest_rustup_target("i686-unknown-linux-gnu", active),
    )


def _match_meson_unknown_opts(text: str, active: str | None) -> FixSuggestion | None:
    del active
    if not _RE_MESON_UNKNOWN_OPT.search(text):
        return None
    return FixSuggestion(
        signature="meson:unknown-options",
        message=(
            "meson rejected an option that used to exist — likely a stale "
            "build/ directory from a previous version of the project"
        ),
        fix_cmd="rm -rf src/build  # then re-run the build",
    )


# ---------------------------------------------------------------------------
# CUDA host-compiler too new (nvcc rejects the system gcc)
# ---------------------------------------------------------------------------

# Canonical message emitted by CUDA's crt/host_config.h gate when the host gcc
# is newer than the toolkit supports.
_RE_CUDA_UNSUPPORTED_GCC = re.compile(
    r"unsupported GNU version|UNSUPPORTED COMPILER", re.IGNORECASE
)
# Fallback signal: nvcc's frontend chokes on a too-new libstdc++ before the
# clean gate fires — a `.cu` compilation summary plus libstdc++ header errors.
_RE_CU_COMPILE_FAIL = re.compile(r'errors detected in the compilation of "[^"]*\.cu"')
_RE_LIBSTDCXX_PATH = re.compile(r"/usr/include/c\+\+/(\d+)(?:\.\d+)*/")

_CUDA_HOST_CONFIG_PATHS = (
    "/opt/cuda/include/crt/host_config.h",
    "/usr/local/cuda/include/crt/host_config.h",
)


def _cuda_max_gcc_major() -> int | None:
    """Read CUDA's ``crt/host_config.h`` ``#if __GNUC__ > N`` gate → N (the
    highest supported gcc major), or None if it can't be determined."""
    paths: list[str] = []
    nvcc = shutil.which("nvcc")
    if nvcc:
        # .../bin/nvcc → .../include/crt/host_config.h
        paths.append(
            str(Path(nvcc).resolve().parent.parent / "include" / "crt" / "host_config.h")
        )
    paths.extend(_CUDA_HOST_CONFIG_PATHS)
    for p in paths:
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"__GNUC__\s*>\s*(\d+)", txt)
        if m:
            return int(m.group(1))
    return None


def _highest_gpp_upto(major: int) -> str | None:
    """Return the path to the highest installed ``/usr/bin/g++-N`` with
    ``N <= major``, or None when none is installed."""
    best: tuple[int, str] | None = None
    for path in glob.glob("/usr/bin/g++-*"):
        suffix = path.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            continue
        n = int(suffix)
        if n <= major and (best is None or n > best[0]):
            best = (n, path)
    return best[1] if best else None


def _match_cuda_host_gcc(text: str, active: str | None) -> FixSuggestion | None:
    del active
    canonical = _RE_CUDA_UNSUPPORTED_GCC.search(text)
    cu_libstdcxx = _RE_CU_COMPILE_FAIL.search(text) and _RE_LIBSTDCXX_PATH.search(text)
    if not (canonical or cu_libstdcxx):
        return None
    # Best-effort fix derivation; never let a probe failure mask the diagnosis.
    try:
        max_gcc = _cuda_max_gcc_major()
    except Exception:
        max_gcc = None
    m = _RE_LIBSTDCXX_PATH.search(text)
    sys_major = int(m.group(1)) if m else None
    ccbin = None
    if max_gcc is not None:
        try:
            ccbin = _highest_gpp_upto(max_gcc)
        except Exception:
            ccbin = None

    if max_gcc is not None and sys_major is not None:
        message = (
            f"nvcc rejected the system host compiler: this CUDA toolkit supports "
            f"gcc <= {max_gcc}, but the system gcc is {sys_major}"
        )
    else:
        message = (
            "nvcc rejected the system host compiler — the system gcc is newer "
            "than this CUDA toolkit supports"
        )
    if ccbin:
        fix_cmd = (
            f"NVCC_APPEND_FLAGS='-ccbin {ccbin}' makepkg   "
            "# point nvcc at a supported host gcc"
        )
    elif max_gcc is not None:
        fix_cmd = (
            f"install gcc{max_gcc} (or older), then build with "
            f"NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-{max_gcc}'"
        )
    else:
        fix_cmd = (
            "build with NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-<N>' "
            "for a CUDA-supported gcc <N>"
        )
    return FixSuggestion(
        signature="cuda:host-gcc-too-new", message=message, fix_cmd=fix_cmd
    )


# ---------------------------------------------------------------------------
# Broken / mismatched LLVM toolchain (clang cannot run)
# ---------------------------------------------------------------------------

# A clang/libLLVM ABI mismatch (e.g. clang built against a libLLVM that no
# longer exports a symbol it needs, or a clang↔llvm-libs version skew) makes
# clang fail to even start. Surfaces as a dynamic-link symbol error and, via
# meson, as "Unknown compiler(s): [['clang']]".
_RE_LLVM_UNDEF_SYMBOL = re.compile(r"undefined symbol: LLVMInitialize\w+")
_RE_CLANG_SYMBOL_LOOKUP = re.compile(r"symbol lookup error: \S*clang")
_RE_MESON_UNKNOWN_CLANG = re.compile(r"Unknown compiler\(s\): \[\['clang'?'?\]\]")


def _match_broken_llvm_toolchain(text: str, active: str | None) -> FixSuggestion | None:
    del active
    if not (
        _RE_LLVM_UNDEF_SYMBOL.search(text)
        or _RE_CLANG_SYMBOL_LOOKUP.search(text)
        or _RE_MESON_UNKNOWN_CLANG.search(text)
    ):
        return None
    return FixSuggestion(
        signature="toolchain:llvm-broken",
        message=(
            "the installed clang/libLLVM are mismatched — clang cannot run "
            "(likely a clang↔llvm-libs version skew or a half-installed "
            "toolchain upgrade)"
        ),
        fix_cmd=(
            "reinstall a consistent toolchain: sudo pacman -Syu clang llvm "
            "llvm-libs lld   # or rebuild via `sysforge run toolchain`"
        ),
    )


_MATCHERS = (
    _match_rust_missing_std,
    _match_gst_ptp,
    _match_meson_unknown_opts,
    _match_cuda_host_gcc,
    _match_broken_llvm_toolchain,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diagnose(
    captured_lines: list[str],
    build_dir: Path | None,
    *,
    active_rust_toolchain: str | None = None,
) -> list[FixSuggestion]:
    """Scan captured output + side-car logs; return any matched suggestions.

    Order is matcher-table order, deduped on ``signature`` so the same
    failure pattern never appears twice (e.g. when both stdout and the
    meson log contain the same E0463 block).
    """
    text = _collect_text(captured_lines, build_dir)
    if not text:
        return []
    out: list[FixSuggestion] = []
    seen: set[str] = set()
    for matcher in _MATCHERS:
        s = matcher(text, active_rust_toolchain)
        if s is None or s.signature in seen:
            continue
        seen.add(s.signature)
        out.append(s)
    return out


def render_suggestions(suggestions: list[FixSuggestion]) -> str:
    """Render the suggestions as a fix block, or '' when empty."""
    if not suggestions:
        return ""
    header = f"  [{_TAG}]" + " " * max(1, 17 - len(_TAG) - 2)
    lines = [f"{header}possible fixes:"]
    for s in suggestions:
        lines.append(f"    - {s.message}")
        if s.fix_cmd:
            lines.append(f"      $ {s.fix_cmd}")
    return "\n".join(lines)
