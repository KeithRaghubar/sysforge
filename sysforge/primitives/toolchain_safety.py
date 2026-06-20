# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain_safety.py — guardrails so the LLVM toolchain stage can never leave
the live ``/usr`` compiler broken.

This is the primitive behind the toolchain stage's three safety gates and the
shared facts both health-check entry points draw from. Everything here is
read-only and pure enough to unit-test against fixture trees; the toolchain
stage owns the *policy* (what aborts vs warns), this module owns the *facts*.

It is **not** a third health-check entry point — ``_verify_llvm_install``
(pipeline post-install) and ``toolchain_preflight._probe_cc`` (primitives,
update path) remain the only two, and both call into these functions. This
module imports :data:`LLVM_LOCKSTEP_SUITE` from ``toolchain_preflight`` (both
primitives, no layering issue) so the lockstep set has one source of truth.

Capabilities:
  - detect_suite_skew              — installed-suite pkgver disagreement
  - check_link_resolution          — clang/lld resolve libLLVM under /usr/lib
  - smoke_test_compilers           — clang/lld present + actually run
  - scan_abi_hazards               — _ZNSt*@LLVM_* C++-stdlib-in-LLVM-ns symbols
  - check_system_consumer_symbols  — built libLLVM drops a target an installed
                                     mesa/graphics consumer imports (pre-install)
  - check_installed_consumer_symbols— same, vs the now-installed libLLVM (post-install)
  - detect_residual_instrumentation— stale -fprofile-generate LLVM libs (advisory)
  - check_pkgver_lockstep          — PKGBUILD pkgver skew across lockstep members
  - assess_libllvm_soname_impact   — impending libLLVM soname bump + broken consumers
  - check_build_space              — staging/pgo_store/builddir filesystems have headroom
  - check_multilib_enabled         — [multilib] present when a lib32-* is in scope

Severity model: each ToolchainFinding carries ``is_brick`` — True means "the
live toolchain is/would-be broken, or the build will definitely fail". The
toolchain stage hard-fails on brick findings (some overridable via flags) and
warns on the rest.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"

# Filesystem roots / binaries — module-level so tests can repoint them at a
# fixture tree (same idiom as kernel_safety).
_USR_LIB = Path("/usr/lib")
_USR_BIN_CLANG = Path("/usr/bin/clang")
_PACMAN_CONF = Path("/etc/pacman.conf")
_LIBLLVM_SUPPORT_A = Path("/usr/lib/libLLVMSupport.a")

# A staging prefix appearing in an installed binary's ldd output is the
# Pass-3-bad-RPATH hazard: the live toolchain about to load a libLLVM from
# /var/tmp that is about to be wiped.
_STAGING_PREFIX_MARKER = "/var/tmp/sysforge-llvm-stage"


@dataclass(frozen=True)
class ToolchainFinding:
    severity: str          # SEV_ERROR | SEV_WARN | SEV_INFO
    check_id: str          # short stable id, e.g. "suite_skew"
    message: str
    remediation: str = ""
    is_brick: bool = False  # True → live toolchain broken / build will fail
    healable: bool = False  # True → fixable by rebuilding the affected consumer(s)
                            #        (same-soname std:: re-export drift), not a hard block


@dataclass(frozen=True)
class SonameImpact:
    """Facts about an impending libLLVM soname change and who it would break.

    ``old_soname`` / ``new_soname`` are the bare versioned filenames
    (``libLLVM.so.22.1`` → ``libLLVM.so.23.0``); ``consumers`` is the sorted,
    deduped list of installed pkgbases that link the *old* soname and would be
    left with a dangling ``NEEDED`` until rebuilt. Only ever constructed when
    the soname actually changes AND at least one consumer remains after
    excluding the LLVM suite + the packages being built this run.
    """
    old_soname: str
    new_soname: str
    consumers: list[str]


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    """Run a command, returning None when the binary is absent.

    Guards every external command so a missing tool degrades to "no finding"
    rather than raising — the same contract the doctor probes rely on.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, **kwargs
        )
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Installed-suite version skew (post-install / verify facet)
# ---------------------------------------------------------------------------

def detect_suite_skew(
    installed_versions: Mapping[str, str | None],
) -> ToolchainFinding | None:
    """Brick finding when installed LLVM-suite members disagree on version.

    ``installed_versions`` maps suite member → ``pkgver-pkgrel`` (None = not
    installed), typically the result of one ``pacman -Q``. A disagreement is
    the canonical interrupted-install symptom (one pass of a ``pacman -U``
    landed while the rest stayed behind). Compares the full version string so a
    pkgrel-only bump still surfaces here — the install verifier wants exact
    lockstep, unlike the looser preflight skew probe. Returns None when fewer
    than two members are installed or all agree.
    """
    installed = {n: v for n, v in installed_versions.items() if v is not None}
    if len(set(installed.values())) <= 1:
        return None
    detail = ", ".join(f"{n}={v}" for n, v in sorted(installed.items()))
    return ToolchainFinding(
        SEV_ERROR, "suite_skew",
        "LLVM component versions disagree (canonical interrupted-install "
        f"symptom): {detail}",
        "Restore a consistent set: "
        "sudo pacman -S " + " ".join(sorted(installed)),
        is_brick=True,
    )


# ---------------------------------------------------------------------------
# Link resolution — installed clang/lld must load libLLVM from /usr/lib
# ---------------------------------------------------------------------------

def check_link_resolution() -> list[ToolchainFinding]:
    """Verify installed LLVM binaries resolve libLLVM only under /usr/lib.

    A staging prefix (``/var/tmp/sysforge-llvm-stage*``) in an installed
    binary's ``ldd`` output means Pass 3 packaged a bad RPATH or the install is
    incomplete — the live toolchain is about to lose its libLLVM when /var/tmp
    is cleaned. A resolution outside /usr/lib means a sibling libLLVM is
    shadowing the package-managed one. Both are brick-class. Returns [] when
    clean or when ldd / the binaries are unavailable.
    """
    findings: list[ToolchainFinding] = []
    usr_lib = str(_USR_LIB)
    clang = _USR_BIN_CLANG
    lld = clang.parent / "lld"
    for bin_path, label in ((clang, "clang"), (lld, "lld")):
        if not bin_path.exists():
            continue
        proc = _run(["ldd", str(bin_path)])
        if proc is None:
            findings.append(ToolchainFinding(
                SEV_WARN, "link_resolution:ldd_missing",
                "ldd not available — could not verify libLLVM link resolution",
                is_brick=False,
            ))
            return findings
        if proc.returncode != 0:
            findings.append(ToolchainFinding(
                SEV_WARN, f"link_resolution:{label}",
                f"ldd {bin_path}: exit {proc.returncode}",
                is_brick=False,
            ))
            continue
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if "libLLVM" not in stripped:
                continue
            parts = stripped.split("=>")
            if len(parts) < 2:
                continue
            tail = parts[1].strip().split()
            if not tail or tail[0] in ("not", "(0x0)"):
                continue
            resolved = tail[0]
            if _STAGING_PREFIX_MARKER in resolved:
                findings.append(ToolchainFinding(
                    SEV_ERROR, f"link_resolution:{label}:staging",
                    f"{label} resolves libLLVM from a staging prefix: "
                    f"{resolved} — Pass 3 packaged a bad RPATH or the install "
                    "is incomplete",
                    "Rebuild the toolchain; the live clang/lld must load "
                    "libLLVM from /usr/lib, not /var/tmp.",
                    is_brick=True,
                ))
            elif not resolved.startswith(usr_lib):
                findings.append(ToolchainFinding(
                    SEV_ERROR, f"link_resolution:{label}:shadow",
                    f"{label} resolves libLLVM outside /usr/lib: {resolved} "
                    "— a sibling libLLVM is shadowing the package-managed one",
                    "Remove the shadowing library or fix LD_LIBRARY_PATH so "
                    "the package-managed /usr/lib/libLLVM wins.",
                    is_brick=True,
                ))
    return findings


# ---------------------------------------------------------------------------
# Compiler smoke test — clang/lld present and actually run
# ---------------------------------------------------------------------------

def smoke_test_compilers() -> list[ToolchainFinding]:
    """Confirm clang compiles a trivial program and lld is on PATH.

    Generalises the clang/lld checks formerly buried in the PGO-only
    ``_validate_pgo_environment`` so the non-PGO path gets them too. ``clang
    --version`` short-circuits before loading libclang-cpp.so, so it misses
    symbol-version skew from mixed aborted runs — a real compilation fully
    exercises the dynamic linker and surfaces "symbol lookup error:
    libclang-cpp.so: undefined symbol ..., version LLVM_X.Y". Brick-class on
    failure (the system clang is the Pass-1 bootstrap compiler). Returns [] when
    everything runs.
    """
    findings: list[ToolchainFinding] = []
    clang = _USR_BIN_CLANG

    if not clang.exists():
        return [ToolchainFinding(
            SEV_ERROR, "smoke:clang_missing",
            f"{clang} not found — required as the Pass-1 bootstrap compiler",
            "Install clang before running the LLVM toolchain build: "
            "sudo pacman -S clang",
            is_brick=True,
        )]

    probe = _run(
        [str(clang), "-x", "c", "-", "-o", "/dev/null"],
        input="int main(void){return 0;}\n",
    )
    if probe is None or probe.returncode != 0 or "symbol lookup error" in (probe.stderr or ""):
        detail = ""
        if probe is not None:
            detail = (probe.stderr.strip() or probe.stdout.strip())[:300]
        findings.append(ToolchainFinding(
            SEV_ERROR, "smoke:clang_broken",
            f"{clang} is not functional — likely mismatched packages from a "
            f"prior aborted run:\n  {detail}",
            "Restore a consistent set: "
            "sudo pacman -S " + " ".join(LLVM_LOCKSTEP_SUITE),
            is_brick=True,
        ))

    if not shutil.which("lld"):
        findings.append(ToolchainFinding(
            SEV_ERROR, "smoke:lld_missing",
            "lld not found on PATH — required for all toolchain build passes",
            "Install lld before running the LLVM toolchain build: "
            "sudo pacman -S lld",
            is_brick=True,
        ))
    return findings


# ---------------------------------------------------------------------------
# ABI hazard scan — C++ stdlib symbols bound to the LLVM version namespace
# ---------------------------------------------------------------------------

def scan_abi_hazards(pkg_files: list[Path]) -> list[ToolchainFinding]:
    """Scan built ``.pkg.tar*`` for the std::-bound-to-libLLVM ABI hazard.

    Any UND versioned symbol with a mangled C++ stdlib prefix (``_ZNSt``)
    whose version starts with ``LLVM_`` means the linker bound a std::string
    method (etc.) to libLLVM's version namespace instead of libstdc++'s
    GLIBCXX_*. Installing such a package leaves the live toolchain unable to
    resolve those symbols at runtime (``clang --version`` →
    ``symbol lookup error: libclang-cpp.so: undefined symbol _ZNSt..., version
    LLVM_X.Y``). Each hazard is brick-class. Returns [] when clean.
    """
    from sysforge.primitives.abi_check import (
        _extract_sos,
        _list_sos_in_pkg,
        _undefined_versioned,
    )

    findings: list[ToolchainFinding] = []
    with tempfile.TemporaryDirectory(prefix="sysforge-abi-") as tmpdir:
        tmp = Path(tmpdir)
        for pkg in pkg_files:
            members = _list_sos_in_pkg(pkg)
            if not members:
                continue
            sos = _extract_sos(pkg, members, tmp / pkg.name)
            for so in sos:
                for sym, ver in sorted(_undefined_versioned(so)):
                    if sym.startswith("_ZNSt") and ver.startswith("LLVM_"):
                        findings.append(ToolchainFinding(
                            SEV_ERROR, "abi_hazard",
                            f"{pkg.name}: {so.name}: C++ stdlib symbol bound to "
                            f"LLVM version namespace: {sym}@{ver} "
                            "(should be GLIBCXX_*)",
                            "Rebuild with --rebuild-profdata; do not install "
                            "these packages — they would break the live clang.",
                            is_brick=True,
                        ))
    return findings


# ---------------------------------------------------------------------------
# System graphics-consumer symbol sufficiency — a rebuilt libLLVM that dropped
# an LLVM backend an installed consumer imports would brick the desktop
# ---------------------------------------------------------------------------

# Installed external libLLVM consumers (relative to _USR_LIB) whose dynamic
# symbols must stay satisfiable across a system-libLLVM swap. mesa's libgallium
# links the AMDGPU (radeonsi) + host-CPU (llvmpipe) target-init symbols
# UNCONDITIONALLY, so a libLLVM rebuilt with a reduced LLVM_TARGETS_TO_BUILD
# that drops a needed backend leaves these with dangling
# LLVMInitialize*@LLVM_x.y references → mesa EGL fails → black screen. Curated,
# not exhaustive (mirrors hardware._ARCH_OWNED_KCONFIG): the needed-soname
# filter below makes a non-consumer that happens to match a glob a harmless
# skip, so extend freely for new consumers.
_SYSTEM_LIBLLVM_CONSUMER_GLOBS: tuple[str, ...] = (
    "libgallium-*.so",
    "dri/*.so",
    "libvulkan_radeon.so",
    "libvulkan_lvp.so",
)

# LLVMInitialize<Target>{Target,TargetInfo,TargetMC,TargetMCA,AsmPrinter,
# AsmParser,Disassembler} → the backend name, for human-readable diagnostics.
_RE_LLVM_INIT_TARGET = re.compile(
    r"^LLVMInitialize(?P<target>[A-Za-z0-9]+?)"
    r"(Target(Info|MCA|MC)?|AsmPrinter|AsmParser|Disassembler)$"
)


def _llvm_init_target(sym: str) -> str | None:
    """Map an ``LLVMInitialize<Target>*`` symbol to its backend (AMDGPU, X86…)."""
    m = _RE_LLVM_INIT_TARGET.match(sym)
    return m.group("target") if m else None


def _diff_consumers_against_libllvm(
    libllvm_path: Path, *, source_label: str,
) -> list[ToolchainFinding]:
    """Flag installed graphics consumers whose ``LLVM_*``-versioned imports the
    given libLLVM does not export. Shared core of the pre-install (vs the built
    ``.pkg.tar`` member) and post-install (vs the now-installed ``/usr`` lib)
    gates — symbol-version precise via abi_check, no parallel differ.

    Scope: only consumers that actually ``NEED`` this libLLVM's *exact* soname
    are checked (a soname *bump* is owned by ``assess_libllvm_soname_impact`` /
    ``_gate_soname_consumers`` — those consumers are rebuilt, not symbol-diffed).
    Only the consumer's ``LLVM_*`` version-node imports are considered; its libc
    / libstdc++ imports are irrelevant to a libLLVM swap. A non-empty miss is
    brick-class. Fail-open on an unreadable export set (returns []) — the gate
    must not block a build on an ``nm`` hiccup; Part-1 enforcement + the other
    gate arm still protect.
    """
    from sysforge.primitives.abi_check import (
        _exported_versioned,
        _undefined_versioned,
        needed_sonames,
    )

    new_soname = libllvm_path.name  # LLVM sets SONAME == versioned filename
    cache: dict[str, set[tuple[str, str]]] = {}
    exported = _exported_versioned(str(libllvm_path), cache)
    if not exported:
        return []

    findings: list[ToolchainFinding] = []
    for pattern in _SYSTEM_LIBLLVM_CONSUMER_GLOBS:
        for consumer in sorted(_USR_LIB.glob(pattern)):
            if not consumer.is_file() or consumer.is_symlink():
                continue
            if new_soname not in needed_sonames(consumer):
                continue  # doesn't link this libLLVM soname
            missing = sorted(
                (s, v)
                for (s, v) in _undefined_versioned(consumer)
                if v.startswith("LLVM_") and (s, v) not in exported
            )
            if not missing:
                continue
            targets = sorted(
                {t for (s, _) in missing if (t := _llvm_init_target(s))}
            )
            shown = ", ".join(f"{s}@{v}" for s, v in missing[:6])
            more = "" if len(missing) <= 6 else f" (+{len(missing) - 6} more)"
            # Class split. A *target-init* drop (an LLVMInitialize<T>* symbol the
            # new libLLVM no longer provides) is unhealable — the backend is gone,
            # rebuilding the consumer cannot conjure the symbol; it stays a hard
            # block and the AMDGPU baseline is the real fix. A drop of *only*
            # non-target-init LLVM_*-versioned symbols is the same-soname std::
            # re-export drift: the -fprofile-use libLLVM inlined away the weak
            # libstdc++ copies the stock `global: *` script globbed into the
            # LLVM_<ver> node. Rebuilding the consumer re-links those to libstdc++
            # (@GLIBCXX_*), so it is healable via the consumer-rebuild path.
            if targets:
                tgt = f" — dropped LLVM target(s): {', '.join(targets)}"
                findings.append(ToolchainFinding(
                    SEV_ERROR, "libllvm_consumer_symbols",
                    f"{consumer.name} links {new_soname} but the {source_label} "
                    f"libLLVM does not export {len(missing)} symbol(s) it imports"
                    f"{tgt}: {shown}{more}. mesa EGL/GL would fail to load — the "
                    "whole desktop black-screens.",
                    "Rebuild the toolchain with the dropped target(s) kept in "
                    "LLVM_TARGETS_TO_BUILD (sysforge enforces the AMDGPU baseline "
                    "automatically — check toolchain.toml [llvm] targets for an "
                    "explicit override that omits them). To recover a system "
                    "already in this state, reinstall the official llvm-libs.",
                    is_brick=True,
                ))
            else:
                findings.append(ToolchainFinding(
                    SEV_ERROR, "libllvm_consumer_symbols",
                    f"{consumer.name} links {new_soname} but the {source_label} "
                    f"libLLVM no longer re-exports {len(missing)} libstdc++ "
                    f"symbol(s) it imports from the LLVM version namespace: "
                    f"{shown}{more}. The PGO (-fprofile-use) libLLVM inlined away "
                    "the weak std:: copies the stock build globbed into the "
                    "LLVM_<ver> node — rebuilding the consumer re-links them to "
                    "libstdc++. Until then mesa EGL/GL fails to load (desktop "
                    "black-screens).",
                    "Rebuild the affected libLLVM consumer(s) against the new "
                    "libLLVM so they re-link these symbols to libstdc++ "
                    "(@GLIBCXX_*). sysforge does this automatically per "
                    "rebuild_soname_consumers (prompt|auto|off); or rebuild them "
                    "yourself with: sysforge build <consumer pkgbases>.",
                    is_brick=True,
                    healable=True,
                ))
    return findings


def check_system_consumer_symbols(
    built_pkg_files: list[Path],
) -> list[ToolchainFinding]:
    """Pre-install gate: refuse a freshly-built libLLVM that would strand an
    already-installed graphics consumer by dropping target-init symbols.

    Extracts the system ``libLLVM.so.*`` (the ``usr/lib`` ELF — lib32 excluded)
    from ``built_pkg_files`` and diffs it against installed consumers. Returns
    [] when the batch ships no system libLLVM (nothing replacing the live lib).
    """
    from sysforge.primitives.abi_check import _extract_sos, _list_sos_in_pkg

    with tempfile.TemporaryDirectory(prefix="sysforge-consumer-") as tmpdir:
        tmp = Path(tmpdir)
        libllvm_path: Path | None = None
        for pkg in built_pkg_files:
            members = [
                m for m in _list_sos_in_pkg(pkg)
                if m.startswith("usr/lib/")
                and Path(m).name.startswith("libLLVM.so.")
            ]
            for member in members:
                extracted = _extract_sos(pkg, [member], tmp / pkg.name)
                if extracted:
                    libllvm_path = extracted[0]
                    break
            if libllvm_path is not None:
                break
        if libllvm_path is None:
            return []
        return _diff_consumers_against_libllvm(
            libllvm_path, source_label="freshly-built",
        )


def check_installed_consumer_symbols() -> list[ToolchainFinding]:
    """Post-install gate: the system libLLVM is now in place — verify installed
    graphics consumers still resolve against it. Run inside the toolchain
    sentinel so a brick triggers the snapshot auto-restore. Returns [] when no
    versioned ``/usr/lib/libLLVM.so.*`` ELF is present.
    """
    libllvm = next(
        (
            p for p in sorted(_USR_LIB.glob("libLLVM.so.*"))
            if p.is_file() and not p.is_symlink()
        ),
        None,
    )
    if libllvm is None:
        return []
    return _diff_consumers_against_libllvm(libllvm, source_label="installed")


# ---------------------------------------------------------------------------
# Residual instrumentation (advisory) — leftover -fprofile-generate libs
# ---------------------------------------------------------------------------

def _system_llvm_static_is_instrumented() -> bool:
    """True if /usr/lib/libLLVMSupport.a carries PGO instrumentation symbols."""
    if not _LIBLLVM_SUPPORT_A.exists():
        return False
    result = _run(["nm", "--defined-only", str(_LIBLLVM_SUPPORT_A)])
    if result is None or result.returncode != 0:
        return False
    return "__llvm_profile_" in result.stdout


def detect_residual_instrumentation() -> list[ToolchainFinding]:
    """Advisory findings for residual instrumentation from an aborted Pass 1.

    An instrumented ``libLLVM-*.so`` (``__llvm_prf_*`` ELF sections) or
    ``libLLVMSupport.a`` (``__llvm_profile_*`` symbols) left on the system by a
    prior aborted Pass 1 produces profraw noise / requires profile-runtime
    LDFLAGS injection. Neither bricks the toolchain (the build compensates), so
    these are warn-class — the stage surfaces them up front rather than letting
    the user discover them an hour into the build. Returns [] when clean.
    """
    findings: list[ToolchainFinding] = []

    llvm_sos = sorted(_USR_LIB.glob("libLLVM-*.so"))
    if llvm_sos:
        readelf = _run(["readelf", "-S", str(llvm_sos[0])])
        if readelf is not None and readelf.returncode == 0 and "__llvm_prf_" in readelf.stdout:
            findings.append(ToolchainFinding(
                SEV_WARN, "residual:libLLVM_so",
                f"{llvm_sos[0].name} is instrumented (has __llvm_prf_* "
                "sections) — from a prior aborted Pass 1 install",
                "For a clean build: sudo pacman -S llvm llvm-libs",
                is_brick=False,
            ))

    if _system_llvm_static_is_instrumented():
        findings.append(ToolchainFinding(
            SEV_WARN, "residual:libLLVMSupport_a",
            "libLLVMSupport.a is instrumented — the profile runtime will be "
            "injected into LDFLAGS for Pass 2 and Pass 3 automatically",
            "For a clean build: sudo pacman -S llvm",
            is_brick=False,
        ))
    return findings


# ---------------------------------------------------------------------------
# PKGBUILD pkgver lockstep — pre-build
# ---------------------------------------------------------------------------

def check_pkgver_lockstep(
    pkgbuild_pkgvers: dict[str, str],
) -> ToolchainFinding | None:
    """Brick finding when in-tree PKGBUILD pkgvers skew across lockstep members.

    ``pkgbuild_pkgvers`` maps package name → the pkgver parsed from its
    PKGBUILD. Only members of :data:`LLVM_LOCKSTEP_SUITE` are compared —
    ``spirv-llvm-translator`` (its own upstream version scheme) and ``lib32-*``
    (separate multilib lineage, may carry an epoch) are deliberately excluded so
    their legitimately-different versions do not raise a false skew (the bug in
    the old whole-set ``_check_pkgver_consistency``). A skew across the locked
    members means dependency resolution will fail at build time
    (``clang requires llvm=X but llvm is Y``). Returns None when fewer than two
    locked members are present or all agree.
    """
    locked = {
        name: pkgbuild_pkgvers[name]
        for name in pkgbuild_pkgvers
        if name in LLVM_LOCKSTEP_SUITE and pkgbuild_pkgvers[name]
    }
    versions = set(locked.values())
    if len(versions) <= 1:
        return None

    by_ver: dict[str, list[str]] = {}
    for name, ver in sorted(locked.items()):
        by_ver.setdefault(ver, []).append(name)
    groups = " vs ".join(
        f"{'/'.join(names)} {ver}" for ver, names in sorted(by_ver.items())
    )
    return ToolchainFinding(
        SEV_ERROR, "pkgver_lockstep",
        f"LLVM PKGBUILD pkgver skew across lockstep members ({groups}) — "
        "dependency resolution will fail at build time",
        "Sync the stale source trees before building (e.g. rerun with "
        "--cleansrc), or pass --allow-version-skew to override.",
        is_brick=True,
    )


# ---------------------------------------------------------------------------
# libLLVM soname-change impact — pre-build reverse-dependency assessment
# ---------------------------------------------------------------------------

_LIBLLVM_SO_RE = re.compile(r"(?:^|/)libLLVM\.so\.(?P<ver>\d+\.\d+)$")


def _installed_libllvm_soname() -> str | None:
    """Return the installed ``libLLVM.so.<major>.<minor>`` filename, or None.

    Read authoritatively from the on-disk file list of the installed
    ``llvm-libs`` package (``pacman -Ql`` equivalent), picking the versioned
    runtime object — not the unversioned ``libLLVM.so`` dev symlink. None when
    ``llvm-libs`` is not installed (fresh system: nothing to break) or no
    versioned object is recorded.
    """
    from sysforge.primitives import pacman

    for path in pacman.get_package_files("llvm-libs"):
        m = _LIBLLVM_SO_RE.search(path)
        if m:
            return f"libLLVM.so.{m.group('ver')}"
    return None


def assess_libllvm_soname_impact(
    target_pkgver: str, *, exclude: set[str],
) -> SonameImpact | None:
    """Detect an impending libLLVM soname change and enumerate broken consumers.

    ``target_pkgver`` is the pkgver parsed from the about-to-be-built llvm
    PKGBUILD (e.g. ``22.1.5``); the new soname is ``libLLVM.so.<major>.<minor>``
    of its first two components. The *installed* soname is read from the live
    ``llvm-libs`` file list (authoritative — independent of any version scheme
    assumption). Returns None when:

      - ``llvm-libs`` is not installed (nothing on the system to break),
      - the target pkgver has no major.minor (parse failure),
      - the soname is unchanged (the common same-version PGO-refresh case), or
      - no installed consumer links the old soname after ``exclude`` is applied.

    Otherwise it walks every installed package's recorded ``%DEPENDS%`` for an
    auto-generated ``libLLVM.so=<major.minor>`` soname dep matching the *old*
    soname (reusing :data:`dep_analysis._SONAME_RE`), collapses each hit to its
    pkgbase, drops anything in ``exclude`` (the LLVM lockstep suite + the names
    being built this run — they are rebuilt here already), and returns the
    deduped, sorted consumer list in a :class:`SonameImpact`.

    Pure facts only: every external read is guarded and degrades to "no impact"
    (None) rather than raising — the stage owns the abort/warn/prompt policy.
    """
    parts = (target_pkgver or "").split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    target_mm = f"{parts[0]}.{parts[1]}"
    new_soname = f"libLLVM.so.{target_mm}"

    old_soname = _installed_libllvm_soname()
    if old_soname is None:
        return None
    old_mm = old_soname[len("libLLVM.so."):]
    if old_mm == target_mm:
        return None  # same-version rebuild — soname-identical (the std:: re-export
        # drift is owned by the Gate-2/3 symbol diff + libllvm_abi_consumers, not
        # by this soname-bump path).

    consumers = libllvm_soname_consumers(old_mm, exclude=exclude)
    if not consumers:
        return None
    return SonameImpact(old_soname, new_soname, consumers)


def libllvm_soname_consumers(mm: str, *, exclude: set[str]) -> list[str]:
    """Installed pkgbases whose recorded ``%DEPENDS%`` carry an auto-generated
    ``libLLVM.so=<mm>`` soname dep, collapsed to pkgbase and minus ``exclude``.

    The reverse-dependency walk shared by :func:`assess_libllvm_soname_impact`
    (soname bump) and :func:`libllvm_abi_consumers` (same-soname ABI regression).
    Reuses ``dep_analysis._SONAME_RE``; degrades to ``[]`` on any pacman read
    error rather than raising — the stage owns the abort/warn/prompt policy.
    """
    from sysforge.primitives import pacman
    from sysforge.primitives.dep_analysis import _SONAME_RE

    consumers: set[str] = set()
    try:
        installed = pacman.get_all_installed_packages()
    except Exception:
        return []
    for name in installed:
        for dep in pacman.get_package_depends(name):
            m = _SONAME_RE.match(dep)
            if m and m.group("base") == "libLLVM.so" and m.group("ver") == mm:
                pkgbase = pacman.get_pkgbase(name) or name
                if pkgbase not in exclude:
                    consumers.add(pkgbase)
                break
    return sorted(consumers)


def libllvm_abi_consumers(*, exclude: set[str]) -> list[str]:
    """Installed pkgbases linking the *currently-installed* libLLVM soname.

    Same reverse-dep enumeration as :func:`assess_libllvm_soname_impact`, but NOT
    gated on a soname change — used when a same-soname ABI regression (the PGO
    ``-fprofile-use`` std:: re-export drift) strands consumers that must be
    rebuilt against the freshly-installed libLLVM. Returns the deduped, sorted
    consumer list (minus ``exclude``), or ``[]`` when ``llvm-libs`` is absent or
    nothing links it.
    """
    old_soname = _installed_libllvm_soname()
    if old_soname is None:
        return []
    old_mm = old_soname[len("libLLVM.so."):]
    return libllvm_soname_consumers(old_mm, exclude=exclude)


# ---------------------------------------------------------------------------
# Build-space headroom — pre-build
# ---------------------------------------------------------------------------

def _device_of(path: Path) -> tuple[int, Path] | None:
    """Return ``(st_dev, existing_ancestor)`` for ``path``.

    Walks up to the nearest existing ancestor so a not-yet-created staging dir
    still resolves to the filesystem that will host it. None if nothing in the
    chain exists (degrades to "no finding").
    """
    p = path
    while True:
        if p.exists():
            try:
                return os.stat(p).st_dev, p
            except OSError:
                return None
        if p.parent == p:
            return None
        p = p.parent


def check_build_space(
    paths: list[Path], min_free_gb: float,
) -> ToolchainFinding | None:
    """Brick finding when a filesystem hosting a build path lacks headroom.

    Each distinct filesystem (deduped by ``st_dev``) backing any of ``paths``
    (staging1 / staging2 / pgo_store / builddir parents) must have at least
    ``min_free_gb`` free — staging dirs sharing one filesystem are counted once
    against that single pool, not summed. Reports the most-constrained device.
    Returns None when every device clears the bar (or none can be stat'd).
    """
    if min_free_gb <= 0:
        return None
    seen_devs: dict[int, Path] = {}
    for path in paths:
        dev = _device_of(Path(path))
        if dev is None:
            continue
        st_dev, ancestor = dev
        seen_devs.setdefault(st_dev, ancestor)

    worst: tuple[float, Path] | None = None
    for ancestor in seen_devs.values():
        try:
            usage = shutil.disk_usage(ancestor)
        except OSError:
            continue
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_free_gb and (worst is None or free_gb < worst[0]):
            worst = (free_gb, ancestor)

    if worst is None:
        return None
    free_gb, ancestor = worst
    return ToolchainFinding(
        SEV_ERROR, "build_space",
        f"filesystem at {ancestor} has only {free_gb:.1f} GiB free "
        f"(need ≥ {min_free_gb:g} GiB) — the LLVM build will likely fail "
        "partway with no space left",
        "Free space on the staging/pgo_store/build filesystem, lower "
        "min_build_free_gb in toolchain.toml, or pass --skip-build-space-check.",
        is_brick=True,
    )


# ---------------------------------------------------------------------------
# Multilib repo — pre-build, only when a lib32-* is in scope
# ---------------------------------------------------------------------------

def check_multilib_enabled(lib32_in_scope: bool) -> ToolchainFinding | None:
    """Brick finding when a lib32-* is in scope but [multilib] is disabled.

    Building any ``lib32-*`` package needs the ``[multilib]`` repo enabled in
    /etc/pacman.conf (it provides ``lib32-glibc`` and friends as makedeps).
    Without it the lib32 builds fail at dependency resolution. Detection mirrors
    the section parse in ``pacman.py`` — a ``[multilib]`` header that is not
    commented out. Returns None when no lib32 is in scope or multilib is
    enabled.
    """
    if not lib32_in_scope:
        return None
    if not _PACMAN_CONF.is_file():
        return None
    try:
        text = _PACMAN_CONF.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("[multilib]"):
            return None
    return ToolchainFinding(
        SEV_ERROR, "multilib_disabled",
        "lib32-* packages are in the toolchain build scope but the [multilib] "
        "repo is not enabled in /etc/pacman.conf — the lib32 builds will fail "
        "at dependency resolution",
        "Enable [multilib] in /etc/pacman.conf (uncomment the section and its "
        "Include line, then `sudo pacman -Sy`), drop the lib32 packages from "
        "toolchain.toml, or set require_multilib = false.",
        is_brick=True,
    )
