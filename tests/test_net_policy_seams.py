# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""The source freeze, enforced at each egress seam (3.0.0-F2)."""

from pathlib import Path

import pytest

from sysforge.primitives import aur, build_prep, git_ops, source_sync, vcs_pkgver
from sysforge.primitives.net_policy import NetworkFrozen
from sysforge.update_common import _SYNC_BLOCKING_STATUSES


def test_aur_clone_denied_when_frozen(frozen_policy, tmp_path, monkeypatch):
    """No git process may be spawned — assert on the runner, not just the raise.

    A test that only checks the exception would pass while the clone still
    happened, which is the entire failure this gate exists to prevent.
    """
    def _boom(*a, **kw):
        raise AssertionError("git must not run under freeze")

    monkeypatch.setattr(aur.subprocess, "run", _boom)
    with pytest.raises(NetworkFrozen) as exc:
        aur.aur_clone("mesa", tmp_path / "mesa")
    assert "mesa" in str(exc.value)


def test_aur_clone_allowed_when_thawed(frozen_policy, tmp_path, monkeypatch):
    frozen_policy(thawed=["mesa"])
    calls = []

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        calls.append(cmd)
        dest = tmp_path / "mesa"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "PKGBUILD").write_text("pkgname=mesa\n")
        return _OK()

    monkeypatch.setattr(aur.subprocess, "run", _run)
    aur.aur_clone("mesa", tmp_path / "mesa")
    assert calls and calls[0][:2] == ["git", "clone"]


def test_aur_clone_unaffected_when_not_frozen(tmp_path, monkeypatch):
    """No fixture — the default permissive policy must not change behaviour."""
    calls = []

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        calls.append(cmd)
        dest = tmp_path / "mesa"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "PKGBUILD").write_text("pkgname=mesa\n")
        return _OK()

    monkeypatch.setattr(aur.subprocess, "run", _run)
    aur.aur_clone("mesa", tmp_path / "mesa")
    assert len(calls) == 1

def test_git_fetch_and_compare_denied_when_frozen(frozen_policy, tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("git must not run under freeze")

    monkeypatch.setattr(git_ops.subprocess, "run", _boom)
    with pytest.raises(NetworkFrozen):
        git_ops.git_fetch_and_compare(tmp_path / "mesa")


def test_frozen_is_a_blocking_sync_status():
    """STATUS_FROZEN must be in the blocking set, or update builds anyway.

    The default for an unrecognised status is *non-blocking* (the build
    proceeds against the local PKGBUILD) — correct for skip-shaped statuses,
    exactly wrong for a refusal.
    """
    assert source_sync.STATUS_FROZEN in _SYNC_BLOCKING_STATUSES


def test_scheduler_reports_frozen_and_does_not_raise(
    frozen_policy, tmp_path, monkeypatch
):
    """The run continues: a denial is a per-package result, not an abort."""
    real_run = git_ops.subprocess.run

    def _boom(cmd, *a, **kw):
        # Only the seam's own first probe (git_fetch_and_compare's
        # --git-dir check) must never run under freeze; the scheduler's
        # unrelated local HEAD lookup (for the RPC short-circuit) is not
        # part of this seam and must still work normally.
        if "--git-dir" in cmd:
            raise AssertionError("git must not run under freeze")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(git_ops.subprocess, "run", _boom)

    pkgdir = tmp_path / "mesa"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=mesa\n", encoding="utf-8")

    sched = source_sync.SourceSyncScheduler(state_dir=tmp_path / "state")
    result = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=pkgdir,
    ))
    assert result.status == source_sync.STATUS_FROZEN
    assert result.error and "mesa" in result.error


def test_offline_scheduler_does_not_trip_the_freeze(tmp_path, monkeypatch):
    """--offline causes no egress, so it must not itself be refused.

    Its status is SKIPPED_OFFLINE (a skip, build proceeds), never FROZEN (a
    blocker). Conflating them would break `sysforge update --offline` under a
    config-enabled freeze — the documented way to work while frozen. No git
    process may run either way — assert on the runner, not just the status.
    """
    def _boom(*a, **kw):
        raise AssertionError("git must not run under --offline")

    monkeypatch.setattr(git_ops.subprocess, "run", _boom)

    pkgdir = tmp_path / "mesa"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=mesa\n", encoding="utf-8")

    sched = source_sync.SourceSyncScheduler(
        state_dir=tmp_path / "state", offline=True)
    result = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=pkgdir))
    assert result.status != source_sync.STATUS_FROZEN


def test_scheduler_denies_through_the_real_seam_not_a_mock(
    frozen_policy, tmp_path, monkeypatch
):
    """Important-2: no scheduler test may prove the gate by mocking around it.

    Unlike the other scheduler tests, ``git_fetch_and_compare`` itself is
    left real here — only ``subprocess.run`` inside ``git_ops`` is patched to
    a raiser. If the freeze check were ever removed from the seam, this test
    would fail with the raiser's AssertionError instead of quietly passing.
    """
    real_run = git_ops.subprocess.run

    def _boom(cmd, *a, **kw):
        if "--git-dir" in cmd:
            raise AssertionError("git must not run under freeze")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(git_ops.subprocess, "run", _boom)

    pkgdir = tmp_path / "mesa"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=mesa\n", encoding="utf-8")
    # Prime the cache so the RPC short-circuit can't skip the fetch call
    # entirely and hide whether the seam itself was reached.
    sched = source_sync.SourceSyncScheduler(state_dir=tmp_path / "state")

    result = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=pkgdir, force_fetch=True,
    ))
    assert result.status == source_sync.STATUS_FROZEN


def test_scheduler_thaw_permits_fetch_when_checkout_dir_name_differs_from_pkgbase(
    frozen_policy, tmp_path, monkeypatch
):
    """Important-1: the scheduler and the seam must agree on ONE pkgbase.

    The checkout directory is deliberately named differently from the
    pkgbase (as split packages and renamed checkouts do). ``--thaw mesa``
    must permit the fetch even though the directory is not named "mesa" —
    if the seam fell back to the directory name instead of the threaded
    pkgbase, this would be wrongly refused.
    """
    frozen_policy(thawed=["mesa"])

    class _NotARepo:
        returncode = 1
        stdout = ""
        stderr = ""

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _NotARepo()

    monkeypatch.setattr(git_ops.subprocess, "run", _run)

    pkgdir = tmp_path / "mesa-checkout-dir"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=mesa\n", encoding="utf-8")

    sched = source_sync.SourceSyncScheduler(state_dir=tmp_path / "state")
    result = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=pkgdir, force_fetch=True,
    ))
    # Not FROZEN: the freeze check passed (pkgbase="mesa" is thawed) and the
    # seam proceeded to the (faked) git probe, which reports "not_a_repo" —
    # any status other than FROZEN proves the thaw was honoured.
    assert result.status != source_sync.STATUS_FROZEN
    assert calls  # the seam was actually reached, not short-circuited


def test_scheduler_syncs_a_thawed_package_beside_a_frozen_one(
    frozen_policy, tmp_path, monkeypatch
):
    """--thaw is per-package: one syncs, its neighbour is refused, same run.

    Goes through the real seam (only ``subprocess.run`` is faked) rather
    than mocking ``git_fetch_and_compare`` itself — Important-2: a scheduler
    test that replaces the seam function can't prove the seam's own check
    ever runs.
    """
    frozen_policy(thawed=["mesa"])

    for name in ("mesa", "cosmic-comp"):
        d = tmp_path / name
        d.mkdir()
        (d / "PKGBUILD").write_text(f"pkgname={name}\n", encoding="utf-8")

    seen = []

    class _NotARepo:
        returncode = 1
        stdout = ""
        stderr = ""

    def _run(cmd, **kw):
        # Only git_fetch_and_compare's own first probe uses --git-dir; the
        # scheduler's separate HEAD lookup (for the RPC short-circuit) does
        # not, so this isolates "the seam itself was reached" from other,
        # unrelated local git calls the scheduler makes regardless of freeze.
        if "--git-dir" in cmd:
            seen.append(Path(cmd[cmd.index("-C") + 1]).name)
        return _NotARepo()

    monkeypatch.setattr(git_ops.subprocess, "run", _run)

    sched = source_sync.SourceSyncScheduler(state_dir=tmp_path / "state")
    ok = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=tmp_path / "mesa", force_fetch=True))
    denied = sched.request(source_sync.SyncRequest(
        pkgbase="cosmic-comp", pkgbuild_dir=tmp_path / "cosmic-comp",
        force_fetch=True))

    assert ok.status != source_sync.STATUS_FROZEN
    assert denied.status == source_sync.STATUS_FROZEN
    assert seen == ["mesa"]  # the refused package never reached the seam


_VCS_PKGBUILD = """\
pkgname=mesa-git
pkgver=1
pkgrel=1
source=("git+https://gitlab.freedesktop.org/mesa/mesa.git")
"""


def _vcs_dir(tmp_path):
    d = tmp_path / "mesa-git"
    d.mkdir()
    (d / "PKGBUILD").write_text(_VCS_PKGBUILD, encoding="utf-8")
    return d


def test_peek_upstream_commit_denied_when_frozen(frozen_policy, tmp_path, monkeypatch):
    monkeypatch.setattr(
        vcs_pkgver.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("git must not run under freeze")),
    )
    assert vcs_pkgver.peek_upstream_commit(_vcs_dir(tmp_path)) is None


def test_evaluate_vcs_pkgver_denied_when_frozen(frozen_policy, tmp_path, monkeypatch):
    """makepkg must not run: it sources the PKGBUILD and executes pkgver().

    --nobuild suppresses build(), not top-level statements, so reaching this
    call under freeze is arbitrary code execution — the thing the gate exists
    to stop. Asserting on the return value alone would not catch it.
    """
    monkeypatch.setattr(
        vcs_pkgver.subprocess, "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("makepkg must not run under freeze")),
    )
    assert vcs_pkgver.evaluate_vcs_pkgver(_vcs_dir(tmp_path)) is None


def test_both_vcs_seams_are_gated_together(frozen_policy, tmp_path, monkeypatch):
    """The fall-through regression guard.

    peek returning None routes control to evaluate_vcs_pkgver. If a future
    change gates only the peek, this test is what fails.
    """
    ran = []
    monkeypatch.setattr(
        vcs_pkgver.subprocess, "run",
        lambda cmd, **kw: ran.append(cmd) or (_ for _ in ()).throw(
            AssertionError("no subprocess may run under freeze")),
    )
    d = _vcs_dir(tmp_path)
    assert vcs_pkgver.peek_upstream_commit(d) is None
    assert vcs_pkgver.evaluate_vcs_pkgver(d) is None
    assert ran == []


def test_vcs_seams_allowed_when_thawed(frozen_policy, tmp_path, monkeypatch):
    frozen_policy(thawed=["mesa-git"])
    calls = []

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(
        vcs_pkgver.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())
    vcs_pkgver.evaluate_vcs_pkgver(_vcs_dir(tmp_path))
    assert calls, "a thawed package must still reach makepkg"


# ---------------------------------------------------------------------------
# `pkgctl repo clone` — the fifth seam (Final-review HIGH-1).
#
# Repo-source PKGBUILDs arrive from gitlab.archlinux.org, not the AUR, but
# they are code-ingress all the same: a `repo_mode = build_from_source`
# package fetches a fresh PKGBUILD that the build then executes. Gating only
# `aur_clone` left this wide open.
# ---------------------------------------------------------------------------


def test_pkgctl_checkout_denied_when_frozen(frozen_policy, tmp_path, monkeypatch):
    """No pkgctl process may be spawned — assert on the runner, not the raise."""
    def _boom(*a, **kw):
        raise AssertionError("pkgctl must not run under freeze")

    monkeypatch.setattr(build_prep.subprocess, "run", _boom)
    monkeypatch.setattr(build_prep.subprocess, "Popen", _boom)
    with pytest.raises(NetworkFrozen) as exc:
        build_prep.pkgctl_checkout("mesa", tmp_path / "mesa")
    assert "mesa" in str(exc.value)


def test_repo_source_clone_yields_frozen_status(frozen_policy, tmp_path, monkeypatch):
    """A repo-source package must come back STATUS_FROZEN, not FAILED/CLONED.

    Goes through the real seam: only ``subprocess`` inside ``build_prep`` is
    faked, so removing the check would spawn the raiser rather than pass.
    """
    def _boom(*a, **kw):
        raise AssertionError("pkgctl must not run under freeze")

    monkeypatch.setattr(build_prep.subprocess, "run", _boom)
    monkeypatch.setattr(build_prep.subprocess, "Popen", _boom)

    sched = source_sync.SourceSyncScheduler(state_dir=tmp_path / "state")
    result = sched.request(source_sync.SyncRequest(
        pkgbase="mesa", pkgbuild_dir=tmp_path / "mesa", source="repo",
    ))
    assert result.status == source_sync.STATUS_FROZEN
    assert result.error and "mesa" in result.error


def test_repo_source_clone_allowed_when_thawed(frozen_policy, tmp_path, monkeypatch):
    """--thaw <pkgbase> lifts the repo-checkout seam like any other."""
    frozen_policy(thawed=["mesa"])
    calls = []

    def _popen(cmd, **kw):
        calls.append(cmd)
        raise FileNotFoundError  # pkgctl "missing" — the seam was reached

    monkeypatch.setattr(build_prep.subprocess, "Popen", _popen)
    with pytest.raises(RuntimeError) as exc:
        build_prep.pkgctl_checkout("mesa", tmp_path / "mesa")
    assert not isinstance(exc.value, NetworkFrozen)
    assert calls and calls[0][:3] == ["pkgctl", "repo", "clone"]


def test_find_pkgbuild_propagates_the_repo_freeze(frozen_policy, tmp_path, monkeypatch):
    """config.find_pkgbuild's repo branch must fail closed, like its AUR sibling."""
    from sysforge.primitives import config as config_mod

    monkeypatch.setattr(
        build_prep.subprocess, "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("pkgctl must not run under freeze")),
    )
    monkeypatch.setattr(
        "sysforge.primitives.aur.is_repo_package", lambda pkg: True)

    with pytest.raises(NetworkFrozen):
        config_mod.find_pkgbuild(
            "mesa", config={"paths": {"pkgbuild_src_dir": str(tmp_path / "src")}})


# ---------------------------------------------------------------------------
# --thaw key agreement across seams (Final-review MEDIUM-1).
# ---------------------------------------------------------------------------


def test_vcs_thaw_honours_pkgbase_when_dir_name_differs(
    frozen_policy, tmp_path, monkeypatch
):
    """A renamed checkout must still be liftable by its real pkgbase.

    A coexist ``-sysforge`` rename or a kernel local-rename makes the
    checkout directory name diverge from the pkgbase. If the VCS seams keyed
    the thaw on the directory name, ``--thaw mesa-git`` would silently fail
    to lift them (fail-closed, but a usability trap).
    """
    frozen_policy(thawed=["mesa-git"])
    d = tmp_path / "mesa-git-sysforge"
    d.mkdir()
    (d / "PKGBUILD").write_text(_VCS_PKGBUILD, encoding="utf-8")

    calls = []

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(
        vcs_pkgver.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R())

    assert vcs_pkgver.peek_upstream_commit(d, pkgbase="mesa-git") is None
    assert vcs_pkgver.evaluate_vcs_pkgver(d, pkgbase="mesa-git") is None
    assert calls, "both VCS seams must be reached for a thawed pkgbase"

    # And without the threaded pkgbase the dir-name fallback still refuses —
    # proving the assertion above is about the pkgbase, not a lax policy.
    calls.clear()
    assert vcs_pkgver.evaluate_vcs_pkgver(d) is None
    assert calls == []
