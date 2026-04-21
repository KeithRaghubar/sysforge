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
        source: str = "aur"              # "aur" | "repo" | "git"
        force_fetch: bool = False        # bypass RPC short-circuit

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
                  force_devel=False) -> SourceSyncScheduler

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
    is_rate_limit_error,
    purge_src,
)
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
STATUS_PURGE_REFUSED = "purge_refused"


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


@dataclass
class SyncRequest:
    pkgbase: str
    pkgbuild_dir: Path
    source: str = "aur"
    force_fetch: bool = False


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
        force_devel: bool = False,
        min_fetch_interval_ms: int | None = None,
        rate_limit_abort_s: float | None = None,
        fetch_timeout: int | None = None,
        clone_timeout: int | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.offline = offline
        self.cleansrc = cleansrc
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

        if req.source == "repo":
            # Repo packages live in pkgctl-managed dirs; sync is out of scope here.
            return SyncResult(pkgbase=pkgbase, status=STATUS_UP_TO_DATE)

        if self._aborted:
            return SyncResult(pkgbase=pkgbase, status=STATUS_RATE_LIMITED,
                              error="rate-limit abort window active")

        # --- cleansrc: purge before any other work ---
        if self.cleansrc and pkgbuild_dir.exists():
            try:
                purge_src(pkgbuild_dir)
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
                purge_src(pkgbuild_dir)
            except RuntimeError as e:
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_PURGE_REFUSED, error=str(e),
                )
            needs_clone = True

        if needs_clone:
            return self._clone(pkgbase, pkgbuild_dir)

        # --- RPC short-circuit (skip fetch if nothing moved upstream) ---
        meta = self.cache.get(pkgbase)
        rpc_entry = self._rpc_entry(pkgbase)
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

        return self._fetch(pkgbase, pkgbuild_dir, rpc_entry)

    def _can_short_circuit(
        self, rpc_entry: dict | None, meta: dict | None, local_head: str | None,
    ) -> bool:
        if rpc_entry is None or meta is None:
            return False
        if rpc_entry.get("Version") != meta.get("rpc_version"):
            return False
        if rpc_entry.get("LastModified") != meta.get("rpc_last_modified"):
            return False
        if local_head != meta.get("head_commit"):
            return False
        return True

    def _clone(self, pkgbase: str, pkgbuild_dir: Path) -> SyncResult:
        self.limiter.wait_before_fetch()
        try:
            aur_clone(pkgbase, pkgbuild_dir, timeout=self.clone_timeout)
        except RuntimeError as e:
            err = str(e)
            if is_rate_limit_error(err):
                self.limiter.apply_retry_after(None, source="AUR clone 429/503")
                if self.limiter.remaining_penalty_s() > self.rate_limit_abort_s:
                    self._abort_remaining("rate-limit penalty exceeds threshold")
                return SyncResult(
                    pkgbase=pkgbase, status=STATUS_RATE_LIMITED, error=err,
                )
            return SyncResult(pkgbase=pkgbase, status=STATUS_FAILED, error=err)

        head = _head_commit(pkgbuild_dir)
        rpc_entry = self._rpc_entry(pkgbase)
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

    def _fetch(
        self, pkgbase: str, pkgbuild_dir: Path, rpc_entry: dict | None,
    ) -> SyncResult:
        outcome: GitFetchOutcome = git_fetch_and_compare(
            pkgbuild_dir, timeout=self.fetch_timeout, limiter=self.limiter,
        )

        if outcome.status == STATUS_RATE_LIMITED:
            if self.limiter.remaining_penalty_s() > self.rate_limit_abort_s:
                self._abort_remaining("rate-limit penalty exceeds threshold")
            return SyncResult(
                pkgbase=pkgbase, status=STATUS_RATE_LIMITED,
                head_before=outcome.head_before, head_after=outcome.head_after,
                error=outcome.error,
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


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_scheduler: SourceSyncScheduler | None = None


def get_scheduler(
    *,
    state_dir: Path | None = None,
    offline: bool = False,
    cleansrc: bool = False,
    force_devel: bool = False,
    min_fetch_interval_ms: int | None = None,
    rate_limit_abort_s: float | None = None,
    fetch_timeout: int | None = None,
    clone_timeout: int | None = None,
) -> SourceSyncScheduler:
    """Return the per-process scheduler, constructing it on first call.

    Subsequent calls update mutable runtime flags (``offline`` / ``cleansrc``
    / ``force_devel``) on the existing instance so callers in different
    commands (e.g. `sysforge fetch` after `sysforge update`) can share the
    metadata cache without re-reading ``source_meta.toml``.
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
            force_devel=force_devel,
            min_fetch_interval_ms=min_fetch_interval_ms,
            rate_limit_abort_s=rate_limit_abort_s,
            fetch_timeout=fetch_timeout,
            clone_timeout=clone_timeout,
        )
        atexit.register(_scheduler.close)
    else:
        _scheduler.offline = offline or _scheduler.offline
        _scheduler.cleansrc = cleansrc or _scheduler.cleansrc
        _scheduler.force_devel = force_devel or _scheduler.force_devel
    return _scheduler


def reset_scheduler() -> None:
    """Test helper — drop the module-level singleton."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.close()
    _scheduler = None
