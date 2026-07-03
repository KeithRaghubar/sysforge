"""
test_state_probe.py — sysforge state-integrity probe (doctor ``state`` axis).

BuildState, StageSentinel, and state-dir resolution are stubbed. Confirms the
probe is read-only (it never calls the recovering sentinel path or BuildState.save)
and maps failures / sentinel / drift onto the right severities.
"""
from __future__ import annotations

from pathlib import Path

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import state_probe


def _setup(monkeypatch, *, failures=None, packages=None, sentinel=None):
    failures = failures or {}
    packages = packages or {}

    class FakeBS:
        def __init__(self, _dir):
            pass

        def all_failures(self):
            return dict(failures)

        def all_packages(self):
            return dict(packages)

    class FakeSentinel:
        def __init__(self, _dir):
            pass

        def get_active(self):
            return sentinel

    import sysforge.pipeline.state as st
    import sysforge.primitives.build_state as bs_mod
    import sysforge.primitives.stage_sentinel as ss_mod

    monkeypatch.setattr(st, "resolve_state_dir", lambda d: (Path("/tmp/state"), "test"))
    monkeypatch.setattr(bs_mod, "BuildState", FakeBS)
    monkeypatch.setattr(ss_mod, "StageSentinel", FakeSentinel)


def test_clean_state_no_findings(monkeypatch):
    _setup(monkeypatch)
    assert state_probe.collect_state_findings(installed={}) == []


def test_build_failure_with_fix_cmd(monkeypatch):
    _setup(monkeypatch, failures={
        "gpu-burn-git": {
            "failed_at": "2026-05-30T00:00:00Z",
            "error": "Command 'makepkg' returned non-zero exit status 4.",
            "signature": "cuda:host-gcc-too-new",
            "fix_cmd": "NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15' makepkg",
        },
    })
    findings = state_probe.collect_state_findings(installed={})
    assert len(findings) == 1
    f = findings[0]
    assert f.check_id == "build_failure:gpu-burn-git"
    assert f.severity == diag.SEV_WARN
    assert f.fix_cmd == "NVCC_APPEND_FLAGS='-ccbin /usr/bin/g++-15' makepkg"
    assert "cuda:host-gcc-too-new" in f.message


def test_build_failure_without_fix_cmd_gets_log_hint(monkeypatch):
    _setup(monkeypatch, failures={
        "weston-git": {"failed_at": "2026-05-30T00:00:00Z", "error": "boom"},
    })
    f = state_probe.collect_state_findings(installed={})[0]
    assert f.fix_cmd is None
    assert "sysforge log weston-git" in f.remediation


def test_build_failure_remediation_offers_state_forget(monkeypatch):
    # A user who intentionally reverted to the repo package needs the manual
    # path out of the warning (1.2.0-F34) — with and without a fix_cmd.
    _setup(monkeypatch, failures={
        "mesa": {"failed_at": "2026-05-30T00:00:00Z", "error": "boom"},
        "gpu-burn-git": {
            "failed_at": "2026-05-30T00:00:00Z", "error": "boom",
            "fix_cmd": "makepkg -f",
        },
    })
    by_id = {f.check_id: f for f in state_probe.collect_state_findings(installed={})}
    assert "sysforge state forget mesa" in by_id["build_failure:mesa"].remediation
    assert ("sysforge state forget gpu-burn-git"
            in by_id["build_failure:gpu-burn-git"].remediation)


def test_stale_sentinel_is_error_with_recovery(monkeypatch):
    _setup(monkeypatch, sentinel={
        "stage": "toolchain",
        "started_at": "2026-05-31T22:57:21Z",
        "compiler": "llvm",
        "pgo": True,
        "recovery_cmd": "sudo pacman -S llvm llvm-libs clang lld",
    })
    findings = state_probe.collect_state_findings(installed={})
    sent = [f for f in findings if f.check_id == "stale_sentinel"]
    assert len(sent) == 1
    f = sent[0]
    assert f.severity == diag.SEV_ERROR
    assert f.fix_cmd == "sudo pacman -S llvm llvm-libs clang lld"
    assert "toolchain" in f.message
    # metadata surfaced inline
    assert "compiler=llvm" in f.message


def test_state_drift_zombies_is_info(monkeypatch):
    _setup(monkeypatch, packages={
        "stillthere": {"build_mode": "pacman"},
        "removed-pkg": {"build_mode": "pacman"},
    })
    findings = state_probe.collect_state_findings(installed={"stillthere": "1-1"})
    drift = [f for f in findings if f.check_id == "state_drift:zombies"]
    assert len(drift) == 1
    assert drift[0].severity == diag.SEV_INFO
    assert "removed-pkg" in drift[0].message


def test_no_drift_finding_when_installed_is_none(monkeypatch):
    # installed unknown (pacman fetch stubbed to fail) → no drift finding, no crash.
    _setup(monkeypatch, packages={"x": {}})
    import sysforge.primitives.pacman as pac
    monkeypatch.setattr(pac, "get_all_installed_packages",
                        lambda: (_ for _ in ()).throw(RuntimeError("no pacman")))
    findings = state_probe.collect_state_findings(installed=None)
    assert not any(f.check_id == "state_drift:zombies" for f in findings)
