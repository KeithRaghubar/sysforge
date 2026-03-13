"""
test_dep_analysis.py — unit tests for pre-build soname dependency analysis.

All system calls (ldconfig) are replaced with an injectable callable so no
real system tools are required.

Covers:
  _parse_ldconfig     — soname extraction from ldconfig -p output
  check_soname_deps   — present (any version), present (exact major),
                        missing (any), missing (exact major), non-.so skipped,
                        multiple entries, abort behaviour
  run_dep_analysis    — no soname entries skipped, findings returned,
                        split pkgname, makedepends not checked
"""
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.dep_analysis import (
    _parse_ldconfig,
    check_soname_deps,
    run_dep_analysis,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

LDCONFIG_SAMPLE = """\
\tlibcap.so.2 (libc6,x86-64) => /usr/lib/libcap.so.2
\tlibcap.so.2.69 (libc6,x86-64) => /usr/lib/libcap.so.2.69
\tlibncursesw.so.6 (libc6,x86-64) => /usr/lib/libncursesw.so.6
\tlibz.so.1 (libc6,x86-64) => /usr/lib/libz.so.1
\tlibc.so.6 (libc6,x86-64) => /usr/lib/libc.so.6
"""

def mock_ldconfig():
    return LDCONFIG_SAMPLE

DEFAULT_CONFIG = {}

def abort_config():
    return {"failure_handling": {"abi_mismatch": "abort"}}


# ---------------------------------------------------------------------------
# _parse_ldconfig
# ---------------------------------------------------------------------------

def test_parse_ldconfig_extracts_sonames():
    result = _parse_ldconfig(LDCONFIG_SAMPLE)
    assert "libcap.so.2" in result
    assert "libncursesw.so.6" in result
    assert "libz.so.1" in result

def test_parse_ldconfig_skips_lines_without_arrow():
    output = "some header line\n\tlibfoo.so.1 (libc6) => /usr/lib/libfoo.so.1\n"
    result = _parse_ldconfig(output)
    assert "libfoo.so.1" in result
    assert len(result) == 1

def test_parse_ldconfig_empty():
    assert _parse_ldconfig("") == set()


# ---------------------------------------------------------------------------
# check_soname_deps
# ---------------------------------------------------------------------------

def test_soname_present_any_version():
    assert check_soname_deps(["libcap.so"], DEFAULT_CONFIG,
                              ldconfig_fn=mock_ldconfig) == []

def test_soname_present_exact_major():
    assert check_soname_deps(["libcap.so=2"], DEFAULT_CONFIG,
                              ldconfig_fn=mock_ldconfig) == []

def test_soname_present_ncurses():
    assert check_soname_deps(["libncursesw.so=6"], DEFAULT_CONFIG,
                              ldconfig_fn=mock_ldconfig) == []

def test_soname_missing_any_version():
    findings = check_soname_deps(["libmissing.so"], DEFAULT_CONFIG,
                                  ldconfig_fn=mock_ldconfig)
    assert len(findings) == 1
    assert "libmissing.so" in findings[0][0]

def test_soname_missing_exact_major():
    # libcap.so=3 — only .so.2 is present
    findings = check_soname_deps(["libcap.so=3"], DEFAULT_CONFIG,
                                  ldconfig_fn=mock_ldconfig)
    assert len(findings) == 1
    assert "libcap.so=3" in findings[0][0]

def test_soname_wrong_major_flagged():
    findings = check_soname_deps(["libz.so=2"], DEFAULT_CONFIG,
                                  ldconfig_fn=mock_ldconfig)
    assert len(findings) == 1  # only .so.1 present

def test_soname_non_so_entries_skipped():
    findings = check_soname_deps(["git", "cmake", "libcap.so=2"],
                                  DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig)
    assert findings == []

def test_soname_multiple_entries_one_missing():
    findings = check_soname_deps(
        ["libcap.so=2", "libncursesw.so=6", "libmissing.so=1"],
        DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig,
    )
    assert len(findings) == 1
    assert "libmissing.so=1" in findings[0][0]

def test_soname_missing_with_abort_raises():
    with pytest.raises(RuntimeError, match="abi_mismatch"):
        check_soname_deps(["libmissing.so"], abort_config(),
                          ldconfig_fn=mock_ldconfig)

def test_soname_all_missing_abort_raises_on_first():
    # abort stops at first failure
    with pytest.raises(RuntimeError):
        check_soname_deps(["libmissing.so", "libalsomissing.so"],
                          abort_config(), ldconfig_fn=mock_ldconfig)

def test_soname_empty_depends():
    assert check_soname_deps([], DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig) == []


# ---------------------------------------------------------------------------
# run_dep_analysis
# ---------------------------------------------------------------------------

def test_run_no_soname_entries_skips():
    pkgmeta = {"globals": {"pkgname": "htop", "depends": ["libcap", "ncurses"]}}
    findings = run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig)
    assert findings == []

def test_run_soname_all_present():
    pkgmeta = {
        "globals": {
            "pkgname": "htop",
            "depends": ["libcap", "libcap.so=2", "ncurses", "libncursesw.so=6"],
        }
    }
    assert run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig) == []

def test_run_soname_missing_returns_finding():
    pkgmeta = {
        "globals": {
            "pkgname": "mypkg",
            "depends": ["libmissing.so=1"],
        }
    }
    findings = run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig)
    assert len(findings) == 1

def test_run_no_depends_key():
    pkgmeta = {"globals": {"pkgname": "minimal"}}
    assert run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig) == []

def test_run_split_pkgname():
    pkgmeta = {
        "globals": {
            "pkgname": ["lib32-llvm", "lib32-llvm-libs"],
            "depends": ["libcap.so=2"],
        }
    }
    # Should not raise; uses first entry for logging
    assert run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig) == []

def test_run_makedepends_not_checked():
    # soname entry in makedepends only — should not be checked
    pkgmeta = {
        "globals": {
            "pkgname": "mypkg",
            "depends": [],
            "makedepends": ["libmissing.so=9"],
        }
    }
    assert run_dep_analysis(pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig) == []

def test_run_abort_propagates():
    pkgmeta = {
        "globals": {
            "pkgname": "mypkg",
            "depends": ["libmissing.so=1"],
        }
    }
    with pytest.raises(RuntimeError, match="abi_mismatch"):
        run_dep_analysis(pkgmeta, abort_config(), ldconfig_fn=mock_ldconfig)
