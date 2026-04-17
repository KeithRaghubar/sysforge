"""
abi_check.py — ABI compatibility checker

For each shared library (.so.*), cross-references undefined versioned symbol
references against the exported versioned symbols of its NEEDED runtime
libraries as currently installed on the system.

This catches ABI breakage — e.g. a library built against a newer libfoo that
exports sym@@FOO_2.0, but the system still has libfoo exporting only
sym@@FOO_1.0.

Public API:
    check_so_files(so_paths: list[Path]) -> list[str]
        Pure .so-level core. Takes any list of on-disk shared libraries and
        returns warning strings. Used by the build path (via check_package_abi)
        and by doctor.py on installed .so files.

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

# nm -D output line: "address type name"
# Undefined symbols have an empty (whitespace-only) address column; defined have hex.
# Versioned: "sym@VER" (undefined requirement) or "sym@@VER" (exported default)
_RE_NM_UNDEF = re.compile(r"^\S*\s+U\s+(.+@[^@].+)$", re.MULTILINE)
_RE_NM_EXPORT = re.compile(r"^\S+\s+\S\s+(.+@@.+)$", re.MULTILINE)

# readelf -d NEEDED line: " 0x... (NEEDED)  Shared library: [libfoo.so.1]"
_RE_NEEDED = re.compile(r"\(NEEDED\)\s+Shared library:\s+\[([^\]]+)\]")

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


def _undefined_versioned(so_path: Path) -> set[tuple[str, str]]:
    """Return set of (symbol, version) pairs that are undefined with a version requirement."""
    result = subprocess.run(
        ["nm", "-D", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return set()
    out = set()
    for m in _RE_NM_UNDEF.finditer(result.stdout):
        raw = m.group(1)
        # sym@VER — single @ means undefined version reference
        # (sym@@VER would be defined default version — won't appear as U)
        at = raw.index("@")
        sym = raw[:at]
        ver = raw[at + 1:]
        if ver.startswith("@"):
            ver = ver[1:]  # normalise @@VER → VER if it slipped through
        out.add((sym, ver))
    return out


def _needed_sonames(so_path: Path) -> list[str]:
    """Return NEEDED sonames from the dynamic section of so_path."""
    result = subprocess.run(
        ["readelf", "-d", str(so_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    return _RE_NEEDED.findall(result.stdout)


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
        for m in _RE_NM_EXPORT.finditer(result.stdout):
            raw = m.group(1)
            # sym@@VER — double @ is the exported default version
            dbl = raw.index("@@")
            sym = raw[:dbl]
            ver = raw[dbl + 2:]
            exports.add((sym, ver))
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


def _is_shim_lib(so_path: Path) -> bool:
    """True if so_path is a known-benign compat shim that should be skipped."""
    return so_path.name in _ABI_CHECK_SHIM_LIBS


def check_so_files(so_paths: list[Path]) -> list[str]:
    """
    Check ABI compatibility of a list of on-disk shared libraries.

    For each library:
    - Collects undefined versioned symbol requirements (nm -D, U sym@VER)
    - Collects NEEDED sonames (readelf -d)
    - Maps sonames to system library paths (ldconfig -p)
    - Checks each required (sym, ver) is exported by at least one NEEDED lib

    Returns a list of warning strings describing any unsatisfied references
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

        needed = _needed_sonames(so_path)
        if not needed:
            continue

        so_class = _elf_class(so_path) or "ELF64"

        # Build union of all symbols exported by NEEDED libs of matching arch
        all_exports: set[tuple[str, str]] = set()
        missing_libs: list[str] = []
        for soname in needed:
            lib_path = ldconfig_map.get((soname, so_class))
            if lib_path is None:
                missing_libs.append(soname)
                continue
            all_exports |= _exported_versioned(lib_path, export_cache)

        unsatisfied = undef - all_exports
        if not unsatisfied:
            continue

        # Demangle symbol names for readability
        mangled_names = [sym for sym, _ in unsatisfied]
        dm = _demangle(mangled_names)

        for sym, ver in sorted(unsatisfied):
            readable = dm.get(sym, sym)
            if readable != sym:
                label = f"{readable} ({sym})"
            else:
                label = sym
            issues.append(
                f"{so_path.name}: undefined versioned symbol not found in any NEEDED lib: "
                f"{label}@{ver}"
            )

        for soname in missing_libs:
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
