# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
source_sync.py — RPC-first, cache-backed source sync for AUR packages.

Replaces the old `_sync_sources` in `update.py` with a scheduler that:

  1. Batches one AUR RPC ``info`` call for every known pkgbase (the entire
     run — `aur_info` already accepts the full list in a single request).
  2. Diffs the result against ``source_meta.toml`` to decide which packages
     actually need a shallow ``git fetch``. Steady-state = zero fetches.
  3. Runs the remaining fetches sequentially through
     ``rate_limit.run_throttled_git``; a Retry-After response on either the
     RPC or a git fetch locks both paths until the window clears.

Public API::

    @dataclass
    class SyncRequest:
        pkgbase: str
        pkgbuild_dir: Path
        source: str = "aur"              # "aur" | "repo" | "git" | "local"
        force_fetch: bool = False        # bypass RPC short-circuit

``source = "local"`` describes a hand-maintained PKGBUILD with no upstream
remote to sync from. The scheduler short-circuits to
``STATUS_SKIPPED_LOCAL`` without any network or git activity; the directory
must already exist.

    @dataclass
    class SyncResult:
        pkgbase: str
        status: str                      # see STATUS_* constants
        head_before: str | None
        head_after: str | None
        error: str | None = None

    class SourceSyncScheduler:
        request(req)                     -> SyncResult
        sync_many(reqs)                  -> dict[str, SyncResult]
        invalidate(pkgbase)              -> None
        close()                          -> None   # persists cache

    get_scheduler(*, state_dir, offline=False, cleansrc=False,
                  min_fetch_interval_ms=None, rate_limit_abort_s=None,
                  fetch_timeout=None, clone_timeout=None,
                  force_devel=False, repo_track="stable") -> SourceSyncScheduler

``repo_track`` governs ``source = "repo"`` checkouts: ``"stable"`` (default)
pins them to pacman's sync-DB release tag after every clone/fetch (detached
HEAD via ``pkgctl repo switch``); ``"main"`` leaves them tracking the
packaging repo's main branch (testing-track). Callers thread
``config.resolve_repo_track(sysforge_toml["build"])``.

Sequential execution, per-process singleton, per-run dedup: a second
``request()`` for the same pkgbase returns the cached result. Cross-process
coordination is intentionally out of scope — a concurrent second sysforge
process would race on ``source_meta.toml``, and atomic write-then-rename
means last-writer-wins (same as ``build_state.toml``).
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur import (
    GitFetchOutcome,
    aur_clone,
    aur_info,
    git_fetch_and_compare,
    git_is_dirty,
    is_rate_limit_error,
)
from sysforge.primitives.build_prep import pkgctl_checkout, pkgctl_switch_version
from sysforge.primitives.git_ops import (
    _uncommitted_dirty_paths,
    purge_src,
    purge_srcdest,
)
from sysforge.primitives.net_policy import (
    NetworkFrozen,
)
from sysforge.primitives.pacman import get_pacman_sync_version, get_srcdest
from sysforge.primitives.rate_limit import RateLimiter
from sysforge.primitives.source_meta import SourceMetaCache, _now_iso

_log = log.get_logger("SYNC")


# --- Status constants (also used by SyncResult.status) ----------------------
STATUS_UP_TO_DATE    = "up_to_date"
STATUS_FETCHED       = "fetched"
STATUS_CLONED        = "cloned"
STATUS_DIVERGED      = "diverged"
STATUS_RATE_LIMITED  = "rate_limited"
STATUS_FAILED        = "failed"
STATUS_SKIPPED_OFFLINE = "skipped_offline"
STATUS_SKIPPED_NO_TRACKING = "no_tracking"
STATUS_SKIPPED_LOCAL = "skipped_local"
STATUS_PURGE_REFUSED = "purge_refused"

# 3.0.0-F2: the source freeze denied this package's fetch. A *blocker*, not a
# skip — the build must not proceed against a checkout we were refused
# permission to refresh.
STATUS_FROZEN        = "frozen"


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")


def is_vcs_pkgbase(pkgbase: str) -> bool:
    """True for VCS packaging repos (``-git``/``-svn``/``-hg``/``-bzr``).

    Public because the VCS-aware dirty-tree question is asked outside this
    module too (``llvm_state``); ``_VCS_SUFFIXES`` stays the single home for
    the suffix list.
    """
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# Internal alias — the in-module call sites read better short.
_is_vcs = is_vcs_pkgbase


@dataclass
class SyncRequest:
    pkgbase: str
    pkgbuild_dir: Path
    source: str = "aur"
    force_fetch: bool = False
    # Stock upstream base used for the pacman sync-DB pin lookup (source=repo).
    # For a coexist ``-sysforge`` rename the checkout tree/pkgbase is the renamed
    # value (``mesa-sysforge``) but pacman only knows the stock name (``mesa``);
    # callers thread ``origin_pkgbase`` here so the pin resolves. None → use
    # ``pkgbase`` (the common, un-renamed case).
    sync_db_name: str | None = None


@dataclass
class SyncResult:
    pkgbase: str
    status: str
    head_before: str | None = None
    head_after: str | None = None
    error: str | None = None


class SourceSyncScheduler:
    """Sequential, cache-backed source-sync executor.

    One instance is created per sysforge process via ``get_scheduler``.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        offline: bool = False,
        cleansrc: bool = False,
        cleansrc_force: bool = False,
        force_devel: bool = False,
        repo_track: str = "stable",
        min_fetch_interval_ms: int | None = None,
        rate_limit_abort_s: float | None = None,
        fetch_timeout: int | None = None,
        clone_timeout: int | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.offline = offline
        self.repo_track = repo_track
        # cleansrc_force implies cleansrc — same purge gate, but with the
        # dirty-tree refusal bypassed at purge_src.
        self.cleansrc = cleansrc or cleansrc_force
        self.cleansrc_force = cleansrc_force
        self.force_devel = force_devel
        self.rate_limit_abort_s = float(rate_limit_abort_s or 120.0)
        self.fetch_timeout = fetch_timeout if fetch_timeout is not None else 30
        self.clone_timeout = clone_timeout if clone_timeout is not None else 60

        self.limiter = RateLimiter(
            min_git_interval_s=(min_fetch_interval_ms or 500) / 1000.0,
        )
        self.cache = SourceMetaCache(self.state_dir)
        self._results: dict[str, SyncResult] = {}
        self._rpc_done = False
        self._rpc_map: dict[str, dict] = {}
        self._aborted = False
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request(self, req: SyncRequest) -> SyncResult:
        """Sync a single pkgbase. Cached per-process; safe to call repeatedly."""
        if req.pkgbase in self._results:
            return self._results[req.pkgbase]
        result = self._sync_one(req)
        self._results[req.pkgbase] = result
        return result

    def sync_many(self, reqs: list[SyncRequest]) -> dict[str, SyncResult]:
        """Sync a batch. Issues the single RPC call up front, then processes
        each request in order.
        """
        aur_bases = [r.pkgbase for r in reqs
                     if r.source in ("aur", "git") and r.pkgbase not in self._rpc_map]
        if aur_bases and not self.offline:
            self._ensure_rpc(aur_bases)

        out: dict[str, SyncResult] = {}
        for r in reqs:
            out[r.pkgbase] = self.request(r)
        return out

    def invalidate(self, pkgbase: str) -> None:
        """Forget both the per-run result and the persistent cache entry."""
        self._results.pop(pkgbase, None)
        self.cache.delete(pkgbase)

    def close(self) -> None:
        """Persist the metadata cache. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.cache.save()
        except OSError as e:
            _log.warn(f"could not persist source_meta.toml: {e}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_rpc(self, pkgbases: list[str]) -> None:
        """Run the batched AUR RPC call once per scheduler lifetime.

        Network errors are non-fatal: callers fall back to fetch-all, which
        is still bounded by the rate limiter and no worse than the old flow.
        """
        if self._rpc_done or self.offline:
            return
        self._rpc_done = True

        pending = [p for p in pkgbases if p not in self._rpc_map]
        if not pending:
            return

        try:
            self.limiter.wait_before_rpc()
            results = aur_info(pending)
        except Exception as e:  # noqa: BLE001 — urllib raises a sprawling tree
            _log.warn(f"AUR RPC batch failed: {e} — falling back to fetch-all")
            return

        self._rpc_map.update(results)
        self.cache.mark_rpc_sync()

    def _rpc_entry(self, pkgbase: str) -> dict | None:
        return self._rpc_map.get(pkgbase)

    def _abort_remaining(self, reason: str) -> None:
        """Set a flag so subsequent requests short-circuit to rate_limited."""
        if not self._aborted:
            _log.error(
                f"AUR sync aborted: {reason} — remaining packages marked "
                "rate_limited; retry in a few minutes"
            )
        self._aborted = True

    def _sync_one(self, req: SyncRequest) -> SyncResult:
        pkgbase = req.pkgbase
        pkgbuild_dir = Path(req.pkgbuild_dir)

        if req.source == "local":
            # Hand-maintained PKGBUILD: no remote to sync against. The
            # directory must already exist — refuse silently otherwise so
            # callers see a clean status (it's not a network/AUR failure).
            if not pkgbuild_dir.is_dir():
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_FAILED,
                    error=f"local PKGBUILD missing: {pkgbuild_dir}",
                )
            return SyncResult(pkgbase=pkgbase, status=STATUS_SKIPPED_LOCAL)

        if self._aborted:
            return SyncResult(pkgbase=pkgbase, status=STATUS_RATE_LIMITED,
                              error="rate-limit abort window active")

        # --- cleansrc: purge before any other work ---
        if self.cleansrc and pkgbuild_dir.exists():
            try:
                purge_src(
                    pkgbuild_dir,
                    force=self.cleansrc_force,
                    is_vcs=_is_vcs(pkgbase),
                )
                purge_srcdest(pkgbase, get_srcdest(), pkgbuild_dir=pkgbuild_dir)
                self.invalidate(pkgbase)
            except RuntimeError as e:
                _log.error(f"--cleansrc {pkgbase}: {e}")
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_PURGE_REFUSED, error=str(e),
                )

        if self.offline:
            if not pkgbuild_dir.is_dir():
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_SKIPPED_OFFLINE,
                    error="offline: local PKGBUILD missing",
                )
            return SyncResult(pkgbase=pkgbase, status=STATUS_SKIPPED_OFFLINE)

        # --- clone if missing ---
        needs_clone = not pkgbuild_dir.is_dir()
        needs_recovery = (pkgbuild_dir.is_dir()
                          and not (pkgbuild_dir / "PKGBUILD").exists())
        if needs_recovery:
            try:
                # The recovery branch covers degenerate ``.git/``-only
                # leftovers; ``git_is_dirty`` already reports those as not
                # dirty (no HEAD), so the explicit force is unnecessary
                # here. We still propagate ``cleansrc_force`` for
                # consistency with the explicit cleansrc branch above.
                purge_src(
                    pkgbuild_dir,
                    force=self.cleansrc_force,
                    is_vcs=_is_vcs(pkgbase),
                )
                purge_srcdest(pkgbase, get_srcdest(), pkgbuild_dir=pkgbuild_dir)
            except RuntimeError as e:
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_PURGE_REFUSED, error=str(e),
                )
            needs_clone = True

        if needs_clone:
            return self._clone(
                pkgbase, pkgbuild_dir, source=req.source,
                sync_db_name=req.sync_db_name,
            )

        # --- RPC short-circuit (skip fetch if nothing moved upstream) ---
        # Repo packages have no AUR-RPC equivalent — they fall through to
        # the generic fetch path, which works because pkgctl-clones are
        # plain git repos with a tracking branch.
        meta = self.cache.get(pkgbase)
        rpc_entry = self._rpc_entry(pkgbase) if req.source != "repo" else None
        local_head = _head_commit(pkgbuild_dir)
        force = req.force_fetch or self.cleansrc or (
            self.force_devel and _is_vcs(pkgbase)
        )
        if not force and self._can_short_circuit(rpc_entry, meta, local_head):
            # Refresh last_fetch_at so the entry doesn't look stale.
            self.cache.update(pkgbase, last_fetch_at=_now_iso())
            return SyncResult(
                pkgbase=pkgbase, status=STATUS_UP_TO_DATE,
                head_before=local_head, head_after=local_head,
            )

        return self._fetch(
            pkgbase, pkgbuild_dir, rpc_entry, source=req.source,
            sync_db_name=req.sync_db_name,
        )

    def _can_short_circuit(
        self, rpc_entry: dict | None, meta: dict | None, local_head: str | None,
    ) -> bool:
        if rpc_entry is None or meta is None:
            return False
        if rpc_entry.get("Version") != meta.get("rpc_version"):
            return False
        if rpc_entry.get("LastModified") != meta.get("rpc_last_modified"):
            return False
        return local_head == meta.get("head_commit")

    def _clone(self, pkgbase: str, pkgbuild_dir: Path,
               *, source: str = "aur",
               sync_db_name: str | None = None) -> SyncResult:
        self.limiter.wait_before_fetch()
        try:
            if source == "repo":
                # gitlab.archlinux.org isn't subject to AUR's 429/503 budget,
                # so the rate limiter still ticks for the inter-fetch delay
                # but a checkout failure isn't translated to RATE_LIMITED.
                pkgctl_checkout(pkgbase, pkgbuild_dir, timeout=self.clone_timeout)
            else:
                aur_clone(pkgbase, pkgbuild_dir, timeout=self.clone_timeout)
        except NetworkFrozen as e:
            return SyncResult(pkgbase=pkgbase, status=STATUS_FROZEN, error=str(e))
        except RuntimeError as e:
            err = str(e)
            if source != "repo" and is_rate_limit_error(err):
                self.limiter.apply_retry_after(None, source="AUR clone 429/503")
                if self.limiter.remaining_penalty_s() > self.rate_limit_abort_s:
                    self._abort_remaining("rate-limit penalty exceeds threshold")
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_RATE_LIMITED, error=err,
                )
            return SyncResult(pkgbase=pkgbase, status=STATUS_FAILED, error=err)

        if source == "repo":
            err = self._pin_repo_checkout(pkgbase, pkgbuild_dir, sync_db_name)
            if err is not None:
                # Fresh clone: no prior head; report where the failed pin
                # left the checkout so STATUS_FAILED stays reporting-consistent.
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_FAILED,
                    head_before=None, head_after=_head_commit(pkgbuild_dir),
                    error=err,
                )

        head = _head_commit(pkgbuild_dir)
        rpc_entry = self._rpc_entry(pkgbase) if source != "repo" else None
        self.cache.update(
            pkgbase,
            rpc_version=(rpc_entry or {}).get("Version"),
            rpc_last_modified=(rpc_entry or {}).get("LastModified"),
            rpc_package_base=(rpc_entry or {}).get("PackageBase"),
            head_commit=head,
            is_vcs=_is_vcs(pkgbase),
            last_fetch_at=_now_iso(),
        )
        return SyncResult(
            pkgbase=pkgbase, status=STATUS_CLONED,
            head_before=None, head_after=head,
        )

    def _pin_repo_checkout(
        self, pkgbase: str, pkgbuild_dir: Path, sync_db_name: str | None = None,
    ) -> str | None:
        """Pin a source=repo checkout to pacman's sync-DB release tag.

        Returns an error string (sync becomes STATUS_FAILED) or None on
        success/no-op. No sync-DB candidate → warn and stay on main (never an
        error: the package may live only in a custom repo).

        ``sync_db_name`` is the stock upstream base to query pacman with; for a
        coexist ``-sysforge`` rename the checkout ``pkgbase`` is the renamed
        value (``mesa-sysforge``) but pacman only knows the stock name
        (``mesa``). Falls back to ``pkgbase`` for the common un-renamed case
        (2.1.0-B12).
        """
        if self.repo_track != "stable":
            return None
        db_name = sync_db_name or pkgbase
        version = get_pacman_sync_version(db_name)
        if version is None:
            _log.warn(
                f"{db_name}: no sync-DB candidate — leaving checkout on main "
                f"(testing-track)"
            )
            return None
        try:
            pkgctl_switch_version(pkgbuild_dir, version, timeout=self.fetch_timeout)
        except RuntimeError as e:
            return str(e)
        return None

    def _fetch(
        self, pkgbase: str, pkgbuild_dir: Path, rpc_entry: dict | None,
        *, source: str = "aur", sync_db_name: str | None = None,
    ) -> SyncResult:
        try:
            # pkgbase is threaded through explicitly (not left to
            # git_fetch_and_compare's dir-name fallback) so the single freeze
            # check inside the seam always sees the authoritative --thaw
            # name, even when the checkout dir name differs from pkgbase.
            outcome: GitFetchOutcome = git_fetch_and_compare(
                pkgbuild_dir, timeout=self.fetch_timeout, limiter=self.limiter,
                is_vcs=_is_vcs(pkgbase), pkgbase=pkgbase,
            )
        except NetworkFrozen as e:
            return SyncResult(pkgbase=pkgbase, status=STATUS_FROZEN, error=str(e))

        repo_stable = source == "repo" and self.repo_track == "stable"

        if repo_stable and outcome.status == "no_tracking":
            # A pinned checkout sits on a detached HEAD with no tracking
            # branch — the steady state for repo+stable. Refresh tags with a
            # plain fetch, then fall through to the re-pin below.
            head_before = _head_commit(pkgbuild_dir)
            err = _fetch_repo_tags(
                pkgbuild_dir, timeout=self.fetch_timeout, limiter=self.limiter,
            )
            if err is not None:
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_FAILED,
                    head_before=head_before, error=err,
                )
            outcome = GitFetchOutcome(
                status="up_to_date",
                head_before=head_before, head_after=head_before,
            )

        if outcome.status == STATUS_RATE_LIMITED:
            if self.limiter.remaining_penalty_s() > self.rate_limit_abort_s:
                self._abort_remaining("rate-limit penalty exceeds threshold")
            return SyncResult(
                pkgbase=pkgbase, status=STATUS_RATE_LIMITED,
                head_before=outcome.head_before, head_after=outcome.head_after,
                error=outcome.error,
            )

        # A packaging repo's job is to mirror upstream; it only diverges
        # without operator work when upstream rewrote history (force-push /
        # amend — common on the AUR — or pkgctl's staging branch drifting for
        # repo packages). When the working tree is clean per the VCS-aware
        # dirty check, hard-reset to FETCH_HEAD so the local PKGBUILD tracks
        # upstream instead of demanding ``--cleansrc``. Dirty trees carry real
        # operator edits and are respected — they stay in DIVERGED.
        if (outcome.status == STATUS_DIVERGED
                and not git_is_dirty(pkgbuild_dir, is_vcs=_is_vcs(pkgbase))):
            new_head = _reset_hard_fetch_head(pkgbuild_dir)
            if new_head is not None:
                outcome = GitFetchOutcome(
                    status="fetched",
                    head_before=outcome.head_before,
                    head_after=new_head,
                )
                _log.info(
                    f"{pkgbase}: upstream history diverged on a clean tree; "
                    f"reset to upstream {new_head[:10]}"
                )

        if repo_stable and outcome.status in ("up_to_date", "fetched"):
            # B16: gate the re-pin on *genuine uncommitted tracked edits* only,
            # not on git_is_dirty(). A pinned checkout is a detached HEAD with
            # no tracking branch, which git_is_dirty reports as dirty by
            # definition (its no-tracking = "purely local hand-maintained tree"
            # rule) — so git_is_dirty would refuse to re-pin *every* pristine
            # pin and freeze the tree at its first version. _uncommitted_dirty_
            # paths asks the narrower working-tree question, so a clean pin
            # re-advances while real operator edits still block. (Distinct from
            # the DIVERGED auto-reset above, whose git_is_dirty stays.)
            if _uncommitted_dirty_paths(pkgbuild_dir, is_vcs=_is_vcs(pkgbase)):
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_DIVERGED,
                    head_before=outcome.head_before,
                    head_after=outcome.head_after,
                    error="local edits present — not re-pinning",
                )
            pre = _head_commit(pkgbuild_dir)
            err = self._pin_repo_checkout(pkgbase, pkgbuild_dir, sync_db_name)
            if err is not None:
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_FAILED,
                    head_before=pre, head_after=_head_commit(pkgbuild_dir),
                    error=err,
                )
            post = _head_commit(pkgbuild_dir)
            if post != pre:
                # The pin moved HEAD (sync-DB tag advanced) — report the
                # switch as the fetch delta.
                outcome = GitFetchOutcome(
                    status="fetched", head_before=pre, head_after=post,
                )

        if outcome.status in ("up_to_date", "fetched"):
            self.cache.update(
                pkgbase,
                rpc_version=(rpc_entry or {}).get("Version"),
                rpc_last_modified=(rpc_entry or {}).get("LastModified"),
                rpc_package_base=(rpc_entry or {}).get("PackageBase"),
                head_commit=outcome.head_after,
                is_vcs=_is_vcs(pkgbase),
                last_fetch_at=_now_iso(),
            )

        if outcome.status == "not_a_repo":
            # Plain directory with a PKGBUILD — no sync to perform.
            return SyncResult(
                pkgbase=pkgbase, status=STATUS_UP_TO_DATE,
                head_before=None, head_after=None,
            )

        if outcome.status == "no_tracking":
            return SyncResult(
                pkgbase=pkgbase, status=STATUS_SKIPPED_NO_TRACKING,
                head_before=outcome.head_before, head_after=outcome.head_after,
            )

        return SyncResult(
            pkgbase=pkgbase,
            status=outcome.status,
            head_before=outcome.head_before,
            head_after=outcome.head_after,
            error=outcome.error,
        )


def _head_commit(pkgbuild_dir: Path) -> str | None:
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _fetch_repo_tags(
    pkgbuild_dir: Path, *, timeout: int | None = 30, limiter=None,
) -> str | None:
    """Tags-included fetch for a pinned (detached-HEAD) repo checkout.

    ``git_fetch_and_compare`` bails with ``no_tracking`` on detached HEADs,
    so pinned repo+stable checkouts refresh release tags with a plain
    ``git fetch --tags origin`` instead. Returns an error string or None.
    """
    import subprocess
    cmd = ["git", "-C", str(pkgbuild_dir), "fetch", "--tags", "origin"]
    try:
        if limiter is not None:
            from sysforge.primitives.rate_limit import run_throttled_git
            r = run_throttled_git(cmd, limiter, timeout=timeout or None)
        else:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout or None,
            )
    except subprocess.TimeoutExpired:
        return f"git fetch --tags timed out after {timeout}s"
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip() or "git fetch --tags failed"
    return None


def _reset_hard_fetch_head(pkgbuild_dir: Path) -> str | None:
    """Hard-reset the local branch to FETCH_HEAD.

    Used by the repo-source divergence override: pkgctl checkouts have no
    user commits worth preserving, so when fetch reports divergence and the
    working tree is clean it's safe to force-track upstream.
    """
    import subprocess
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "reset", "--hard", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return _head_commit(pkgbuild_dir)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_scheduler: SourceSyncScheduler | None = None


def get_scheduler(
    *,
    state_dir: Path | None = None,
    offline: bool = False,
    cleansrc: bool = False,
    cleansrc_force: bool = False,
    force_devel: bool = False,
    repo_track: str = "stable",
    min_fetch_interval_ms: int | None = None,
    rate_limit_abort_s: float | None = None,
    fetch_timeout: int | None = None,
    clone_timeout: int | None = None,
) -> SourceSyncScheduler:
    """Return the per-process scheduler, constructing it on first call.

    Subsequent calls update mutable runtime flags (``offline`` / ``cleansrc``
    / ``cleansrc_force`` / ``force_devel`` / ``repo_track``) on the existing
    instance so
    callers in different commands (e.g. `sysforge fetch` after `sysforge
    update`) can share the metadata cache without re-reading
    ``source_meta.toml``.
    """
    global _scheduler
    if _scheduler is None:
        if state_dir is None:
            from sysforge.pipeline.state import resolve_state_dir
            state_dir, _ = resolve_state_dir(None)
        _scheduler = SourceSyncScheduler(
            state_dir=state_dir,
            offline=offline,
            cleansrc=cleansrc,
            cleansrc_force=cleansrc_force,
            force_devel=force_devel,
            repo_track=repo_track,
            min_fetch_interval_ms=min_fetch_interval_ms,
            rate_limit_abort_s=rate_limit_abort_s,
            fetch_timeout=fetch_timeout,
            clone_timeout=clone_timeout,
        )
        atexit.register(_scheduler.close)
    else:
        _scheduler.offline = offline or _scheduler.offline
        _scheduler.cleansrc = cleansrc or cleansrc_force or _scheduler.cleansrc
        _scheduler.cleansrc_force = cleansrc_force or _scheduler.cleansrc_force
        _scheduler.force_devel = force_devel or _scheduler.force_devel
        if repo_track != "stable":
            _scheduler.repo_track = repo_track
    return _scheduler


def reset_scheduler() -> None:
    """Test helper — drop the module-level singleton."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.close()
    _scheduler = None
