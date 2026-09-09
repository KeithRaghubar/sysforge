# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/pkgbuilds.py — locating and syncing the suite's PKGBUILDs.

Everything between "the config names llvm, clang, lld" and "here are the
PKGBUILD paths to hand the build passes". Two responsibilities that belong
together because the second depends on the first: resolving each package name
to a PKGBUILD (repo checkout, AUR, or local tree) and bringing those source
trees up to date through the shared sync scheduler.

Source sync is routed through ``source_sync.get_scheduler`` rather than direct
git calls so the rate limiter, the freeze/diverge guards and the run log apply
here exactly as they do to ``update``.
"""
from pathlib import Path
import sys

from sysforge.primitives.llvm_state import collect_llvm_state
from sysforge.primitives.llvm_state import evaluate_strict
from sysforge.primitives.llvm_state import render_preflight
from sysforge.primitives.source_sync import STATUS_DIVERGED
from sysforge.primitives.source_sync import STATUS_FAILED
from sysforge.primitives.source_sync import STATUS_FROZEN
from sysforge.primitives.source_sync import STATUS_PURGE_REFUSED
from sysforge.primitives.source_sync import STATUS_RATE_LIMITED
from sysforge.primitives.source_sync import SyncRequest
from sysforge.primitives.source_sync import get_scheduler

from sysforge.primitives import prompt
from sysforge.primitives import config as prim_config
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


SYNC_BLOCKING_STATUSES = frozenset({
    STATUS_FAILED, STATUS_RATE_LIMITED, STATUS_PURGE_REFUSED, STATUS_FROZEN,
})


def sync_pkgbuild_dirs(
    pkgbuild_map: dict[str, "Path"],
    *,
    cleansrc: bool = False,
    cleansrc_force: bool = False,
) -> None:
    """
    Sync each unique resolved PKGBUILD directory through ``SourceSyncScheduler``.

    Mirrors the pattern in ``sysforge.update._sync_sources``: a single batched
    AUR RPC short-circuit followed by sequential per-pkgbase requests. Each
    pkgbase is classified as ``source="repo"`` (in any pacman sync DB) or
    ``source="aur"`` via a single batched ``pacman -Si`` (``repo_packages``)
    so that the scheduler's ``_clone`` path picks the right transport
    (``pkgctl_checkout`` for repo, ``aur_clone`` for AUR) on the first sync
    or after a ``--cleansrc`` purge. Without this classification, cleansrc
    on a repo package (clang/llvm/lld/...) would purge the tree then try
    to re-clone from AUR and silently leave the dir empty.

    ``cleansrc`` / ``cleansrc_force`` mirror the CLI flags: when set the
    scheduler purges + re-clones each tree, with ``cleansrc_force`` further
    bypassing the dirty-tree refusal in ``purge_src``.

    Blocker statuses (``STATUS_FAILED`` / ``STATUS_RATE_LIMITED`` /
    ``STATUS_PURGE_REFUSED`` / ``STATUS_FROZEN``) raise ``RuntimeError``;
    ``STATUS_DIVERGED`` is surfaced as a warning so users can opt to keep
    their local edits.
    """
    from sysforge.primitives.aur import repo_packages
    from sysforge.update_sync import _resolve_fetch_timeout

    git_cfg = (prim_config.load_sysforge_toml().get("git", {}) or {})
    aur_cfg = (prim_config.load_sysforge_toml().get("aur", {}) or {})
    fetch_timeout = _resolve_fetch_timeout(git_cfg)
    clone_timeout = git_cfg.get("clone_timeout", 60)

    scheduler = get_scheduler(
        cleansrc=cleansrc or cleansrc_force,
        cleansrc_force=cleansrc_force,
        repo_track=prim_config.resolve_repo_track(
            prim_config.load_sysforge_toml().get("build", {})
        ),
        min_fetch_interval_ms=aur_cfg.get("min_fetch_interval_ms", 500),
        rate_limit_abort_s=aur_cfg.get("rate_limit_abort_s", 120.0),
        fetch_timeout=fetch_timeout,
        clone_timeout=clone_timeout,
    )

    pkgbases: list[str] = []
    dirs: list[Path] = []
    seen: set[str] = set()
    for path in pkgbuild_map.values():
        pkgbuild_dir = path.parent if path.name == "PKGBUILD" else path
        key = str(pkgbuild_dir)
        if key in seen:
            continue
        seen.add(key)
        pkgbases.append(pkgbuild_dir.name)
        dirs.append(pkgbuild_dir)

    if not pkgbases:
        return

    in_repo = repo_packages(pkgbases) if pkgbases else set()

    reqs: list[SyncRequest] = [
        SyncRequest(
            pkgbase=pkgbase,
            pkgbuild_dir=pkgbuild_dir,
            source="repo" if pkgbase in in_repo else "aur",
        )
        for pkgbase, pkgbuild_dir in zip(pkgbases, dirs, strict=True)
    ]

    # Repo packages have no AUR-RPC entry; priming the RPC with their
    # names just wastes a request. Mirrors update.py:_sync_sources.
    aur_bases = [r.pkgbase for r in reqs if r.source != "repo"]
    if aur_bases:
        scheduler._ensure_rpc(aur_bases)

    failures: list[str] = []
    for req in reqs:
        result = scheduler.request(req)
        if result.status in SYNC_BLOCKING_STATUSES:
            failures.append(
                f"{req.pkgbase}: {result.status} — {result.error or result.status}"
            )
        elif result.status == STATUS_DIVERGED:
            _log.warn(
                f"{req.pkgbase}: {result.error or 'divergent upstream'} — "
                "build will use the local PKGBUILD; rerun with --cleansrc "
                "to discard local edits and re-clone"
            )

    scheduler.close()

    if failures:
        raise RuntimeError(
            "[TOOLCHAIN] PKGBUILD sync failed:\n  " + "\n  ".join(failures)
        )

# ---------------------------------------------------------------------------
# PKGBUILD resolution
# ---------------------------------------------------------------------------


def resolve_all_pkgbuilds(
    names: list[str], config: dict, *,
    update: bool = True,
    cleansrc: bool = False,
    cleansrc_force: bool = False,
) -> dict[str, Path]:
    """
    Resolve PKGBUILD paths for all package names.

    Three-pass strategy to handle split packages (e.g. llvm-libs comes from the
    llvm PKGBUILD and has no standalone clone target):
      1. Local direct: check pkgbuild_src_dir/<name>/PKGBUILD without cloning.
      2. Split scan: parse already-found PKGBUILDs for their pkgname arrays;
         reuse the path if a match is found.
      3. Full resolve: fall back to prim_config.find_pkgbuild() which may clone from AUR/repo.

    When ``update`` is True (default), every unique resolved PKGBUILD directory
    is then routed through ``SourceSyncScheduler`` so missing trees get cloned
    and pre-existing trees are refreshed against upstream — same RPC short-
    circuit, rate-limit, and dirty-tree handling as ``sysforge update``. Pass
    ``update=False`` (mapped from ``--no-update``) to use whatever is on disk
    verbatim. Blocker statuses raise ``RuntimeError``.

    Returns {name: pkgbuild_path}. Raises RuntimeError on any miss.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    resolved: dict[str, Path] = {}

    pkgbuild_dir: Path | None = None
    if config:
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            pkgbuild_dir = Path(raw).expanduser()

    # Pass 1 — local direct (no clone)
    remaining = []
    for name in names:
        if pkgbuild_dir:
            candidate = pkgbuild_dir / name / "PKGBUILD"
            if candidate.exists():
                resolved[name] = candidate.resolve()
                continue
        remaining.append(name)

    # Pass 2 — split-package scan: check pkgname arrays of already-resolved PKGBUILDs
    if remaining and resolved:
        coverage: dict[Path, set] = {}
        for path in set(resolved.values()):
            try:
                meta = parse_pkgbuild(path)
                pkgnames = meta.get("globals", {}).get("pkgname", [])
                if isinstance(pkgnames, str):
                    pkgnames = [pkgnames]
                coverage[path] = set(pkgnames)
            except Exception as e:
                _log.info(f"  split-package scan: parse failed for {path}: {e}")
                coverage[path] = set()

        still_remaining = []
        for name in remaining:
            matched = next(
                (p for p, provides in coverage.items() if name in provides), None
            )
            if matched:
                resolved[name] = matched
                _log.info(
                    f"  {name} → split package in {matched.parent.name}/"
                )
            else:
                still_remaining.append(name)
        remaining = still_remaining

    # Pass 3 — full resolution with potential clone; re-scan for split packages after each success
    errors = []
    remaining = list(remaining)
    i = 0
    while i < len(remaining):
        name = remaining[i]
        try:
            path = prim_config.find_pkgbuild(name, config)
            resolved[name] = path
            # Re-run split scan: the freshly cloned PKGBUILD may cover other remaining names
            try:
                meta = parse_pkgbuild(path)
                pkgnames = meta.get("globals", {}).get("pkgname", [])
                if isinstance(pkgnames, str):
                    pkgnames = [pkgnames]
                pkgbase = meta.get("globals", {}).get("pkgbase")
                covered = set(pkgnames)
                if pkgbase:
                    covered.add(pkgbase)
                satisfied = []
                for r in remaining[i + 1 :]:
                    if r in covered:
                        resolved[r] = path
                        satisfied.append(r)
                        _log.info(
                            f"  {r} → split package in {path.parent.name}/",
                        )
                for r in satisfied:
                    remaining.remove(r)
            except Exception as e:
                _log.info(f"  split-package re-scan: parse failed for {path}: {e}")
        except FileNotFoundError as e:
            errors.append(str(e))
        i += 1

    if errors:
        raise RuntimeError(
            "[TOOLCHAIN] Could not resolve PKGBUILDs:\n  " + "\n  ".join(errors)
        )

    if update or cleansrc or cleansrc_force:
        sync_pkgbuild_dirs(
            resolved,
            cleansrc=cleansrc,
            cleansrc_force=cleansrc_force,
        )

    return resolved


def _llvm_strict_enabled() -> bool:
    """Read [safety] llvm_strict_toolchain from sysforge.toml. Default True."""
    safety = prim_config.load_sysforge_toml().get("safety", {}) or {}
    return bool(safety.get("llvm_strict_toolchain", True))


def run_llvm_preflight(names: list[str], config: dict, options) -> None:
    """Surface LLVM source state and (when strict) refuse on dirty/diverged.

    Always renders the report so users see the situation. When strict mode
    is enabled (the default for the toolchain stage) and the report has
    blockers, the stage is aborted unless ``options.allow_dirty_llvm`` is
    set or the user accepts the prompt interactively.

    A PGO profdata version mismatch is never suppressible — building
    against a stale profdata silently corrupts the output.
    """
    report = collect_llvm_state(names, config, probe_fetch=True)
    if not report.states:
        return

    rendered = render_preflight(report, verbose=True)
    if rendered:
        _log.ui(rendered)

    allow_dirty = bool(getattr(options, "allow_dirty_llvm", False))
    if not _llvm_strict_enabled() and not report.has_pgo_profdata_mismatch:
        return

    blockers = evaluate_strict(report, allow_dirty=allow_dirty)
    if not blockers:
        return

    blocker_lines = "\n".join(f"  - {b}" for b in blockers)
    if not prompt.is_interactive():
        raise RuntimeError(
            "[TOOLCHAIN] LLVM safety pre-flight refused — strict mode "
            "blocked the run on the following:\n"
            f"{blocker_lines}\n"
            "Re-run with --allow-dirty-llvm to bypass dirty/diverged "
            "blockers (PGO profdata mismatches cannot be bypassed)."
        )

    _log.warn("LLVM safety pre-flight has blockers:")
    for b in blockers:
        _log.warn(f"  {b}")
    choice = prompt.prompt_choice(
        "Proceed with toolchain build despite LLVM blockers? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        tag="TOOLCHAIN",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        raise RuntimeError(
            "[TOOLCHAIN] LLVM safety pre-flight aborted by user."
        )


def show_resolution_table(
    pkgbuild_map: dict[str, Path], role_map: dict[str, str] | None = None
) -> None:
    _log.ui("─── PKGBUILD resolution ─────────────────────────────")
    for name, path in pkgbuild_map.items():
        role = f"  [{role_map[name]}]" if role_map and name in role_map else ""
        _log.ui(f"  {name:<36}  {path}{role}")
    _log.ui("─────────────────────────────────────────────────────")


def confirm_or_abort(state_dir) -> None:
    """Prompt user to confirm. On abort, print resume command and raise.

    EOF (non-interactive) defaults to "y" so unattended runs proceed without
    prompting — matches the long-standing behaviour of this stage.
    """
    choice = prompt.prompt_choice(
        "Proceed with toolchain build? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="y",
        tag="TOOLCHAIN",
    )
    if choice not in ("y", "yes"):
        dir_str = str(state_dir) if state_dir else "/var/lib/sysforge"
        print(
            f"\n  Resume command: sysforge run pipeline --resume --state-dir {dir_str}\n",
            file=sys.stderr,
        )
        raise RuntimeError(
            "[TOOLCHAIN] Aborted by user. Use the resume command to return."
        )
