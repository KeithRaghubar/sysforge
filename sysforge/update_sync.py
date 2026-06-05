"""Phase 2 of ``update``: source synchronisation.

``_sync_sources`` ensures every in-scope package has an up-to-date local
PKGBUILD before the version check runs. It is a thin driver over
``SourceSyncScheduler`` — it builds the request list (skipping pacman-class
and non-``--devel`` VCS packages, deduplicating by directory), primes the
batched AUR RPC call, then dispatches sequential fetches and folds the
results into the ``{pkgbase: (status, error)}`` blocking map the version
check consumes.

All real sync work lives in ``primitives.source_sync`` (the scheduler);
``update_sync`` only owns the per-run policy of which packages to sync and
how to summarise the outcome. ``update.py`` re-exports ``_sync_sources`` so
existing call sites and tests are unchanged.
"""

from pathlib import Path

from sysforge import log
from sysforge.primitives.source_sync import (
    SyncRequest, SyncResult, STATUS_DIVERGED, get_scheduler,
)
from sysforge.primitives.config import load_sysforge_toml
from sysforge.pipeline.state import resolve_state_dir
from sysforge.update_common import _SYNC_BLOCKING_STATUSES, _is_vcs

_log = log.get_logger("UPDATE")


def _sync_sources(
    pkgbase_map: dict[str, list],
    pkgbase_entry: dict[str, dict],
    args,
) -> dict[str, tuple[str, str]]:
    """Ensure every package has an up-to-date local PKGBUILD.

    Delegates to ``SourceSyncScheduler``: one batched AUR RPC call, then
    sequential shallow fetches for pkgbases whose Version/LastModified/HEAD
    have drifted from ``source_meta.toml``. Returns
    ``{pkgbase: (status, error_message)}`` for packages that blocked on sync;
    the status is a ``STATUS_*`` constant from source_sync that determines
    the per-package action shown in the update summary.
    """
    offline = getattr(args, "offline", False)
    dry_run = getattr(args, "dry_run", False)
    cleansrc_force = getattr(args, "cleansrc_force", False) and not dry_run
    cleansrc = (cleansrc_force or getattr(args, "cleansrc", False)) and not dry_run
    force_devel = getattr(args, "devel", False)

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    sysforge_toml = load_sysforge_toml()
    git_cfg = sysforge_toml.get("git", {})
    aur_cfg = sysforge_toml.get("aur", {})
    # Accept legacy pull_timeout as an alias for fetch_timeout.
    fetch_timeout = git_cfg.get("fetch_timeout", git_cfg.get("pull_timeout", 30))
    clone_timeout = git_cfg.get("clone_timeout", 60)

    scheduler = get_scheduler(
        state_dir=state_dir,
        offline=offline,
        cleansrc=cleansrc,
        cleansrc_force=cleansrc_force,
        force_devel=force_devel,
        min_fetch_interval_ms=aur_cfg.get("min_fetch_interval_ms", 500),
        rate_limit_abort_s=aur_cfg.get("rate_limit_abort_s", 120.0),
        fetch_timeout=fetch_timeout,
        clone_timeout=clone_timeout,
    )

    if offline and not cleansrc:
        return {}

    reqs: list[SyncRequest] = []
    seen_dirs: set[str] = set()
    for pkgbase in sorted(pkgbase_map):
        entry = pkgbase_entry[pkgbase]
        source = entry.get("source", "aur")
        # Pacman-class repo packages skip source sync entirely — their
        # upgrade detection runs through ``checkupdates_map`` in Phase 3
        # and the upgrade itself is dispatched as one ``sudo pacman -Syu``
        # after the source-build loop. Avoids hundreds of pkgctl/git
        # fetches for packages that ultimately follow the pacman path.
        if entry.get("repo_class") == "pacman":
            continue
        # VCS packages without ``--devel`` are skipped at the build step
        # (action ``DEVEL``), so source sync — including ``--cleansrc``
        # purge + re-clone — is wasted work. Filter them out here so the
        # progress tracker, status summary, and ``purge_src`` never touch
        # ``-git`` / ``-svn`` / ``-hg`` / ``-bzr`` trees unless the user
        # explicitly asked to rebuild them.
        if _is_vcs(pkgbase) and not force_devel:
            continue
        pkgbuild_dir = Path(entry["pkgbuild_dir"])
        resolved = str(pkgbuild_dir)
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)
        reqs.append(SyncRequest(
            pkgbase=pkgbase, pkgbuild_dir=pkgbuild_dir, source=source,
        ))

    # Prime the RPC batch once so every subsequent request() can hit the
    # short-circuit path. Without this the scheduler only runs _ensure_rpc
    # inside sync_many(), and the per-request loop below would fetch every
    # package on every run.
    aur_bases = [r.pkgbase for r in reqs if r.source in ("aur", "git")]
    if aur_bases:
        scheduler._ensure_rpc(aur_bases)

    from sysforge.ui import progress as _ui_progress
    results: dict[str, SyncResult] = {}
    with _ui_progress.tracker(len(reqs), "source sync") as _tick:
        for req in reqs:
            _tick(req.pkgbase)
            results[req.pkgbase] = scheduler.request(req)

    # Summarise per-status counts once at INFO for operator visibility.
    by_status: dict[str, int] = {}
    for r in results.values():
        by_status[r.status] = by_status.get(r.status, 0) + 1
    if by_status:
        _log.info("source sync: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    scheduler.close()

    sync_failures: dict[str, tuple[str, str]] = {}
    for pkgbase, result in results.items():
        if result.status in _SYNC_BLOCKING_STATUSES:
            sync_failures[pkgbase] = (result.status, result.error or result.status)
        elif result.status == STATUS_DIVERGED:
            # Divergence is not a hard failure: local PKGBUILD is kept; build
            # proceeds against it. Surface as a warning, not a blocker.
            _log.warn(
                f"{pkgbase}: {result.error or 'divergent upstream'} — "
                "build will use the local PKGBUILD; rerun with --cleansrc "
                "to discard local edits and re-clone"
            )
    return sync_failures
