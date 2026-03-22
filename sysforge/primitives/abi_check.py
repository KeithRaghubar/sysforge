"""
abi_check.py — post-build ABI compatibility checker

For each shared library (.so.*) in a built package archive, cross-references
undefined versioned symbol references against the exported versioned symbols
of its NEEDED runtime libraries as currently installed on the system.

This catches ABI breakage at build time before installation — e.g. a library
built against a newer libfoo that exports sym@@FOO_2.0, but the system still
has libfoo exporting only sym@@FOO_1.0.

Public API:
    check_package_abi(pkg_path: Path) -> list[str]
        Returns a list of warning strings (empty if no issues or no .so files).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import sysforge.log as _log


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
_RE_LDCONFIG = re.compile(r"^\s+(\S+)\s+\([^)]+\)\s+=>\s+(\S+)$", re.MULTILINE)


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
        _log.warn("[ABI]", f"bsdtar list failed for {pkg_path.name}: {result.stderr.strip()}")
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
        _log.warn("[ABI]", f"bsdtar extract failed: {result.stderr.strip()}")
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


def _build_ldconfig_map() -> dict[str, str]:
    """Run ldconfig -p and return {soname: /path/to/lib}."""
    result = subprocess.run(
        ["ldconfig", "-p"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return {}
    mapping: dict[str, str] = {}
    for m in _RE_LDCONFIG.finditer(result.stdout):
        soname, path = m.group(1), m.group(2)
        mapping.setdefault(soname, path)  # first hit wins (highest priority)
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

def check_package_abi(pkg_path: Path) -> list[str]:
    """
    Check ABI compatibility of shared libraries in a built package.

    Extracts .so.* files from the archive, then for each library:
    - Collects undefined versioned symbol requirements (nm -D, U sym@VER)
    - Collects NEEDED sonames (readelf -d)
    - Maps sonames to system library paths (ldconfig -p)
    - Checks each required (sym, ver) is exported by at least one NEEDED lib

    Returns a list of warning strings describing any unsatisfied references.
    Returns an empty list if the package has no shared libraries (no-op).
    """
    so_members = _list_sos_in_pkg(pkg_path)
    if not so_members:
        _log.info("[ABI]", f"{pkg_path.name}: no shared libraries — skipping ABI check")
        return []

    _log.info("[ABI]", f"{pkg_path.name}: checking {len(so_members)} shared librar{'y' if len(so_members) == 1 else 'ies'}")

    issues: list[str] = []
    ldconfig_map = _build_ldconfig_map()
    export_cache: dict[str, set[tuple[str, str]]] = {}

    with tempfile.TemporaryDirectory(prefix="sysforge-abi-") as tmpdir:
        tmp = Path(tmpdir)
        extracted = _extract_sos(pkg_path, so_members, tmp)

        for so_path in extracted:
            undef = _undefined_versioned(so_path)
            if not undef:
                continue

            needed = _needed_sonames(so_path)
            if not needed:
                continue

            # Build union of all symbols exported by NEEDED libs
            all_exports: set[tuple[str, str]] = set()
            missing_libs: list[str] = []
            for soname in needed:
                lib_path = ldconfig_map.get(soname)
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
