# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
abi_check.py — ABI compatibility checker

For each shared library (.so.*), cross-references undefined versioned symbol
references against the exported versioned symbols of its NEEDED runtime
libraries as currently installed on the system.

This catches ABI breakage — e.g. a library built against a newer libfoo that
exports sym@@FOO_2.0, but the system still has libfoo exporting only
sym@@FOO_1.0.

Resolution is symbol-version precise to avoid false positives:
  - Exports are captured at BOTH the default (sym@@VER) and non-default
    (sym@VER) version forms — the glibc back-compat pattern; the linker
    resolves an undefined sym@VER against either.
  - Each undefined sym@VER is bound to the specific NEEDED soname that provides
    VER via the .gnu.version_r (Verneed) section, so host/loader-provided
    versions are not mis-attributed to a NEEDED lib.
  - When the bound lib defines VER but not the symbol, and the symbol is an
    optional LLVM target-init entry point (reduced-target libLLVM), it is
    demoted as benign rather than reported. A genuine symbol-within-version
    break (e.g. _ZNSt*@LLVM_*) is still flagged.

Public API:
    check_so_files(so_paths, *, benign_sink=None) -> list[str]
        Pure .so-level core. Takes any list of on-disk shared libraries and
        returns warning strings. Used by the build path (via check_package_abi)
        and by doctor.py on installed .so files. ``benign_sink``, if given,
        accumulates demoted optional-symbol cases for a caller summary line.

    check_package_abi(pkg_path: Path) -> list[str]
        Archive wrapper. Extracts .so.* members from a built .pkg.tar.zst
        and calls check_so_files on the extracted files.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from sysforge import log
_log = log.get_logger("ABI")


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# readelf -d NEEDED line: " 0x... (NEEDED)  Shared library: [libfoo.so.1]"
_RE_NEEDED = re.compile(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]")

# `readelf --version-info` Verneed (.gnu.version_r) lines. Each entry opens with
# a "File: <soname>" line naming the NEEDED library the version requirement was
# bound to at link time, followed by one or more "Name: <version>" lines:
#
#   000000: Version: 1  File: libLLVM.so.22.1  Cnt: 1
#   0x0010:   Name: LLVM_22.1  Flags: none  Version: 12
#
# Parsing this lets us bind an undefined "sym@VER" to the *specific* NEEDED
# soname that provides VER, instead of checking against the union of every
# NEEDED lib's exports (which over-reports when a version is host/loader
# provided and under-attributes which lib is at fault).
_RE_VERNEED_FILE = re.compile(r"\bFile:\s+(\S+)")
_RE_VERNEED_NAME = re.compile(r"\bName:\s+(\S+)")

# LLVM optional target-init entry points. libLLVM is routinely built with a
# reduced LLVM_TARGETS_TO_BUILD (notably the multilib lib32-llvm, which ships
# only X86/NVPTX), so target-registration symbols for un-built backends —
# LLVMInitialize<Target>{Target,TargetInfo,TargetMC,AsmParser,AsmPrinter,
# Disassembler} — are simply absent. Mesa's gallium drivers reference all of
# them, but each is lazily bound and only dereferenced when that GPU target is
# the active driver, so the absence is benign on hardware that never selects it.
# When such a symbol is missing but its version node IS present in the bound
# libLLVM, it is demoted to an info-level note rather than flagged as drift.
# This is deliberately narrow: a genuine ABI break (e.g. a C++ stdlib symbol
# _ZNSt*@LLVM_* bound to the wrong version namespace) does not match this
# pattern and stays a hard finding.
_RE_LLVM_TARGET_INIT = re.compile(
    r"^LLVMInitialize.+?"
    r"(?:TargetInfo|TargetMC|Target|AsmParser|AsmPrinter|Disassembler)$"
)

# ldconfig -p line: "  libfoo.so.1 (libc6,x86-64) => /usr/lib/libfoo.so.1"
# Also capture the tag so we can distinguish 32-bit (libc6) from 64-bit
# (libc6,x86-64) variants — they share a soname but export differently-mangled
# symbols, so a 32-bit .so must only resolve NEEDED references against 32-bit
# libs.
_RE_LDCONFIG = re.compile(r"^\s+(\S+)\s+\(([^)]+)\)\s+=>\s+(\S+)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _list_sos_in_pkg(pkg_path: Path) -> list[str]:
    """Return archive member paths for shared libraries in the package."""
    result = subprocess.run(
        ["bsdtar", "-t", str(pkg_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _log.warn(f"bsdtar list failed for {pkg_path.name}: {result.stderr.strip()}")
        return []
    # Match usr/lib/libfoo.so.N or usr/lib/libfoo.so.N.M etc.
    # Also plain .so symlinks are excluded — we want the actual ELF files.
    entries = []
    for line in result.stdout.splitlines():
        line = line.rstrip("/")
        if ".so." in line and not line.endswith(".a"):
            entries.append(line)
    return entries


def _extract_sos(pkg_path: Path, members: list[str], dest: Path) -> list[Path]:
    """Extract the given archive members to dest, return their extracted paths."""
    if not members:
        return []
    result = subprocess.run(
        ["bsdtar", "-x", "-f", str(pkg_path), "-C", str(dest)] + members,
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _log.warn(f"bsdtar extract failed: {result.stderr.strip()}")
    extracted = []
    for member in members:
        p = dest / member
        if p.exists() and p.is_file():
            extracted.append(p)
    return extracted


def _parse_nm_undefined(nm_output: str) -> set[tuple[str, str]]:
    """Parse `nm -D` output for strong undefined versioned references.

    An undefined symbol prints with an empty address column, so its line splits
    into exactly two tokens: ``<type> <name>``. We keep only strong undefined
    references (``U``) — weak undefined (``w``/``v``) are optional by design and
    resolve to 0 when absent, so they are never a linkage hazard. The name must
    carry a single-``@`` version requirement (``sym@VER``); ``@@`` is the
    defined-default form and never appears on an undefined line.
    """
    out: set[tuple[str, str]] = set()
    for line in nm_output.splitlines():
        toks = line.split()
        if len(toks) != 2:
            continue
        typ, name = toks
        if typ != "U":
            continue
        if "@" not in name or "@@" in name:
            continue
        sym, _, ver = name.partition("@")
        if sym and ver:
            out.add((sym, ver))
    return out


def _undefined_versioned(so_path: Path) -> set[tuple[str, str]]:
    """Return set of (symbol, version) pairs that are undefined with a version requirement."""
    result = subprocess.run(
        ["nm", "-D", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return set()
    return _parse_nm_undefined(result.stdout)


def needed_sonames(so_path: Path) -> list[str]:
    """Return NEEDED sonames from the dynamic section of so_path."""
    result = subprocess.run(
        ["readelf", "-d", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    return _RE_NEEDED.findall(result.stdout)


def _parse_verneed(version_info: str) -> dict[str, set[str]]:
    """Parse `readelf --version-info` Verneed entries into {version: {soname}}.

    Only the ``.gnu.version_r`` (Version needs) section is consulted; the
    Version-definition and Version-symbols sections also carry ``Name:`` lines
    and must not bleed in. Each requirement version maps to the NEEDED soname(s)
    the linker recorded as its provider.
    """
    mapping: dict[str, set[str]] = {}
    current_file: str | None = None
    in_verneed = False
    for line in version_info.splitlines():
        if "Version needs section" in line:
            in_verneed = True
            current_file = None
            continue
        if ("Version definition section" in line
                or "Version symbols section" in line):
            in_verneed = False
            continue
        if not in_verneed:
            continue
        fm = _RE_VERNEED_FILE.search(line)
        if fm:
            current_file = fm.group(1)
            continue
        nm = _RE_VERNEED_NAME.search(line)
        if nm and current_file is not None:
            mapping.setdefault(nm.group(1), set()).add(current_file)
    return mapping


def _verneed_map(so_path: Path) -> dict[str, set[str]]:
    """Return {version_name: {bound NEEDED soname}} for so_path, or {} on failure."""
    result = subprocess.run(
        ["readelf", "--version-info", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return {}
    return _parse_verneed(result.stdout)


def _is_optional_llvm_target_init(sym: str, ver: str) -> bool:
    """True if (sym, ver) is an optional LLVM target-registration entry point.

    These are absent whenever libLLVM was built without that target backend
    (routine for the reduced-target multilib lib32-llvm). Bound to the
    ``LLVM_*`` version namespace and lazily dereferenced, their absence is
    benign — see ``_RE_LLVM_TARGET_INIT``.
    """
    return ver.startswith("LLVM_") and bool(_RE_LLVM_TARGET_INIT.match(sym))


def _elf_class(so_path: Path) -> str:
    """Return 'ELF32' or 'ELF64' for so_path; empty string on failure."""
    result = subprocess.run(
        ["readelf", "-h", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Class:"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[-1]
    return ""


def _ldconfig_tag_class(tag: str) -> str:
    """Map an ldconfig -p tag like 'libc6,x86-64' to 'ELF64' or 'ELF32'."""
    # 64-bit markers ldconfig emits for our supported arches.
    if any(m in tag for m in ("x86-64", "AArch64", "aarch64", "64bit")):
        return "ELF64"
    return "ELF32"


def _build_ldconfig_map() -> dict[tuple[str, str], str]:
    """
    Run ldconfig -p and return {(soname, elf_class): /path/to/lib}.

    The elf_class key ('ELF32' or 'ELF64') ensures 32-bit and 64-bit variants
    of the same soname don't collide — without it a 32-bit .so would resolve
    its NEEDED references against 64-bit exports, producing a storm of
    false-positive "undefined symbol" findings (different mangling for
    unsigned int vs unsigned long).
    """
    result = subprocess.run(
        ["ldconfig", "-p"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return {}
    mapping: dict[tuple[str, str], str] = {}
    for m in _RE_LDCONFIG.finditer(result.stdout):
        soname, tag, path = m.group(1), m.group(2), m.group(3)
        klass = _ldconfig_tag_class(tag)
        mapping.setdefault((soname, klass), path)  # first hit wins
    return mapping


def _parse_nm_exports(nm_output: str) -> set[tuple[str, str]]:
    """Parse `nm -D` output for *defined* versioned exports.

    A defined symbol prints ``<addr> <type> <name>`` (three tokens; the address
    column is non-empty). We capture both the default-version form ``sym@@VER``
    AND the non-default form ``sym@VER`` (single ``@``). The latter is the glibc
    back-compat pattern (e.g. ``realpath@GLIBC_2.2.5``, ``dlopen@GLIBC_2.1``):
    the dynamic linker resolves an undefined ``sym@VER`` against *any* defined
    ``sym@VER``, default or not, so an export set that captured only ``@@``
    under-reported real exports and produced a storm of false positives.
    """
    exports: set[tuple[str, str]] = set()
    for line in nm_output.splitlines():
        toks = line.split()
        if len(toks) < 3:
            # Two-token lines are undefined symbols; not exports.
            continue
        name = toks[2]
        if "@@" in name:
            sym, _, ver = name.partition("@@")
        elif "@" in name:
            sym, _, ver = name.partition("@")
        else:
            continue
        if sym and ver:
            exports.add((sym, ver))
    return exports


def _exported_versioned(lib_path: str,
                        cache: dict[str, set[tuple[str, str]]]) -> set[tuple[str, str]]:
    """Return set of (symbol, version) exported by lib_path; results are cached."""
    if lib_path in cache:
        return cache[lib_path]
    result = subprocess.run(
        ["nm", "-D", lib_path],
        capture_output=True, text=True, check=False,
    )
    exports: set[tuple[str, str]] = set()
    if result.returncode == 0:
        exports = _parse_nm_exports(result.stdout)
    cache[lib_path] = exports
    return exports


def _demangle(symbols: list[str]) -> dict[str, str]:
    """Run c++filt on a list of mangled names; return {mangled: demangled}."""
    if not symbols:
        return {}
    result = subprocess.run(
        ["c++filt"] + symbols,
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return {s: s for s in symbols}
    demangled = result.stdout.splitlines()
    return {s: d for s, d in zip(symbols, demangled)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Shared libraries whose "undefined versioned symbol" reports are inherent
# to the library's design rather than linkage bugs. Skipping these avoids
# drowning real findings under known-benign noise:
#
#   libnsl.so.1           — glibc RPC compat shim; xdr_*/svc_*/clnt_* symbols
#                           are actually implemented in libtirpc at runtime
#                           but libnsl doesn't declare libtirpc as NEEDED.
#   libc_malloc_debug.so  — weak __malloc_initialize_hook override pattern.
#
# Matches on the .so file basename (with or without version suffix).
_ABI_CHECK_SHIM_LIBS = {
    "libnsl.so.1",
    "libc_malloc_debug.so",
    "libc_malloc_debug.so.0",
}


# Packages that ship vendored prebuilt binaries compiled against a fixed
# distro (typically Ubuntu/Debian steam-runtime). Their undefined versioned
# symbols mirror that baked-in toolchain, not the host system — reinstalling
# cannot fix it and the findings overwhelm real signal. Skipped at the
# package level in doctor's ABI pass. Depends check still runs.
#
# Keep this list conservative: only packages that ship a self-contained
# bundle under their own prefix. Do NOT add metapackages (e.g.
# steam-native-runtime) whose closures pull real system libs we want
# checked.
_ABI_CHECK_SKIP_PACKAGES = {
    "steam",
    "discord",
    "brave-bin",
}


def _is_shim_lib(so_path: Path) -> bool:
    """True if so_path is a known-benign compat shim that should be skipped."""
    return so_path.name in _ABI_CHECK_SHIM_LIBS


def is_abi_check_skipped_package(pkgname: str) -> bool:
    """True if pkgname is in the bundled-binary skip list."""
    return pkgname in _ABI_CHECK_SKIP_PACKAGES


def check_so_files(so_paths: list[Path], *,
                   benign_sink: list[str] | None = None) -> list[str]:
    """
    Check ABI compatibility of a list of on-disk shared libraries.

    For each library:
    - Collects strong undefined versioned requirements (nm -D, ``U sym@VER``)
    - Collects NEEDED sonames (readelf -d) and binds each required version to
      the specific NEEDED soname that provides it (readelf --version-info /
      ``.gnu.version_r``); falls back to the union of NEEDED exports when the
      Verneed section can't be parsed
    - Resolves bound sonames to on-disk libs (ldconfig -p), arch-matched
    - Captures both default (``sym@@VER``) and non-default (``sym@VER``) exports

    A requirement is reported as a hard finding only when the bound NEEDED lib
    fails to provide it AND the failure is not a benign optional case:
    - a version bound to no NEEDED soname is host/loader-provided → skipped;
    - an absent LLVM optional target-init symbol whose version node IS present
      in the bound libLLVM (reduced-target build) is demoted to ``benign_sink``
      and logged at info, not reported.

    ``benign_sink`` (when provided) accumulates ``"<so>: <sym>@<ver>"`` strings
    for the demoted optional-symbol cases so a caller can render a single
    summary line instead of per-symbol noise.

    Returns a list of warning strings describing genuine unsatisfied references
    or NEEDED libs missing from the ldconfig cache. Empty list if clean.
    """
    if not so_paths:
        return []

    issues: list[str] = []
    ldconfig_map = _build_ldconfig_map()
    export_cache: dict[str, set[tuple[str, str]]] = {}

    for so_path in so_paths:
        if _is_shim_lib(so_path):
            continue

        undef = _undefined_versioned(so_path)
        if not undef:
            continue

        needed = needed_sonames(so_path)
        if not needed:
            continue

        so_class = _elf_class(so_path) or "ELF64"
        needed_set = set(needed)

        # Resolve NEEDED sonames to arch-matched on-disk libs.
        needed_paths: dict[str, str] = {}
        missing_libs: set[str] = set()
        for soname in needed:
            lib_path = ldconfig_map.get((soname, so_class))
            if lib_path is None:
                missing_libs.add(soname)
            else:
                needed_paths[soname] = lib_path

        # Union of exports and defined version nodes across all resolvable
        # NEEDED libs. Satisfaction is checked against the UNION rather than a
        # single Verneed-bound lib: a GLIBC_*-versioned symbol can legitimately
        # resolve from any glibc-family NEEDED lib (libc/librt/libpthread/libdl
        # have merged over time, so a binary's recorded Verneed soname may no
        # longer be the actual exporter). The version-node set distinguishes
        # "no NEEDED lib ever had version V" (drift / soname skew) from
        # "V exists but this one symbol is absent" (optional/conditional).
        union_exports: set[tuple[str, str]] = set()
        union_versions: set[str] = set()
        for lib_path in needed_paths.values():
            exp = _exported_versioned(lib_path, export_cache)
            union_exports |= exp
            union_versions |= {ver for _, ver in exp}

        # Verneed binds each required version to the NEEDED soname(s) the linker
        # recorded. We use it only to (a) skip versions provided by the host /
        # loader (bound to no NEEDED lib) and (b) attribute a genuinely-absent
        # version to the specific missing NEEDED lib — not to restrict where a
        # symbol may be satisfied from.
        verneed = _verneed_map(so_path)

        hard: list[tuple[str, str]] = []
        needed_missing: set[str] = set()
        for sym, ver in undef:
            if (sym, ver) in union_exports:
                continue  # satisfied by some NEEDED lib

            if verneed:
                bound = verneed.get(ver)
                if not bound or not (bound & needed_set):
                    # Version declared by no NEEDED lib → host/loader provided.
                    continue
                bound_missing = bound & missing_libs
            else:
                bound_missing = missing_libs

            if ver in union_versions:
                # Version node exists but this symbol is absent from it.
                if _is_optional_llvm_target_init(sym, ver):
                    _log.info(
                        f"{so_path.name}: optional LLVM target-init symbol absent "
                        f"(benign; reduced-target libLLVM): {sym}@{ver}"
                    )
                    if benign_sink is not None:
                        benign_sink.append(f"{so_path.name}: {sym}@{ver}")
                    continue
                # A real symbol removal within an existing version (e.g. a C++
                # stdlib symbol bound to the wrong version namespace).
                hard.append((sym, ver))
            elif bound_missing:
                # Version node absent here, but a NEEDED lib that should provide
                # it is missing from ldconfig — attribute to the missing lib.
                needed_missing |= bound_missing
            else:
                # Version node provided by no resolvable NEEDED lib — drift.
                hard.append((sym, ver))

        if hard:
            dm = _demangle([sym for sym, _ in hard])
            for sym, ver in sorted(hard):
                readable = dm.get(sym, sym)
                label = f"{readable} ({sym})" if readable != sym else sym
                issues.append(
                    f"{so_path.name}: undefined versioned symbol not found in any NEEDED lib: "
                    f"{label}@{ver}"
                )

        for soname in sorted(needed_missing):
            issues.append(
                f"{so_path.name}: NEEDED lib {soname!r} not found in ldconfig cache — "
                "may not be installed or ldconfig not yet run"
            )

    return issues


def check_package_abi(pkg_path: Path) -> list[str]:
    """
    Check ABI compatibility of shared libraries in a built package archive.

    Extracts .so.* members with bsdtar, then calls check_so_files.
    Returns an empty list if the package has no shared libraries (no-op).
    """
    so_members = _list_sos_in_pkg(pkg_path)
    if not so_members:
        _log.info(f"{pkg_path.name}: no shared libraries — skipping ABI check")
        return []

    _log.info(f"{pkg_path.name}: checking {len(so_members)} shared librar{'y' if len(so_members) == 1 else 'ies'}")

    with tempfile.TemporaryDirectory(prefix="sysforge-abi-") as tmpdir:
        extracted = _extract_sos(pkg_path, so_members, Path(tmpdir))
        return check_so_files(extracted)


def report_post_build_abi(built_pkgs: list) -> None:
    """Run non-fatal post-build ABI checks on freshly built packages.

    Advisory only: each finding is logged under the ABI tag and any error is
    swallowed (the build already succeeded, so a checker failure must not turn
    a green build red).  Called by the build orchestrator after a successful
    build when ``BuildOptions.abi_check`` is set.
    """
    try:
        if not built_pkgs:
            _log.info("No built packages found for ABI check")
        for pkg in built_pkgs:
            issues = check_package_abi(pkg)
            if issues:
                for issue in issues:
                    _log.warn(issue)
            else:
                _log.info(f"{pkg.name}: OK")
    except Exception as e:
        _log.warn(f"ABI check failed: {e}")
