"""
test_system_probe.py — pacman / system-integrity probe (doctor ``pacman`` axis).

All external commands and filesystem roots are stubbed; no real pacman or /etc
access. Confirms the probe is read-only (never builds a sync command) and maps
each check onto the right severity.
"""
from __future__ import annotations

from types import SimpleNamespace

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import system_probe


def _proc(stdout="", stderr="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


# --- pacman -Dk -------------------------------------------------------------

def test_db_consistency_clean_when_rc_zero(monkeypatch):
    monkeypatch.setattr(system_probe, "_run", lambda cmd: _proc(rc=0))
    assert system_probe._check_db_consistency() == []


def test_db_consistency_reports_missing_dep(monkeypatch):
    err = "error: missing 'foolib' dependency for 'barpkg'\n"
    monkeypatch.setattr(system_probe, "_run", lambda cmd: _proc(stderr=err, rc=1))
    findings = system_probe._check_db_consistency()
    assert len(findings) == 1
    assert findings[0].severity == diag.SEV_ERROR
    assert findings[0].check_id == "pacman_db_inconsistent"
    assert "foolib" in findings[0].message


def test_db_consistency_never_passes_sync_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(system_probe, "_run",
                        lambda cmd: seen.append(cmd) or _proc(rc=0))
    system_probe.collect_system_findings()
    for cmd in seen:
        assert "-Sy" not in cmd and "-Syu" not in cmd, f"sync flag in {cmd}"
        assert not any("y" in tok and tok.startswith("-S") for tok in cmd)


# --- db.lck -----------------------------------------------------------------

def test_stale_lock_detected(monkeypatch, tmp_path):
    lock = tmp_path / "db.lck"
    lock.write_text("")
    monkeypatch.setattr(system_probe, "_PACMAN_DB_LOCK", lock)
    findings = system_probe._check_stale_lock()
    assert len(findings) == 1
    assert findings[0].check_id == "pacman_db_locked"
    assert findings[0].severity == diag.SEV_WARN


def test_stale_lock_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(system_probe, "_PACMAN_DB_LOCK", tmp_path / "nope.lck")
    assert system_probe._check_stale_lock() == []


# --- .pacnew / .pacsave -----------------------------------------------------

def test_pacfiles_scan(monkeypatch, tmp_path):
    # Both pacfiles have a live base file → mergeable, one pacnew_unmerged finding.
    (tmp_path / "a").mkdir()
    (tmp_path / "pacman.conf").write_text("")
    (tmp_path / "pacman.conf.pacnew").write_text("")
    (tmp_path / "a" / "thing.conf").write_text("")
    (tmp_path / "a" / "thing.conf.pacsave").write_text("")
    (tmp_path / "a" / "normal.conf").write_text("")
    monkeypatch.setattr(system_probe, "_ETC", tmp_path)
    findings = system_probe._check_pacfiles()
    assert len(findings) == 1
    assert findings[0].check_id == "pacnew_unmerged"
    assert findings[0].severity == diag.SEV_WARN
    assert "2 unmerged" in findings[0].message


def test_pacfiles_orphaned_pacsave_not_advised_pacdiff(monkeypatch, tmp_path):
    # .pacsave whose base file is gone (removed package) → pacdiff no-ops on it,
    # so it must be reported as orphaned with manual-removal advice, not pacdiff
    # (B10 dead-end-advice regression).
    (tmp_path / "web2c").mkdir()
    (tmp_path / "web2c" / "fmtutil.cnf.pacsave").write_text("")
    monkeypatch.setattr(system_probe, "_ETC", tmp_path)
    findings = system_probe._check_pacfiles()
    assert len(findings) == 1
    assert findings[0].check_id == "pacsave_orphaned"
    assert "pacdiff" not in findings[0].remediation


def test_pacfiles_mixed_mergeable_and_orphaned(monkeypatch, tmp_path):
    (tmp_path / "pacman.conf").write_text("")
    (tmp_path / "pacman.conf.pacnew").write_text("")   # mergeable
    (tmp_path / "gone.conf.pacsave").write_text("")    # orphaned (no base)
    monkeypatch.setattr(system_probe, "_ETC", tmp_path)
    ids = {f.check_id for f in system_probe._check_pacfiles()}
    assert ids == {"pacnew_unmerged", "pacsave_orphaned"}


def test_pacfiles_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(system_probe, "_ETC", tmp_path)
    assert system_probe._check_pacfiles() == []


# --- orphans ----------------------------------------------------------------

def test_orphans_listed_as_info(monkeypatch):
    monkeypatch.setattr(system_probe, "_run",
                        lambda cmd: _proc(stdout="foo\nbar\n", rc=0))
    findings = system_probe._check_orphans()
    assert len(findings) == 1
    assert findings[0].severity == diag.SEV_INFO
    assert findings[0].check_id == "orphan_packages"


def test_orphans_empty_query_rc1(monkeypatch):
    # pacman exits 1 with empty stdout when there are no orphans.
    monkeypatch.setattr(system_probe, "_run", lambda cmd: _proc(stdout="", rc=1))
    assert system_probe._check_orphans() == []


def test_missing_tool_yields_no_findings(monkeypatch):
    monkeypatch.setattr(system_probe, "_run", lambda cmd: None)
    monkeypatch.setattr(system_probe, "_PACMAN_DB_LOCK",
                        system_probe.Path("/nonexistent/db.lck"))
    monkeypatch.setattr(system_probe, "_ETC",
                        system_probe.Path("/nonexistent/etc"))
    assert system_probe.collect_system_findings() == []
