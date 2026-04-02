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

_DATA = Path(__file__).parent / "data"

from sysforge.primitives.dep_analysis import (
    _parse_ldconfig,
    check_soname_deps,
    check_makedep_runtime,
    run_dep_analysis,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

LDCONFIG_SAMPLE = (_DATA / "ldconfig_sample.txt").read_text()

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


# ---------------------------------------------------------------------------
# check_makedep_runtime
# ---------------------------------------------------------------------------

def _mock_run_success(*args, **kwargs):
    """Simulate a successful probe command."""
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


def _mock_run_fail(*args, **kwargs):
    """Simulate a probe command that exits non-zero."""
    class R:
        returncode = 1
        stdout = ""
        stderr = "libguestfs: error: appliance boot failed\n"
    return R()


def _mock_run_timeout(*args, **kwargs):
    import subprocess
    raise subprocess.TimeoutExpired(cmd=args[0], timeout=15)


def _mock_run_not_found(*args, **kwargs):
    raise FileNotFoundError("guestfish")


def test_makedep_probe_success():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_success,
    )
    assert findings == []


def test_makedep_probe_failure():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_fail,
    )
    assert len(findings) == 1
    assert findings[0][0] == "libguestfs"
    assert "probe failed" in findings[0][1]


def test_makedep_probe_timeout():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_timeout,
    )
    assert len(findings) == 1
    assert "timed out" in findings[0][1]


def test_makedep_probe_not_installed():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_not_found,
    )
    assert findings == []


def test_makedep_probe_skips_unprobed():
    """Makedepends not in _MAKEDEP_PROBES are silently skipped."""
    findings = check_makedep_runtime(
        ["git", "python", "make"], DEFAULT_CONFIG, run_fn=_mock_run_fail,
    )
    assert findings == []


def test_makedep_probe_version_constraint():
    """Version constraints like libguestfs>=1.50 are stripped before lookup."""
    findings = check_makedep_runtime(
        ["libguestfs>=1.50"], DEFAULT_CONFIG, pkgname="pkg", run_fn=_mock_run_success,
    )
    assert findings == []


def test_run_dep_analysis_includes_makedep_probes():
    """run_dep_analysis runs makedep probes alongside soname checks."""
    pkgmeta = {
        "globals": {
            "pkgname": "ventoy",
            "depends": [],
            "makedepends": ["libguestfs"],
        }
    }
    findings = run_dep_analysis(
        pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig, run_fn=_mock_run_fail,
    )
    assert len(findings) == 1
    assert findings[0][0] == "libguestfs"
