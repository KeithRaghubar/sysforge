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
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.abi_check import (
    _build_ldconfig_map,
    _demangle,
    _exported_versioned,
    _extract_sos,
    _is_optional_llvm_target_init,
    _is_shim_lib,
    _list_sos_in_pkg,
    _parse_nm_exports,
    _parse_nm_undefined,
    _parse_verneed,
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


def test_list_sos_passes_archive_with_f_and_closes_stdin():
    """Regression: bsdtar must get -f <path> (path is the archive, not a member
    pattern) and stdin redirected to DEVNULL. Without -f, bsdtar reads the
    archive from stdin and blocks forever on the controlling TTY (Gate 2 hang)."""
    pkg = Path("pkg.tar.zst")
    mock = MagicMock(return_value=_mock_run("usr/lib/libfoo.so.1\n"))
    with patch("sysforge.primitives.abi_check.subprocess.run", mock):
        _list_sos_in_pkg(pkg)

    argv = mock.call_args.args[0]
    assert "bsdtar" == argv[0]
    # The package path must be the archive operand, immediately preceded by -f
    # (whether spelled "-tf" or "-t -f").
    assert str(pkg) in argv
    path_idx = argv.index(str(pkg))
    preceding = argv[path_idx - 1]
    assert preceding == "-f" or (preceding.startswith("-") and "f" in preceding), (
        f"package path not introduced by -f: {argv!r}"
    )
    # stdin must be closed so a future flag mistake errors instead of hanging.
    assert mock.call_args.kwargs.get("stdin") == subprocess.DEVNULL


# ---------------------------------------------------------------------------
# _extract_sos
# ---------------------------------------------------------------------------

def test_extract_sos_creates_missing_dest_before_bsdtar(tmp_path):
    """Regression: _extract_sos must create dest before bsdtar -C (which fails
    'could not chdir to <dest>' on a missing dir). scan_abi_hazards passes a
    per-package subdir that does not exist, so Gate 2 silently extracted nothing
    and passed vacuously — letting the PGO Gate-3 brick slip past pre-install
    detection. check_package_abi was unaffected (it passes an existing tmpdir).
    """
    dest = tmp_path / "sub" / "clang.pkg.tar"   # nested, does NOT exist yet
    seen = {}

    def fake_run(cmd, **kwargs):
        # The dir must already exist by the time bsdtar -C runs.
        seen["dest_exists"] = dest.is_dir()
        return MagicMock(returncode=0, stderr="")

    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=fake_run):
        _extract_sos(Path("clang.pkg.tar"), ["usr/lib/libclang-cpp.so.22.1"], dest)

    assert seen["dest_exists"] is True


def test_extract_sos_empty_members_no_subprocess(tmp_path):
    """No .so members → no bsdtar call, no dir created (early return)."""
    dest = tmp_path / "sub"
    with patch("sysforge.primitives.abi_check.subprocess.run") as mock:
        out = _extract_sos(Path("pkg.pkg.tar"), [], dest)
    assert out == []
    mock.assert_not_called()


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


# ---------------------------------------------------------------------------
# Precision: nm parsers
# ---------------------------------------------------------------------------

def test_parse_nm_exports_captures_default_and_nondefault():
    """Both sym@@VER (default) and sym@VER (non-default back-compat) are exports."""
    nm_out = (
        "0000001234 T realpath@@GLIBC_2.3\n"      # default
        "0000001230 T realpath@GLIBC_2.2.5\n"     # non-default back-compat
        "0000005678 W advance@GLIBC_2.2.5\n"      # weak defined, non-default
        "                 U __undef@GLIBC_2.0\n"   # undefined — not an export
        "0000009999 T plain_unversioned\n"        # no version — skipped
    )
    exports = _parse_nm_exports(nm_out)
    assert ("realpath", "GLIBC_2.3") in exports
    assert ("realpath", "GLIBC_2.2.5") in exports     # the key non-default capture
    assert ("advance", "GLIBC_2.2.5") in exports
    assert ("__undef", "GLIBC_2.0") not in exports
    assert not any(s == "plain_unversioned" for s, _ in exports)


def test_parse_nm_undefined_strong_versioned_only():
    """Only strong (U) versioned undefined refs; weak/defined/unversioned excluded."""
    nm_out = (
        "                 U needsym@LLVM_22.1\n"     # strong undefined, versioned
        "                 U plain_undef\n"           # strong undefined, no version
        "                 w weak_undef@GLIBC_2.0\n"  # weak undefined — excluded
        "0000001234 T defined@@LLVM_22.1\n"          # defined — excluded
    )
    undef = _parse_nm_undefined(nm_out)
    assert undef == {("needsym", "LLVM_22.1")}


# ---------------------------------------------------------------------------
# Precision: Verneed (.gnu.version_r) parsing
# ---------------------------------------------------------------------------

def test_parse_verneed_binds_versions_to_sonames():
    version_info = (
        "Version symbols section '.gnu.version' contains 5 entries:\n"
        "  ignored: Name: SHOULD_NOT_APPEAR\n"
        "\n"
        "Version needs section '.gnu.version_r' contains 2 entries:\n"
        " Addr: 0x0  Offset: 0x0  Link: 4 (.dynstr)\n"
        "  000000: Version: 1  File: libLLVM.so.22.1  Cnt: 1\n"
        "  0x0010:   Name: LLVM_22.1  Flags: none  Version: 12\n"
        "  000010: Version: 1  File: libc.so.6  Cnt: 2\n"
        "  0x0020:   Name: GLIBC_2.2.5  Flags: none  Version: 13\n"
        "  0x0030:   Name: GLIBC_2.3  Flags: none  Version: 14\n"
    )
    m = _parse_verneed(version_info)
    assert m["LLVM_22.1"] == {"libLLVM.so.22.1"}
    assert m["GLIBC_2.2.5"] == {"libc.so.6"}
    assert m["GLIBC_2.3"] == {"libc.so.6"}
    # Names outside the version-needs section must not leak in.
    assert "SHOULD_NOT_APPEAR" not in m


def test_parse_verneed_empty_when_no_section():
    assert _parse_verneed("no verneed here\n") == {}


# ---------------------------------------------------------------------------
# Precision: LLVM optional target-init recognition
# ---------------------------------------------------------------------------

def test_is_optional_llvm_target_init_matches_target_entry_points():
    for sym in (
        "LLVMInitializeAMDGPUTarget",
        "LLVMInitializeAMDGPUTargetInfo",
        "LLVMInitializeAMDGPUTargetMC",
        "LLVMInitializeAMDGPUAsmParser",
        "LLVMInitializeAMDGPUAsmPrinter",
        "LLVMInitializeAMDGPUDisassembler",
        "LLVMInitializeAArch64AsmPrinter",
    ):
        assert _is_optional_llvm_target_init(sym, "LLVM_22.1"), sym


def test_is_optional_llvm_target_init_rejects_real_breaks_and_other_namespaces():
    # A C++ stdlib symbol mis-bound to LLVM_* is a genuine break, not optional.
    assert not _is_optional_llvm_target_init(
        "_ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE9_M_assignERKS4_",
        "LLVM_22.1",
    )
    # Right name shape but wrong version namespace → not demoted.
    assert not _is_optional_llvm_target_init("LLVMInitializeAMDGPUTarget", "GLIBC_2.3")
    # LLVMInitializeCore is always present (not a target backend) → not optional.
    assert not _is_optional_llvm_target_init("LLVMInitializeCore", "LLVM_22.1")


# ---------------------------------------------------------------------------
# Precision: check_so_files end-to-end (mocked subprocess)
# ---------------------------------------------------------------------------

_ELF64_HEADER = "ELF Header:\n  Class:                             ELF64\n"


def _precision_dispatcher(*, so_undef, needed, ldconfig, lib_exports,
                          verinfo="", cppfilt=""):
    """Build a subprocess.run side_effect for a single checked .so.

    lib_exports maps an on-disk lib path → its `nm -D` output. Any nm call on a
    path not in lib_exports returns the checked .so's own undefined output.
    """
    def dispatcher(cmd, **_kw):
        tool = cmd[0]
        if tool == "nm":
            return _mock_run(lib_exports.get(cmd[-1], so_undef))
        if tool == "readelf":
            if "--version-info" in cmd:
                return _mock_run(verinfo)
            if "-h" in cmd:
                return _mock_run(_ELF64_HEADER)
            return _mock_run(needed)  # -d
        if tool == "ldconfig":
            return _mock_run(ldconfig)
        if tool == "c++filt":
            return _mock_run(cppfilt or "\n".join(cmd[1:]))
        return _mock_run("")
    return dispatcher


def test_check_so_files_nondefault_export_satisfies(tmp_path):
    """A requirement sym@VER satisfied by a non-default sym@VER export → clean."""
    so = tmp_path / "libfoo.so.1"
    so.write_bytes(b"\x7fELF")
    dispatcher = _precision_dispatcher(
        so_undef="                 U realpath@GLIBC_2.2.5\n",
        needed=" 0x1 (NEEDED)  Shared library: [libc.so.6]\n",
        ldconfig="\tlibc.so.6 (libc6,x86-64) => /usr/lib/libc.so.6\n",
        lib_exports={
            # libc exports realpath ONLY as a non-default single-@ version.
            "/usr/lib/libc.so.6": (
                "0000001234 T realpath@@GLIBC_2.3\n"
                "0000001230 T realpath@GLIBC_2.2.5\n"
            ),
        },
        verinfo=(
            "Version needs section '.gnu.version_r' contains 1 entries:\n"
            "  000000: Version: 1  File: libc.so.6  Cnt: 1\n"
            "  0x0010:   Name: GLIBC_2.2.5  Flags: none  Version: 2\n"
        ),
    )
    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_so_files([so])
    assert issues == []


def test_check_so_files_host_provided_version_not_flagged(tmp_path):
    """A version bound (Verneed) to no NEEDED lib is host/loader-provided → skipped."""
    so = tmp_path / "plugin.so"
    so.write_bytes(b"\x7fELF")
    dispatcher = _precision_dispatcher(
        so_undef="                 U host_symbol@APP_1.0\n",
        needed=" 0x1 (NEEDED)  Shared library: [libsupport.so.1]\n",
        ldconfig="\tlibsupport.so.1 (libc6,x86-64) => /usr/lib/libsupport.so.1\n",
        lib_exports={"/usr/lib/libsupport.so.1": "0000001234 T other@@SUP_1.0\n"},
        # APP_1.0 is provided by the executable that dlopens this plugin, not by
        # any NEEDED lib — Verneed binds it to a non-NEEDED file.
        verinfo=(
            "Version needs section '.gnu.version_r' contains 1 entries:\n"
            "  000000: Version: 1  File: the_app  Cnt: 1\n"
            "  0x0010:   Name: APP_1.0  Flags: none  Version: 2\n"
        ),
    )
    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_so_files([so])
    assert issues == []


def test_check_so_files_llvm_target_init_demoted_to_benign(tmp_path):
    """An absent LLVM target-init symbol (version present) is demoted, not flagged."""
    so = tmp_path / "radeonsi_drv_video.so"
    so.write_bytes(b"\x7fELF")
    dispatcher = _precision_dispatcher(
        so_undef="                 U LLVMInitializeAMDGPUTarget@LLVM_22.1\n",
        needed=" 0x1 (NEEDED)  Shared library: [libLLVM.so.22.1]\n",
        ldconfig="\tlibLLVM.so.22.1 (libc6,x86-64) => /usr/lib/libLLVM.so.22.1\n",
        # libLLVM defines LLVM_22.1 (X86 target present) but NOT the AMDGPU init.
        lib_exports={"/usr/lib/libLLVM.so.22.1": "0461baa0 T LLVMInitializeX86Target@@LLVM_22.1\n"},
        verinfo=(
            "Version needs section '.gnu.version_r' contains 1 entries:\n"
            "  000000: Version: 1  File: libLLVM.so.22.1  Cnt: 1\n"
            "  0x0010:   Name: LLVM_22.1  Flags: none  Version: 2\n"
        ),
    )
    benign: list[str] = []
    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_so_files([so], benign_sink=benign)
    assert issues == []
    assert len(benign) == 1
    assert "LLVMInitializeAMDGPUTarget@LLVM_22.1" in benign[0]


def test_check_so_files_genuine_drift_still_flagged(tmp_path):
    """A required version absent from the bound lib (soname/version skew) is hard."""
    so = tmp_path / "libconsumer.so.1"
    so.write_bytes(b"\x7fELF")
    dispatcher = _precision_dispatcher(
        so_undef="                 U _Z3barv@MYLIB_2.0\n",
        needed=" 0x1 (NEEDED)  Shared library: [libbar.so.1]\n",
        ldconfig="\tlibbar.so.1 (libc6,x86-64) => /usr/lib/libbar.so.1\n",
        # libbar only provides MYLIB_1.0 — the required MYLIB_2.0 node is absent.
        lib_exports={"/usr/lib/libbar.so.1": "0000001234 T _Z3barv@@MYLIB_1.0\n"},
        verinfo=(
            "Version needs section '.gnu.version_r' contains 1 entries:\n"
            "  000000: Version: 1  File: libbar.so.1  Cnt: 1\n"
            "  0x0010:   Name: MYLIB_2.0  Flags: none  Version: 2\n"
        ),
        cppfilt="bar()\n",
    )
    with patch("sysforge.primitives.abi_check.subprocess.run", side_effect=dispatcher):
        issues = check_so_files([so])
    assert len(issues) == 1
    assert "MYLIB_2.0" in issues[0]
    assert "_Z3barv" in issues[0]
