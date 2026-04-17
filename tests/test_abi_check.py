"""
test_abi_check.py — unit tests for the post-build ABI compatibility checker.

All subprocess calls are mocked; no real ELF binaries or system tools required.

Covers:
    _list_sos_in_pkg     — bsdtar -t parsing, filter for .so.N entries
    _undefined_versioned — nm -D parsing for U sym@VER lines
    needed_sonames       — readelf -d parsing for NEEDED entries
    _build_ldconfig_map  — ldconfig -p parsing
    _exported_versioned  — nm -D parsing for defined sym@@VER lines, caching
    _demangle            — c++filt pass-through
    check_package_abi    — no .so files (no-op), clean package, missing symbol,
                           missing NEEDED lib in ldconfig, bsdtar failure
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.abi_check import (
    _build_ldconfig_map,
    _demangle,
    _exported_versioned,
    _is_shim_lib,
    _list_sos_in_pkg,
    needed_sonames,
    _undefined_versioned,
    check_package_abi,
    check_so_files,
)


# ---------------------------------------------------------------------------
# _list_sos_in_pkg
# ---------------------------------------------------------------------------

def _mock_run(stdout="", returncode=0):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


def test_list_sos_filters_so_files():
    listing = "\n".join([
        "usr/",
        "usr/lib/",
        "usr/lib/libfoo.so.1",
        "usr/lib/libfoo.so.1.2.3",
        "usr/lib/libfoo.so",          # plain .so — no version, excluded
        "usr/lib/libfoo.a",           # static lib, excluded
        "usr/share/doc/README",
        "usr/lib/libbar.so.2",
    ])
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(listing)):
        result = _list_sos_in_pkg(Path("pkg.tar.zst"))

    assert "usr/lib/libfoo.so.1" in result
    assert "usr/lib/libfoo.so.1.2.3" in result
    assert "usr/lib/libbar.so.2" in result
    # plain .so and .a must be excluded
    assert not any(e.endswith(".so") and ".so." not in e for e in result)
    assert not any(e.endswith(".a") for e in result)


def test_list_sos_returns_empty_on_failure():
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        result = _list_sos_in_pkg(Path("bad.tar.zst"))
    assert result == []


def test_list_sos_returns_empty_when_no_sos():
    listing = "usr/bin/mytool\nusr/share/doc/readme.txt\n"
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(listing)):
        result = _list_sos_in_pkg(Path("pkg.tar.zst"))
    assert result == []


# ---------------------------------------------------------------------------
# _undefined_versioned
# ---------------------------------------------------------------------------

def test_undefined_versioned_parses_undef_symbols():
    nm_out = (
        "                 U _ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_@LLVM_22.1\n"
        "0000000000000000 T _ZN4llvm5Value3useEv@@LLVM_22.1\n"  # defined, not U
        "                 U __gxx_personality_v0@GCC_3.0\n"
        "                 w __cxa_finalize@@GLIBC_2.17\n"  # defined weak, not U
    )
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(nm_out)):
        result = _undefined_versioned(Path("libfoo.so.1"))

    assert ("_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_", "LLVM_22.1") in result
    assert ("__gxx_personality_v0", "GCC_3.0") in result
    # defined symbols must not appear
    assert not any(ver == "LLVM_22.1" and "Value" in sym for sym, ver in result)


def test_undefined_versioned_returns_empty_on_nm_failure():
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        result = _undefined_versioned(Path("libfoo.so.1"))
    assert result == set()


def test_undefined_versioned_no_versioned_undefs():
    nm_out = "                 U printf\n0000000000001234 T main\n"
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(nm_out)):
        result = _undefined_versioned(Path("libfoo.so.1"))
    assert result == set()


# ---------------------------------------------------------------------------
# needed_sonames
# ---------------------------------------------------------------------------

def test_needed_sonames_parses_readelf():
    readelf_out = (
        " 0x0000000000000001 (NEEDED)  Shared library: [libLLVM.so.22.1]\n"
        " 0x0000000000000001 (NEEDED)  Shared library: [libc.so.6]\n"
        " 0x000000000000000e (SONAME)  Library soname: [libclang-cpp.so.22.1]\n"
    )
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(readelf_out)):
        result = needed_sonames(Path("libclang-cpp.so.22.1"))

    assert result == ["libLLVM.so.22.1", "libc.so.6"]


def test_needed_sonames_returns_empty_on_failure():
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        result = needed_sonames(Path("libfoo.so.1"))
    assert result == []


# ---------------------------------------------------------------------------
# _build_ldconfig_map
# ---------------------------------------------------------------------------

def test_build_ldconfig_map_parses_output():
    ldconfig_out = (
        "\tlibc.so.6 (libc6,x86-64) => /usr/lib/libc.so.6\n"
        "\tlibLLVM.so.22.1 (libc6,x86-64) => /usr/lib/libLLVM.so.22.1\n"
        "\tlibm.so.6 (libc6,x86-64) => /usr/lib/libm.so.6\n"
    )
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(ldconfig_out)):
        result = _build_ldconfig_map()

    assert result[("libc.so.6", "ELF64")] == "/usr/lib/libc.so.6"
    assert result[("libLLVM.so.22.1", "ELF64")] == "/usr/lib/libLLVM.so.22.1"


def test_build_ldconfig_map_separates_32_and_64_bit():
    # Same soname appearing as both 32-bit and 64-bit — must not collapse.
    ldconfig_out = (
        "\tlibc.so.6 (libc6,x86-64) => /usr/lib/libc.so.6\n"
        "\tlibc.so.6 (libc6) => /usr/lib32/libc.so.6\n"
    )
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(ldconfig_out)):
        result = _build_ldconfig_map()

    assert result[("libc.so.6", "ELF64")] == "/usr/lib/libc.so.6"
    assert result[("libc.so.6", "ELF32")] == "/usr/lib32/libc.so.6"


def test_build_ldconfig_map_returns_empty_on_failure():
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        result = _build_ldconfig_map()
    assert result == {}


# ---------------------------------------------------------------------------
# _exported_versioned
# ---------------------------------------------------------------------------

def test_exported_versioned_parses_defined_symbols():
    nm_out = (
        "00007a6c10 W _ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_@@LLVM_22.1\n"
        "000000001234 T _ZN4llvm5Value3useEv@@LLVM_22.1\n"
        "                 U __gxx_personality_v0@GCC_3.0\n"  # undefined — exclude
    )
    cache: dict = {}
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(nm_out)):
        result = _exported_versioned("/usr/lib/libLLVM.so.22.1", cache)

    assert ("_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_", "LLVM_22.1") in result
    assert ("_ZN4llvm5Value3useEv", "LLVM_22.1") in result
    # undefined must not appear
    assert ("__gxx_personality_v0", "GCC_3.0") not in result


def test_exported_versioned_caches_result():
    nm_out = "000000001234 T mysym@@VER_1.0\n"
    cache: dict = {}
    mock = MagicMock(return_value=_mock_run(nm_out))
    with patch("sysforge.primitives.abi_check.subprocess.run", mock):
        r1 = _exported_versioned("/usr/lib/libfoo.so.1", cache)
        r2 = _exported_versioned("/usr/lib/libfoo.so.1", cache)

    # subprocess.run called exactly once — second call served from cache
    assert mock.call_count == 1
    assert r1 == r2


# ---------------------------------------------------------------------------
# _demangle
# ---------------------------------------------------------------------------

def test_demangle_returns_readable_names():
    demangled_out = "std::string::_M_assign(std::string const&)\n"
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(demangled_out)):
        result = _demangle(["_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"])

    assert result["_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"] == "std::string::_M_assign(std::string const&)"


def test_demangle_empty_input():
    result = _demangle([])
    assert result == {}


def test_demangle_fallback_on_failure():
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        result = _demangle(["_ZNfoo"])
    assert result == {"_ZNfoo": "_ZNfoo"}


# ---------------------------------------------------------------------------
# check_package_abi — integration-level (mocked at subprocess boundary)
# ---------------------------------------------------------------------------

def test_check_package_abi_no_so_files():
    """Package with no .so files → no-op, empty issues list."""
    listing = "usr/bin/mytool\nusr/share/doc/readme.txt\n"
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run(listing)):
        issues = check_package_abi(Path("mytool-1.0-1-x86_64.pkg.tar.zst"))

    assert issues == []


def test_check_package_abi_clean_package():
    """Package whose .so has undefined versioned symbols all satisfied by NEEDED libs."""
    bsdtar_list = "usr/lib/libfoo.so.1\n"
    nm_undef = "                 U _Z3foov@MYLIB_1.0\n"
    readelf_needed = " 0x1 (NEEDED)  Shared library: [libbar.so.1]\n"
    ldconfig_out = "\tlibbar.so.1 (libc6,x86-64) => /usr/lib/libbar.so.1\n"
    nm_export = "0000001234 T _Z3foov@@MYLIB_1.0\n"
    cppfilt_out = "foo()\n"

    def dispatcher(cmd, **_kw):
        tool = cmd[0]
        if tool == "bsdtar" and "-t" in cmd:
            return _mock_run(bsdtar_list)
        if tool == "bsdtar" and "-x" in cmd:
            # Simulate extraction: create the file in the temp dir
            dest_flag_idx = cmd.index("-C") + 1
            dest = Path(cmd[dest_flag_idx])
            member = cmd[-1]
            extracted = dest / member
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"\x7fELF")  # fake ELF content
            return _mock_run("")
        if tool == "nm":
            path = cmd[-1]
            if "/usr/lib/" in path:
                return _mock_run(nm_export)
            return _mock_run(nm_undef)
        if tool == "readelf":
            return _mock_run(readelf_needed)
        if tool == "ldconfig":
            return _mock_run(ldconfig_out)
        if tool == "c++filt":
            return _mock_run(cppfilt_out)
        return _mock_run("")

    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_package_abi(Path("libfoo-1.0-1-x86_64.pkg.tar.zst"))

    assert issues == []


def test_check_package_abi_missing_symbol():
    """Package whose .so references a versioned symbol not exported by any NEEDED lib."""
    bsdtar_list = "usr/lib/libclang-cpp.so.22.1\n"

    missing_sym = "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"
    nm_undef = f"                 U {missing_sym}@LLVM_22.1\n"
    readelf_needed = " 0x1 (NEEDED)  Shared library: [libLLVM.so.22.1]\n"
    ldconfig_out = "\tlibLLVM.so.22.1 (libc6,x86-64) => /usr/lib/libLLVM.so.22.1\n"
    # The system libLLVM does NOT export this symbol (simulates the PGO ABI break)
    nm_export = "0000001234 T _ZN4llvm5ValueE@@LLVM_22.1\n"
    cppfilt_out = "std::string::_M_assign(std::string const&)\n"

    def dispatcher(cmd, **_kw):
        tool = cmd[0]
        if tool == "bsdtar" and "-t" in cmd:
            return _mock_run(bsdtar_list)
        if tool == "bsdtar" and "-x" in cmd:
            dest = Path(cmd[cmd.index("-C") + 1])
            member = cmd[-1]
            extracted = dest / member
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"\x7fELF")
            return _mock_run("")
        if tool == "nm":
            path = cmd[-1]
            # Only return exports for the exact system lib path from ldconfig
            if path == "/usr/lib/libLLVM.so.22.1":
                return _mock_run(nm_export)
            return _mock_run(nm_undef)
        if tool == "readelf":
            return _mock_run(readelf_needed)
        if tool == "ldconfig":
            return _mock_run(ldconfig_out)
        if tool == "c++filt":
            return _mock_run(cppfilt_out)
        return _mock_run("")

    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_package_abi(Path("clang-22.1-1-x86_64.pkg.tar.zst"))

    assert len(issues) == 1
    assert "libclang-cpp.so.22.1" in issues[0]
    assert "LLVM_22.1" in issues[0]
    # Demangled name should appear
    assert "std::string" in issues[0]


def test_check_package_abi_needed_lib_not_in_ldconfig():
    """NEEDED lib is not in ldconfig — report it as missing, not as symbol error."""
    bsdtar_list = "usr/lib/libfoo.so.1\n"
    nm_undef = "                 U _Z3barv@MYLIB_1.0\n"
    readelf_needed = " 0x1 (NEEDED)  Shared library: [libbar.so.1]\n"
    ldconfig_out = ""  # libbar.so.1 absent from ldconfig
    cppfilt_out = "bar()\n"

    def dispatcher(cmd, **_kw):
        tool = cmd[0]
        if tool == "bsdtar" and "-t" in cmd:
            return _mock_run(bsdtar_list)
        if tool == "bsdtar" and "-x" in cmd:
            dest = Path(cmd[cmd.index("-C") + 1])
            member = cmd[-1]
            extracted = dest / member
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"\x7fELF")
            return _mock_run("")
        if tool == "nm":
            return _mock_run(nm_undef)
        if tool == "readelf":
            return _mock_run(readelf_needed)
        if tool == "ldconfig":
            return _mock_run(ldconfig_out)
        if tool == "c++filt":
            return _mock_run(cppfilt_out)
        return _mock_run("")

    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_package_abi(Path("libfoo-1.0-1-x86_64.pkg.tar.zst"))

    # Should report both the missing lib and the unsatisfied symbol
    assert any("libbar.so.1" in i and "not found in ldconfig" in i for i in issues)


def test_check_package_abi_bsdtar_list_failure():
    """bsdtar failure on list → no issues, no crash."""
    with patch("sysforge.primitives.abi_check.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        issues = check_package_abi(Path("bad.pkg.tar.zst"))
    assert issues == []


# ---------------------------------------------------------------------------
# check_so_files — loose .so paths, no archive involved
# ---------------------------------------------------------------------------

def test_check_so_files_empty_list():
    """No .so paths → empty issues, no subprocess calls."""
    with patch("sysforge.primitives.abi_check.subprocess.run") as m:
        issues = check_so_files([])
    assert issues == []
    assert m.call_count == 0


def test_check_so_files_missing_symbol_on_installed_so(tmp_path):
    """check_so_files operating on a .so path directly (installed-file use case)."""
    so_path = tmp_path / "libclang-cpp.so.22.1"
    so_path.write_bytes(b"\x7fELF")

    missing_sym = "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_"
    nm_undef = f"                 U {missing_sym}@LLVM_22.1\n"
    readelf_needed = " 0x1 (NEEDED)  Shared library: [libLLVM.so.22.1]\n"
    ldconfig_out = "\tlibLLVM.so.22.1 (libc6,x86-64) => /usr/lib/libLLVM.so.22.1\n"
    nm_export = "0000001234 T _ZN4llvm5ValueE@@LLVM_22.1\n"
    cppfilt_out = "std::string::_M_assign(std::string const&)\n"

    def dispatcher(cmd, **_kw):
        tool = cmd[0]
        if tool == "nm":
            return _mock_run(nm_export if cmd[-1] == "/usr/lib/libLLVM.so.22.1" else nm_undef)
        if tool == "readelf":
            return _mock_run(readelf_needed)
        if tool == "ldconfig":
            return _mock_run(ldconfig_out)
        if tool == "c++filt":
            return _mock_run(cppfilt_out)
        return _mock_run("")

    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_so_files([so_path])

    assert len(issues) == 1
    assert "libclang-cpp.so.22.1" in issues[0]
    assert "LLVM_22.1" in issues[0]
    assert "std::string" in issues[0]


# ---------------------------------------------------------------------------
# Shim-library allowlist
# ---------------------------------------------------------------------------

def test_is_shim_lib_recognises_known_benign():
    assert _is_shim_lib(Path("/usr/lib/libnsl.so.1"))
    assert _is_shim_lib(Path("/usr/lib/libc_malloc_debug.so"))
    assert _is_shim_lib(Path("/usr/lib/libc_malloc_debug.so.0"))
    assert not _is_shim_lib(Path("/usr/lib/libc.so.6"))
    assert not _is_shim_lib(Path("/usr/lib/libLLVM.so.22.1"))


def test_check_so_files_skips_shim_libs(tmp_path):
    """Known-benign compat shims are skipped — no subprocess calls, no issues."""
    shim = tmp_path / "libnsl.so.1"
    shim.write_bytes(b"\x7fELF")

    with patch("sysforge.primitives.abi_check.subprocess.run") as m:
        issues = check_so_files([shim])

    assert issues == []
    # ldconfig -p is still called unconditionally, but no nm/readelf on the shim
    for call in m.call_args_list:
        cmd = call.args[0]
        assert cmd[0] != "nm", "nm must not be invoked on shim libs"
        assert cmd[0] != "readelf", "readelf must not be invoked on shim libs"
