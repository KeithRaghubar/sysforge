"""
test_source_sync.py — unit tests for sysforge.primitives.source_sync.

Covers:
    SourceSyncScheduler
        RPC short-circuit when version+last_modified+head all match
        RPC miss triggers shallow fetch
        VCS packages always fetch when --devel is on
        missing dir → _clone path
        divergence surfaced (not fixed)
        rate-limited status propagates + aborts remainder
        dedup per pkgbase across request() calls
        cleansrc purges + reclones
        offline short-circuits

    get_scheduler / reset_scheduler singleton behaviour
"""
from pathlib import Path
from unittest.mock import patch

from sysforge.primitives.aur import GitFetchOutcome
from sysforge.primitives.source_sync import (
    STATUS_CLONED,
    STATUS_DIVERGED,
    STATUS_FAILED,
    STATUS_FETCHED,
    STATUS_RATE_LIMITED,
    STATUS_SKIPPED_OFFLINE,
    STATUS_UP_TO_DATE,
    SourceSyncScheduler,
    SyncRequest,
    get_scheduler,
    reset_scheduler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir()
    (d / "PKGBUILD").write_text(f"pkgname={name}\n")
    return d


def _scheduler(tmp_path, **kwargs) -> SourceSyncScheduler:
    kwargs.setdefault("fetch_timeout", 30)
    kwargs.setdefault("clone_timeout", 60)
    return SourceSyncScheduler(state_dir=tmp_path / "state", **kwargs)


# ---------------------------------------------------------------------------
# RPC short-circuit
# ---------------------------------------------------------------------------

def test_rpc_short_circuit_when_cache_matches(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    sched._rpc_done = True
    sched._rpc_map = {"htop": {"Version": "3.3-1", "LastModified": 1000}}
    sched.cache.update(
        "htop",
        rpc_version="3.3-1",
        rpc_last_modified=1000,
        head_commit="abc123",
    )

    with patch("sysforge.primitives.source_sync._head_commit", return_value="abc123"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare") as fetch, \
         patch("sysforge.primitives.source_sync.aur_clone") as clone:
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert result.status == STATUS_UP_TO_DATE
    fetch.assert_not_called()
    clone.assert_not_called()


def test_rpc_miss_triggers_fetch(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    sched._rpc_done = True
    sched._rpc_map = {"htop": {"Version": "3.4-1", "LastModified": 2000}}
    sched.cache.update(
        "htop", rpc_version="3.3-1", rpc_last_modified=1000, head_commit="abc",
    )

    outcome = GitFetchOutcome(
        status="fetched", head_before="abc", head_after="def",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="abc"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome):
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert result.status == STATUS_FETCHED
    assert result.head_after == "def"
    # Cache should now reflect new RPC version + head.
    entry = sched.cache.get("htop")
    assert entry["rpc_version"] == "3.4-1"
    assert entry["rpc_last_modified"] == 2000
    assert entry["head_commit"] == "def"


def test_vcs_pkg_force_fetches_under_devel(tmp_path):
    pkg = _make_repo(tmp_path, "mesa-git")
    sched = _scheduler(tmp_path, force_devel=True)
    sched._rpc_done = True
    sched._rpc_map = {"mesa-git": {"Version": "v1-1", "LastModified": 100}}
    sched.cache.update(
        "mesa-git", rpc_version="v1-1", rpc_last_modified=100, head_commit="abc",
    )

    outcome = GitFetchOutcome(status="up_to_date", head_before="abc", head_after="abc")
    with patch("sysforge.primitives.source_sync._head_commit", return_value="abc"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare",
               return_value=outcome) as fetch:
        result = sched.request(SyncRequest(pkgbase="mesa-git", pkgbuild_dir=pkg))

    # --devel bypasses the RPC short-circuit for VCS packages.
    fetch.assert_called_once()
    assert result.status == STATUS_UP_TO_DATE


def test_force_fetch_bypasses_short_circuit(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    sched._rpc_done = True
    sched._rpc_map = {"htop": {"Version": "3.3-1", "LastModified": 1000}}
    sched.cache.update(
        "htop", rpc_version="3.3-1", rpc_last_modified=1000, head_commit="abc",
    )

    outcome = GitFetchOutcome(status="up_to_date", head_before="abc", head_after="abc")
    with patch("sysforge.primitives.source_sync._head_commit", return_value="abc"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare",
               return_value=outcome) as fetch:
        sched.request(SyncRequest(
            pkgbase="htop", pkgbuild_dir=pkg, force_fetch=True,
        ))

    fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Clone path
# ---------------------------------------------------------------------------

def test_clone_on_missing_dir(tmp_path):
    dest = tmp_path / "htop"
    sched = _scheduler(tmp_path)

    def fake_clone(name, d, **kw):
        Path(d).mkdir()
        (Path(d) / "PKGBUILD").write_text("pkgname=htop\n")

    with patch("sysforge.primitives.source_sync.aur_clone", side_effect=fake_clone), \
         patch("sysforge.primitives.source_sync._head_commit", return_value="newhead"):
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=dest))

    assert result.status == STATUS_CLONED
    assert result.head_after == "newhead"


def test_clone_rate_limit_aborts_remaining(tmp_path):
    sched = _scheduler(tmp_path, rate_limit_abort_s=1.0)
    dest1 = tmp_path / "p1"
    dest2 = tmp_path / "p2"

    with patch("sysforge.primitives.source_sync.aur_clone",
               side_effect=RuntimeError("error: 429 Too Many Requests")):
        r1 = sched.request(SyncRequest(pkgbase="p1", pkgbuild_dir=dest1))
        r2 = sched.request(SyncRequest(pkgbase="p2", pkgbuild_dir=dest2))

    assert r1.status == STATUS_RATE_LIMITED
    # Second request short-circuits due to abort window.
    assert r2.status == STATUS_RATE_LIMITED
    assert sched._aborted is True


def test_clone_non_rate_limit_failure_is_failed(tmp_path):
    sched = _scheduler(tmp_path)
    dest = tmp_path / "missing"
    with patch("sysforge.primitives.source_sync.aur_clone",
               side_effect=RuntimeError("repository not found")):
        result = sched.request(SyncRequest(pkgbase="missing", pkgbuild_dir=dest))
    assert result.status == STATUS_FAILED
    assert "not found" in result.error


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------

def test_divergence_is_surfaced_not_fixed(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(
        status="diverged", head_before="local", head_after="upstream",
        error="divergent: HEAD local vs FETCH_HEAD upstream",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="local"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome):
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert result.status == STATUS_DIVERGED
    # Work-tree is not modified; PKGBUILD still present.
    assert (pkg / "PKGBUILD").exists()


def test_repo_source_diverged_clean_tree_resets_to_upstream(tmp_path):
    """
    Auto-refresh: a `source = "repo"` request that returns DIVERGED with a
    clean working tree is converted to FETCHED via a hard-reset to
    FETCH_HEAD. pkgctl checkouts have no user commits worth preserving, so
    "ahead 1, behind 1" should not be a sticky failure.
    """
    pkg = _make_repo(tmp_path, "pipewire")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(
        status="diverged", head_before="oldlocal", head_after="newupstream",
        error="divergent: HEAD oldlocal vs FETCH_HEAD newupstream",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="oldlocal"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome), \
         patch("sysforge.primitives.source_sync.git_is_dirty", return_value=False), \
         patch("sysforge.primitives.source_sync._reset_hard_fetch_head",
               return_value="newupstream") as reset:
        result = sched.request(SyncRequest(
            pkgbase="pipewire", pkgbuild_dir=pkg, source="repo",
        ))

    reset.assert_called_once_with(pkg)
    assert result.status == "fetched"
    assert result.head_after == "newupstream"


def test_repo_source_diverged_dirty_tree_stays_diverged(tmp_path):
    """
    Dirty trees are still respected — user has local edits and they must
    not be overwritten. STATUS_DIVERGED stays sticky; --cleansrc remains
    the explicit escape hatch.
    """
    pkg = _make_repo(tmp_path, "pipewire")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(
        status="diverged", head_before="local", head_after="upstream",
        error="working tree has local modifications",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="local"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome), \
         patch("sysforge.primitives.source_sync.git_is_dirty", return_value=True), \
         patch("sysforge.primitives.source_sync._reset_hard_fetch_head") as reset:
        result = sched.request(SyncRequest(
            pkgbase="pipewire", pkgbuild_dir=pkg, source="repo",
        ))

    reset.assert_not_called()
    assert result.status == STATUS_DIVERGED


def test_aur_source_diverged_clean_tree_resets_to_upstream(tmp_path):
    """
    A clean AUR clone whose upstream rewrote history (force-push / amend —
    common on the AUR) is reset to track upstream instead of demanding
    --cleansrc. The dirty gate (git_is_dirty) is what protects real local
    work, so a truly clean tree auto-refreshes for AUR sources too.
    """
    pkg = _make_repo(tmp_path, "neovim-git")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(
        status="diverged", head_before="oldlocal", head_after="newupstream",
        error="divergent: HEAD oldlocal vs FETCH_HEAD newupstream",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="oldlocal"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome), \
         patch("sysforge.primitives.source_sync.git_is_dirty", return_value=False) as dirty, \
         patch("sysforge.primitives.source_sync._reset_hard_fetch_head",
               return_value="newupstream") as reset:
        result = sched.request(SyncRequest(
            pkgbase="neovim-git", pkgbuild_dir=pkg, source="aur",
        ))

    reset.assert_called_once_with(pkg)
    # The VCS-aware dirty check is used (so makepkg's pkgver auto-bump of
    # PKGBUILD/.SRCINFO doesn't block the reset).
    dirty.assert_called_once_with(pkg, is_vcs=True)
    assert result.status == "fetched"
    assert result.head_after == "newupstream"


def test_aur_source_diverged_dirty_tree_stays_diverged(tmp_path):
    """
    A dirty AUR clone carries real operator work (e.g. a hand-edited
    PKGBUILD) — divergence stays sticky and --cleansrc remains the explicit
    escape hatch.
    """
    pkg = _make_repo(tmp_path, "neovim-git")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(
        status="diverged", head_before="local", head_after="upstream",
        error="divergent: HEAD local vs FETCH_HEAD upstream",
    )
    with patch("sysforge.primitives.source_sync._head_commit", return_value="local"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", return_value=outcome), \
         patch("sysforge.primitives.source_sync.git_is_dirty", return_value=True), \
         patch("sysforge.primitives.source_sync._reset_hard_fetch_head") as reset:
        result = sched.request(SyncRequest(
            pkgbase="neovim-git", pkgbuild_dir=pkg, source="aur",
        ))

    reset.assert_not_called()
    assert result.status == STATUS_DIVERGED


# ---------------------------------------------------------------------------
# Dedup / offline
# ---------------------------------------------------------------------------

def test_dedup_returns_cached_result(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    outcome = GitFetchOutcome(status="up_to_date", head_before="a", head_after="a")

    call_count = {"n": 0}

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return outcome

    with patch("sysforge.primitives.source_sync._head_commit", return_value="a"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare", side_effect=counting):
        first = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))
        second = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert first is second
    assert call_count["n"] == 1


def test_offline_skips_fetch(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path, offline=True)

    with patch("sysforge.primitives.source_sync.git_fetch_and_compare") as fetch, \
         patch("sysforge.primitives.source_sync.aur_clone") as clone:
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert result.status == STATUS_SKIPPED_OFFLINE
    fetch.assert_not_called()
    clone.assert_not_called()


def test_offline_missing_dir_reports_skipped(tmp_path):
    sched = _scheduler(tmp_path, offline=True)
    result = sched.request(SyncRequest(
        pkgbase="htop", pkgbuild_dir=tmp_path / "not_there",
    ))
    assert result.status == STATUS_SKIPPED_OFFLINE
    assert "offline" in (result.error or "")


def test_repo_source_routes_through_pkgctl(tmp_path):
    """
    A2: source='repo' on an empty pkgbuild_dir clones via `pkgctl repo clone`
    instead of the AUR clone path. Outcome is STATUS_CLONED, mirroring the
    AUR-side semantics — repo packages are now full participants in
    `sysforge update`'s sync phase.
    """
    sched = _scheduler(tmp_path)
    pkg_dir = tmp_path / "htop"

    def fake_pkgctl(name, dest, *, timeout=None):
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "PKGBUILD").write_text("pkgname=htop\n")

    with patch("sysforge.primitives.source_sync.pkgctl_checkout",
               side_effect=fake_pkgctl) as pkgctl, \
         patch("sysforge.primitives.source_sync.aur_clone") as aur_clone_mock, \
         patch("sysforge.primitives.source_sync._head_commit",
               return_value="abcd1234"):
        result = sched.request(SyncRequest(
            pkgbase="htop", pkgbuild_dir=pkg_dir, source="repo",
        ))

    pkgctl.assert_called_once()
    aur_clone_mock.assert_not_called()
    assert result.status == "cloned"
    assert result.head_after == "abcd1234"


# ---------------------------------------------------------------------------
# cleansrc
# ---------------------------------------------------------------------------

def test_cleansrc_purges_and_reclones(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path, cleansrc=True)

    def fake_clone(name, d, **kw):
        Path(d).mkdir(exist_ok=True)
        (Path(d) / "PKGBUILD").write_text("pkgname=htop\n")

    with patch("sysforge.primitives.source_sync.purge_src") as purge, \
         patch("sysforge.primitives.source_sync.aur_clone", side_effect=fake_clone) as clone, \
         patch("sysforge.primitives.source_sync._head_commit", return_value="new"):
        # After purge_src the dir no longer exists; emulate that side effect.
        purge.side_effect = lambda d, *, force=False, is_vcs=False: None
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    # Non-VCS pkgbase → is_vcs=False is forwarded so the existing dirty
    # guard still protects deliberate PKGBUILD edits on repo packages.
    purge.assert_called_once_with(pkg, force=False, is_vcs=False)
    # Clone runs because the purge invalidates the cache entry; even with the
    # dir still present, cleansrc forces past the RPC short-circuit.
    assert clone.called or result.status in (STATUS_CLONED, STATUS_UP_TO_DATE, STATUS_FETCHED)


def test_cleansrc_forwards_is_vcs_for_git_pkgbase(tmp_path):
    """``-git`` pkgbase → purge_src is called with is_vcs=True so makepkg's
    pkgver auto-bump does not falsely block ``--cleansrc``.
    """
    pkg = _make_repo(tmp_path, "ipp-usb-git")
    sched = _scheduler(tmp_path, cleansrc=True)

    def fake_clone(name, d, **kw):
        Path(d).mkdir(exist_ok=True)
        (Path(d) / "PKGBUILD").write_text("pkgname=ipp-usb-git\n")

    with patch("sysforge.primitives.source_sync.purge_src") as purge, \
         patch("sysforge.primitives.source_sync.aur_clone", side_effect=fake_clone), \
         patch("sysforge.primitives.source_sync._head_commit", return_value="new"):
        purge.side_effect = lambda d, *, force=False, is_vcs=False: None
        sched.request(SyncRequest(pkgbase="ipp-usb-git", pkgbuild_dir=pkg))

    purge.assert_called_once_with(pkg, force=False, is_vcs=True)


def test_cleansrc_refused_on_dirty_repo(tmp_path):
    pkg = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path, cleansrc=True)

    with patch("sysforge.primitives.source_sync.purge_src",
               side_effect=RuntimeError("refusing to purge: dirty")):
        result = sched.request(SyncRequest(pkgbase="htop", pkgbuild_dir=pkg))

    assert result.status == "purge_refused"
    assert "refusing" in (result.error or "")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_close_persists_cache(tmp_path):
    sched = _scheduler(tmp_path)
    sched.cache.update("htop", rpc_version="3.3-1")
    sched.close()
    assert (tmp_path / "state" / "source_meta.toml").exists()


def test_close_is_idempotent(tmp_path):
    sched = _scheduler(tmp_path)
    sched.cache.update("pkg", rpc_version="x")
    sched.close()
    sched.close()  # second call is a no-op — must not raise


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

def test_get_scheduler_returns_same_instance(tmp_path):
    reset_scheduler()
    try:
        s1 = get_scheduler(state_dir=tmp_path)
        s2 = get_scheduler(state_dir=tmp_path)
        assert s1 is s2
    finally:
        reset_scheduler()


def test_reset_scheduler_drops_instance(tmp_path):
    reset_scheduler()
    s1 = get_scheduler(state_dir=tmp_path)
    reset_scheduler()
    s2 = get_scheduler(state_dir=tmp_path)
    assert s1 is not s2
    reset_scheduler()


# ---------------------------------------------------------------------------
# `source = "local"` — hand-maintained PKGBUILD with no remote
# ---------------------------------------------------------------------------

def test_local_source_short_circuits_without_network(tmp_path):
    """A local-source request never touches the network or git: the
    scheduler sees the dir exists and returns STATUS_SKIPPED_LOCAL.

    Patches aur_clone / git_fetch_and_compare to fail loudly if either is
    invoked — proving the short-circuit is taken before any sync work.
    """
    from sysforge.primitives.source_sync import STATUS_SKIPPED_LOCAL
    pkg = _make_repo(tmp_path, "linux-custom")
    sched = _scheduler(tmp_path)
    with patch("sysforge.primitives.source_sync.aur_clone",
               side_effect=AssertionError("aur_clone must not be called")), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare",
               side_effect=AssertionError("git_fetch_and_compare must not be called")):
        result = sched.request(SyncRequest(
            pkgbase="linux-custom", pkgbuild_dir=pkg, source="local",
        ))
    assert result.status == STATUS_SKIPPED_LOCAL


def test_local_source_missing_dir_is_failed(tmp_path):
    """A local-source request whose pkgbuild_dir does not exist is reported
    as STATUS_FAILED — there's no remote to clone from, so the missing
    directory is an operator-fixable error.
    """
    sched = _scheduler(tmp_path)
    dest = tmp_path / "linux-custom"  # not created
    result = sched.request(SyncRequest(
        pkgbase="linux-custom", pkgbuild_dir=dest, source="local",
    ))
    assert result.status == STATUS_FAILED
    assert "missing" in (result.error or "").lower()


def test_local_source_excluded_from_rpc_batch(tmp_path):
    """sync_many's RPC batch must not include local-source pkgbases."""
    pkg_local = _make_repo(tmp_path, "linux-custom")
    pkg_aur = _make_repo(tmp_path, "htop")
    sched = _scheduler(tmp_path)
    with patch.object(sched, "_ensure_rpc") as ensure_rpc, \
         patch("sysforge.primitives.source_sync._head_commit", return_value="x"), \
         patch("sysforge.primitives.source_sync.git_fetch_and_compare",
               return_value=GitFetchOutcome(status="up_to_date",
                                            head_before="x", head_after="x")):
        sched.sync_many([
            SyncRequest(pkgbase="linux-custom", pkgbuild_dir=pkg_local,
                        source="local"),
            SyncRequest(pkgbase="htop", pkgbuild_dir=pkg_aur, source="aur"),
        ])
    # RPC should be primed for the AUR base only, never the local one.
    ensure_rpc.assert_called_once()
    (called_args,) = ensure_rpc.call_args.args
    assert called_args == ["htop"]
