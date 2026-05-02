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

from sysforge.primitives import dep_analysis as _da
from sysforge.primitives.dep_analysis import (
    _parse_ldconfig,
    check_soname_deps,
    check_makedep_runtime,
    run_dep_analysis,
    soname_available,
    soname_satisfied,
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
# soname_satisfied — pure predicate used by both check_soname_deps and doctor
# ---------------------------------------------------------------------------

def test_soname_satisfied_any_version_present():
    assert soname_satisfied("libcap.so", {"libcap.so.2", "libcap.so.2.69"})

def test_soname_satisfied_any_version_missing():
    assert not soname_satisfied("libmissing.so", {"libcap.so.2"})

def test_soname_satisfied_exact_major_present():
    assert soname_satisfied("libcap.so=2", {"libcap.so.2"})
    assert soname_satisfied("libcap.so=2", {"libcap.so.2.69"})

def test_soname_satisfied_exact_major_missing():
    assert not soname_satisfied("libcap.so=3", {"libcap.so.2"})

def test_soname_satisfied_multi_dot_version():
    # pacman emits e.g. libLLVM.so=22.1-64 — version "22.1", arch suffix "-64".
    assert soname_satisfied("libLLVM.so=22.1-64", {"libLLVM.so.22.1"})
    assert soname_satisfied("libLLVM.so=22.1-64", {"libLLVM.so.22.1.0.1"})
    assert not soname_satisfied("libLLVM.so=23.0-64", {"libLLVM.so.22.1"})

def test_soname_satisfied_arch_suffix_without_version():
    # libfoo.so-64 isn't a format pacman emits, but be defensive: must not
    # crash the regex.
    assert not soname_satisfied("libfoo.so-64", set())

def test_soname_satisfied_bare_base_matches():
    # ldconfig can list a bare "libfoo.so" entry (dev package); base name
    # (no =N) should accept it.
    assert soname_satisfied("libfoo.so", {"libfoo.so"})

def test_soname_satisfied_ignores_non_soname_entries():
    # Not a soname (regular depends entry) → False; caller skips.
    assert not soname_satisfied("glibc>=2.39", {"libc.so.6"})
    assert not soname_satisfied("pacman", {"libalpm.so.14"})


# ---------------------------------------------------------------------------
# soname_available — soname_satisfied + filesystem fallback
# ---------------------------------------------------------------------------

def test_soname_available_uses_ldconfig_when_present():
    # Filesystem fallback is patched to empty by the conftest autouse fixture.
    assert soname_available("libcap.so=2", {"libcap.so.2"})


def test_soname_available_returns_false_when_both_empty():
    assert not soname_available("libcap.so=2", set())


def test_soname_available_falls_back_to_filesystem(monkeypatch):
    """
    Stale /etc/ld.so.cache: soname is missing from ldconfig but the .so
    file is on disk. The filesystem fallback masks the cache lag so doctor
    doesn't surface a false positive.
    """
    monkeypatch.setattr(
        _da, "_filesystem_soname_set",
        lambda lib32=False: frozenset({"libcap.so.2", "libcap.so.2.69"}),
    )
    assert soname_available("libcap.so=2", set())


def test_soname_available_lib32_uses_lib32_filesystem_set(monkeypatch):
    seen: list[bool] = []

    def fake_fs(lib32=False):
        seen.append(lib32)
        return frozenset({"libfoo.so.3"}) if lib32 else frozenset()

    monkeypatch.setattr(_da, "_filesystem_soname_set", fake_fs)
    assert soname_available("libfoo.so=3", set(), lib32=True)
    assert not soname_available("libfoo.so=3", set(), lib32=False)
    assert seen == [True, False]


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

def _mock_run_success(*_args, **_kwargs):
    """Simulate a successful probe command."""
    class R:
        returncode = 0
        stdout = ""
        stderr = ""
    return R()


_GUESTFS_ROOT_UUID_OUTPUT = (
    "supermin: internal insmod virtio_pci.ko\n"
    "supermin: waiting another 1024000000 ns for root UUID to appear\n"
    "This usually means your kernel doesn't support virtio\n"
    "supermin: waiting another 2048000000 ns for root UUID to appear\n"
)


def _mock_run_fail_root_uuid(*_args, **_kwargs):
    """Simulate guestfish hanging on root UUID (missing virtio-scsi)."""
    class R:
        returncode = 1
        stdout = _GUESTFS_ROOT_UUID_OUTPUT
        stderr = ""
    return R()


def _mock_run_fail_generic(*_args, **_kwargs):
    """Simulate a generic guestfish failure without root UUID pattern."""
    class R:
        returncode = 1
        stdout = ""
        stderr = "libguestfs: error: something else went wrong\n"
    return R()


def _mock_run_timeout(*_args, **_kwargs):
    import subprocess
    e = subprocess.TimeoutExpired(cmd=["guestfish"], timeout=15)
    e.stdout = _GUESTFS_ROOT_UUID_OUTPUT
    e.stderr = ""
    raise e


def _mock_run_not_found(*_args, **_kwargs):
    raise FileNotFoundError("guestfish")


def test_makedep_probe_success():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_success,
    )
    assert findings == []


def test_makedep_probe_root_uuid_failure():
    """Root UUID failure should list missing kernel config options."""
    from unittest.mock import patch
    mock_config = {"CONFIG_SCSI_VIRTIO": "n", "CONFIG_EXT4_FS": "y",
                   "CONFIG_VIRTIO": "y", "CONFIG_VIRTIO_PCI": "y",
                   "CONFIG_VIRTIO_NET": "m"}
    with patch("sysforge.primitives.dep_analysis._parse_kernel_config", return_value=mock_config):
        findings = check_makedep_runtime(
            ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy",
            run_fn=_mock_run_fail_root_uuid,
        )
    assert len(findings) == 1
    assert "CONFIG_SCSI_VIRTIO" in findings[0][1]
    assert "CONFIG_EXT4_FS" not in findings[0][1]  # ext4 is enabled, not listed


def test_makedep_probe_timeout_with_diagnosis():
    """Timeout should still parse debug output for diagnosis."""
    from unittest.mock import patch
    mock_config = {"CONFIG_SCSI_VIRTIO": "n", "CONFIG_EXT4_FS": "y",
                   "CONFIG_VIRTIO": "y", "CONFIG_VIRTIO_PCI": "y",
                   "CONFIG_VIRTIO_NET": "y"}
    with patch("sysforge.primitives.dep_analysis._parse_kernel_config", return_value=mock_config):
        findings = check_makedep_runtime(
            ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy",
            run_fn=_mock_run_timeout,
        )
    assert len(findings) == 1
    assert "CONFIG_SCSI_VIRTIO" in findings[0][1]


def test_makedep_probe_generic_failure():
    """Non-root-UUID failure should give a generic message."""
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy",
        run_fn=_mock_run_fail_generic,
    )
    assert len(findings) == 1
    assert "probe failed" in findings[0][1]
    assert "LIBGUESTFS_DEBUG" in findings[0][1]


def test_makedep_probe_not_installed():
    findings = check_makedep_runtime(
        ["libguestfs"], DEFAULT_CONFIG, pkgname="ventoy", run_fn=_mock_run_not_found,
    )
    assert findings == []


def test_makedep_probe_skips_unprobed():
    """Makedepends not in _PROBED_MAKEDEPS are silently skipped."""
    findings = check_makedep_runtime(
        ["git", "python", "make"], DEFAULT_CONFIG, run_fn=_mock_run_fail_root_uuid,
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
        pkgmeta, DEFAULT_CONFIG, ldconfig_fn=mock_ldconfig,
        run_fn=_mock_run_fail_generic,
    )
    assert len(findings) == 1
    assert findings[0][0] == "libguestfs"
