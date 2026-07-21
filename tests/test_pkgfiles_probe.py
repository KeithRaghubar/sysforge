# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for pkgfiles_probe — the doctor `integrity` axis (pacman -Qkk)."""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import pkgfiles_probe


def _proc(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _patch(monkeypatch, proc):
    monkeypatch.setattr(pkgfiles_probe, "_run", lambda packages: proc)


def test_backup_file_discrepancy_is_info(monkeypatch):
    _patch(monkeypatch, _proc(stdout=(
        "backup file: pacman: /etc/pacman.conf (Modification time mismatch)\n"
        "backup file: pacman: /etc/pacman.conf (Size mismatch)\n"
        "backup file: pacman: /etc/pacman.conf (SHA256 checksum mismatch)\n"
        "pacman: 426 total files, 0 altered files\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == diag.SEV_INFO
    assert f.check_id == "integrity_backup_edited"
    assert "/etc/pacman.conf" in f.message


def test_non_backup_missing_is_error(monkeypatch):
    _patch(monkeypatch, _proc(stderr=(
        "warning: coreutils: /usr/bin/ls (No such file or directory)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert len(findings) == 1
    assert findings[0].severity == diag.SEV_ERROR
    assert findings[0].check_id == "integrity_missing"


def test_non_backup_content_change_is_warn_single_finding(monkeypatch):
    _patch(monkeypatch, _proc(stdout=(
        "openssl: /usr/lib/libcrypto.so (Modification time mismatch)\n"
        "openssl: /usr/lib/libcrypto.so (Size mismatch)\n"
        "openssl: /usr/lib/libcrypto.so (SHA256 checksum mismatch)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == diag.SEV_WARN
    assert f.check_id == "integrity_altered"
    # Reasons grouped into the single message.
    assert "Size mismatch" in f.message and "SHA256" in f.message


def test_non_backup_mtime_only_is_info(monkeypatch):
    _patch(monkeypatch, _proc(stdout=(
        "foo: /usr/share/foo/data (Modification time mismatch)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert len(findings) == 1
    assert findings[0].severity == diag.SEV_INFO
    assert findings[0].check_id == "integrity_altered"


def test_summary_line_ignored(monkeypatch):
    _patch(monkeypatch, _proc(stdout=(
        "pacman: 426 total files, 0 altered files\n"
    ), returncode=0))
    assert pkgfiles_probe.collect_integrity_findings() == []


def test_stderr_and_stdout_both_parsed(monkeypatch):
    _patch(monkeypatch, _proc(
        stdout="pkgA: /usr/lib/a.so (Size mismatch)\n",
        stderr="warning: pkgB: /usr/bin/b (File type mismatch)\n",
        returncode=1,
    ))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert {f.check_id for f in findings} == {"integrity_altered"}
    assert len(findings) == 2


def test_clean_package_no_findings(monkeypatch):
    _patch(monkeypatch, _proc(stdout="pkg: 10 total files, 0 altered files\n",
                              returncode=0))
    assert pkgfiles_probe.collect_integrity_findings() == []


def test_run_unavailable_yields_single_warn(monkeypatch):
    _patch(monkeypatch, None)
    findings = pkgfiles_probe.collect_integrity_findings()
    assert len(findings) == 1
    assert findings[0].severity == diag.SEV_WARN
    assert findings[0].check_id == "integrity_unavailable"


def test_access_error_only_path_is_partial_coverage_not_warn(monkeypatch):
    _patch(monkeypatch, _proc(stderr=(
        "warning: cups: /etc/cups/cupsd.conf (failed to calculate SHA256 checksum)\n"
        "warning: shadow: /etc/shadow (Permission denied)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    # No per-file altered warnings for unreadable files.
    assert all(f.check_id != "integrity_altered" for f in findings)
    advisory = [f for f in findings if f.check_id == "integrity_partial_coverage"]
    assert len(advisory) == 1
    assert advisory[0].severity == diag.SEV_INFO
    assert "2" in advisory[0].message  # two unreadable files counted


def test_partial_readable_path_keeps_real_drift_as_warn(monkeypatch):
    # stat succeeded (Size mismatch) but read failed (hash) — real drift stays.
    _patch(monkeypatch, _proc(stdout=(
        "openssl: /usr/lib/libcrypto.so (Size mismatch)\n"
        "openssl: /usr/lib/libcrypto.so (failed to calculate SHA256 checksum)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    altered = [f for f in findings if f.check_id == "integrity_altered"]
    assert len(altered) == 1
    assert altered[0].severity == diag.SEV_WARN
    assert "Size mismatch" in altered[0].message
    # The access-error reason is dropped from the message.
    assert "failed to calculate" not in altered[0].message
    # A partially-readable path is NOT counted as unreadable.
    assert not any(f.check_id == "integrity_partial_coverage" for f in findings)


def test_no_advisory_when_all_readable(monkeypatch):
    _patch(monkeypatch, _proc(stdout=(
        "openssl: /usr/lib/libcrypto.so (Size mismatch)\n"
    ), returncode=1))
    findings = pkgfiles_probe.collect_integrity_findings()
    assert not any(f.check_id == "integrity_partial_coverage" for f in findings)


def test_packages_forwarded_to_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        pkgfiles_probe, "_run",
        lambda packages: (seen.setdefault("pkgs", packages), _proc(returncode=0))[1],
    )
    pkgfiles_probe.collect_integrity_findings(["pacman"])
    assert seen["pkgs"] == ["pacman"]
