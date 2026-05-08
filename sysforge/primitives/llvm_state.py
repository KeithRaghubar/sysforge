"""
llvm_state.py — LLVM source-tree safety pre-flight.

Snapshots the on-disk state of LLVM-toolchain packages (which variant is
present, where it came from, whether it has uncommitted work, whether it
diverges from upstream, what's installed via pacman, what build mode would
fire) so commands that touch ``~/src/<llvm-pkg>/`` can surface the situation
before acting and (for ``run toolchain``) refuse to proceed when the tree is
unsafe.

This module is the only allowed entry point for new code that wants to inspect
LLVM source state — direct ``git_is_dirty`` + URL parsing in callers tends to
drift out of sync with the rules below.

Public API:
    is_llvm_in_scope(pkgnames)                           -> list[str]
    collect_llvm_state(pkgnames, config, *,
                       probe_fetch=False, offline=False) -> LlvmPreflightReport
    render_preflight(report, *, verbose=False)           -> str
    evaluate_strict(report, *, allow_dirty=False)        -> list[str]

Wiring:
    fetch / update / build / converge — informational render only.
    run toolchain                     — render + ``evaluate_strict``; on
                                        blockers, refuse-by-default.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur import (
    GitFetchOutcome,
    git_fetch_and_compare,
)
from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase

_log = log.get_logger("LLVM")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LlvmPackageState:
    pkgbase: str
    pkgbuild_dir: Path | None
    variant: str
    source_origin: str           # "repo" | "aur" | "user" | "missing"
    remote_url: str | None
    is_dirty: bool
    dirty_reason: str | None
    # one of: up_to_date | behind | ahead | diverged | no_tracking | unknown | missing
    divergence: str
    head_short: str | None
    upstream_short: str | None
    install_origin: str          # "repo" | "foreign" | "not_installed"
    installed_ver: str | None
    pkgbuild_ver: str | None
    build_mode: str | None
    pgo_profdata_mismatch: bool


@dataclass(frozen=True)
class LlvmPreflightReport:
    states: tuple[LlvmPackageState, ...]
    blockers: tuple[str, ...]
    has_dirty: bool
    has_diverged: bool
    has_pgo_profdata_mismatch: bool


# ---------------------------------------------------------------------------
# Variant + origin classification
# ---------------------------------------------------------------------------

def _classify_variant(pkgbase: str) -> str:
    """Return a short label for which LLVM variant pkgbase represents.

    Examples: ``llvm`` → ``"llvm"``, ``llvm-git`` → ``"llvm-git"``,
    ``llvm-minimal-git`` → ``"llvm-minimal-git"``, ``lib32-clang`` →
    ``"lib32-clang"``. The full pkgbase is the most useful label here — every
    variant has a distinct name on disk — so we just echo it.
    """
    return pkgbase


def _git_remote_url(pkgbuild_dir: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _classify_origin(pkgbuild_dir: Path | None) -> tuple[str, str | None]:
    """Return ``(origin, remote_url)``.

    ``origin`` is one of:
        ``"missing"``  — directory does not exist
        ``"repo"``     — clone from gitlab.archlinux.org or has a
                         ``.git/pkgctl-source`` sentinel (``pkgctl repo clone``)
        ``"aur"``      — clone from aur.archlinux.org
        ``"user"``     — anything else (custom remote, fork, no remote)
    """
    if pkgbuild_dir is None or not pkgbuild_dir.exists():
        return "missing", None

    sentinel = pkgbuild_dir / ".git" / "pkgctl-source"
    if sentinel.exists():
        return "repo", _git_remote_url(pkgbuild_dir)

    url = _git_remote_url(pkgbuild_dir)
    if url is None:
        return "user", None
    if "aur.archlinux.org" in url:
        return "aur", url
    if "gitlab.archlinux.org" in url:
        return "repo", url
    return "user", url


# ---------------------------------------------------------------------------
# Local-only PKGBUILD resolution (mirror of config.find_pkgbuild steps 1-3,
# without the auto-clone branch)
# ---------------------------------------------------------------------------

def _resolve_local_only(pkg: str, config: dict | None) -> Path | None:
    """Return the on-disk PKGBUILD path for ``pkg`` without cloning.

    Mirrors steps 1-3 of :func:`sysforge.primitives.config.find_pkgbuild`:
    explicit path → ``<cwd>/<pkg>/PKGBUILD`` → ``<pkgbuild_src_dir>/<pkg>/PKGBUILD``.
    Returns ``None`` when nothing is found locally; callers must NOT trigger
    a clone — the whole point of the pre-flight is to inspect what's already
    on disk.
    """
    p = Path(pkg)
    if p.is_dir():
        p = p / "PKGBUILD"
    if p.exists():
        return p.resolve()

    cwd_candidate = Path.cwd() / pkg / "PKGBUILD"
    if cwd_candidate.exists():
        return cwd_candidate.resolve()

    if config:
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            dir_candidate = Path(raw).expanduser() / pkg / "PKGBUILD"
            if dir_candidate.exists():
                return dir_candidate.resolve()

    return None


# ---------------------------------------------------------------------------
# Dirty + divergence probing
# ---------------------------------------------------------------------------

def _dirty_reason(pkgbuild_dir: Path) -> tuple[bool, str | None]:
    """Re-derive the reason for a dirty tree so we can render it."""
    if not pkgbuild_dir.exists():
        return False, None

    is_repo = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    ).returncode == 0
    if not is_repo:
        return False, None

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "status",
         "--short", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return True, "uncommitted changes"

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse",
         "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return True, "no upstream tracking branch"

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-list", "--count", "@{u}..HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        count = r.stdout.strip()
        if count.isdigit() and int(count) > 0:
            return True, f"{count} unpushed commit(s)"

    return False, None


def _head_commit(pkgbuild_dir: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _short(sha: str | None) -> str | None:
    return sha[:10] if sha else None


def _divergence_from_outcome(outcome: GitFetchOutcome) -> str:
    s = outcome.status
    if s == "up_to_date":
        return "up_to_date"
    if s == "fetched":
        return "behind"
    if s == "diverged":
        return "diverged"
    if s == "no_tracking":
        return "no_tracking"
    return "unknown"


def _divergence_from_cache(
    pkgbuild_dir: Path, cached: dict | None,
) -> tuple[str, str | None, str | None]:
    """Cheap divergence guess using the SourceMetaCache snapshot.

    Returns ``(divergence, head_short, upstream_short)``. Without a cache
    entry we cannot tell whether HEAD is up-to-date — report ``"unknown"``
    rather than lie.
    """
    head = _head_commit(pkgbuild_dir)
    if cached is None or "head_commit" not in cached:
        return "unknown", _short(head), None

    cached_head = cached["head_commit"]
    if head is None:
        return "unknown", None, _short(cached_head)
    if head == cached_head:
        return "up_to_date", _short(head), _short(cached_head)
    # The cache records the head observed on the last successful fetch. If
    # the local HEAD has moved past it, treat as ``ahead`` (user committed
    # locally); if it doesn't match, the most we can say without a probe is
    # that the tree has drifted from the last sync.
    return "ahead", _short(head), _short(cached_head)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_llvm_in_scope(pkgnames) -> list[str]:
    """Filter ``pkgnames`` down to those that look like LLVM-toolchain pkgbases."""
    return [n for n in pkgnames if is_llvm_pkgbase(n)]


def collect_llvm_state(
    pkgnames,
    config: dict | None,
    *,
    probe_fetch: bool = False,
    offline: bool = False,
) -> LlvmPreflightReport:
    """Snapshot the LLVM-package state for ``pkgnames``.

    Filters ``pkgnames`` via :func:`is_llvm_pkgbase` first, so callers can
    safely pass the full set in scope for their command.

    ``probe_fetch=True`` runs an actual ``git fetch`` per package via
    :func:`sysforge.primitives.aur.git_fetch_and_compare`. Reserve this for
    long-running commands (the toolchain stage) that have already decided
    they're willing to spend the round trips. Default is the cheap path:
    compare HEAD against the cached ``SourceMetaCache.head_commit`` from a
    prior sync. ``offline=True`` forces the cheap path even when
    ``probe_fetch`` is requested.

    Reads pacman + the SourceMetaCache for context fields. Never mutates
    state, never clones, never installs.
    """
    in_scope = is_llvm_in_scope(pkgnames)
    if not in_scope:
        return LlvmPreflightReport(
            states=(), blockers=(),
            has_dirty=False, has_diverged=False,
            has_pgo_profdata_mismatch=False,
        )

    # Lazy imports — keep llvm_state cheap to import.
    from sysforge.primitives.makepkg_wrapper import _resolve_pgo_state
    from sysforge.primitives.pacman import (
        get_foreign_packages,
        get_installed_version,
    )
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    from sysforge.primitives.profile import get_build_mode, match_rules
    from sysforge.primitives.source_sync import get_scheduler
    from sysforge.primitives.version import format_version

    foreign = get_foreign_packages()

    cache_all: dict[str, dict] = {}
    try:
        cache_all = get_scheduler().cache.all()
    except Exception:  # noqa: BLE001 — best-effort; absent cache is fine
        cache_all = {}

    states: list[LlvmPackageState] = []
    blockers: list[str] = []
    has_dirty = False
    has_diverged = False
    has_pgo_mismatch = False

    for name in in_scope:
        pkgbuild_path = _resolve_local_only(name, config)
        pkgbuild_dir = pkgbuild_path.parent if pkgbuild_path else None

        origin, remote_url = _classify_origin(pkgbuild_dir)

        is_dirty = False
        dirty_reason: str | None = None
        divergence = "missing"
        head_short: str | None = None
        upstream_short: str | None = None
        pkgbuild_ver: str | None = None
        build_mode: str | None = None
        pgo_mismatch = False

        if pkgbuild_dir is not None and pkgbuild_dir.exists():
            is_dirty, dirty_reason = _dirty_reason(pkgbuild_dir)
            if is_dirty:
                has_dirty = True

            if offline or not probe_fetch:
                divergence, head_short, upstream_short = _divergence_from_cache(
                    pkgbuild_dir, cache_all.get(name),
                )
            else:
                outcome = git_fetch_and_compare(pkgbuild_dir)
                divergence = _divergence_from_outcome(outcome)
                # For ``diverged``, head_after is the upstream FETCH_HEAD —
                # the local HEAD is in head_before. For other statuses
                # head_after reflects the post-fetch local HEAD.
                local_head = (
                    outcome.head_before if outcome.status == "diverged"
                    else (outcome.head_after or outcome.head_before)
                )
                head_short = _short(local_head)
                upstream_short = _short(outcome.head_after)
            if divergence == "diverged":
                has_diverged = True

            if pkgbuild_path is not None:
                try:
                    pkgmeta = parse_pkgbuild(pkgbuild_path)
                    pkgbuild_ver = format_version(pkgmeta.get("globals", {}))
                    matched = match_rules(pkgmeta, (config or {}).get("rules", []))
                    build_mode = get_build_mode(matched, config or {})
                except (OSError, KeyError, ValueError) as e:
                    _log.warn(f"{name}: PKGBUILD parse failed: {e}")

                if build_mode == "pgo_llvm_toolchain":
                    state, _ = _resolve_pgo_state(pkgbuild_path)
                    pgo_mismatch = (state == "mismatch")
                    if pgo_mismatch:
                        has_pgo_mismatch = True

        if name in foreign:
            install_origin = "foreign"
            installed_ver = foreign[name]
        else:
            installed_ver = get_installed_version(name)
            install_origin = "repo" if installed_ver else "not_installed"

        states.append(LlvmPackageState(
            pkgbase=name,
            pkgbuild_dir=pkgbuild_dir,
            variant=_classify_variant(name),
            source_origin=origin,
            remote_url=remote_url,
            is_dirty=is_dirty,
            dirty_reason=dirty_reason,
            divergence=divergence,
            head_short=head_short,
            upstream_short=upstream_short,
            install_origin=install_origin,
            installed_ver=installed_ver,
            pkgbuild_ver=pkgbuild_ver,
            build_mode=build_mode,
            pgo_profdata_mismatch=pgo_mismatch,
        ))

    for s in states:
        if s.is_dirty:
            blockers.append(
                f"{s.pkgbase}: dirty ({s.dirty_reason or 'unknown'})"
            )
        if s.divergence == "diverged":
            blockers.append(
                f"{s.pkgbase}: diverged from upstream "
                f"(HEAD {s.head_short} vs upstream {s.upstream_short})"
            )
        if s.pgo_profdata_mismatch:
            blockers.append(
                f"{s.pkgbase}: PGO profdata version mismatch"
            )

    return LlvmPreflightReport(
        states=tuple(states),
        blockers=tuple(blockers),
        has_dirty=has_dirty,
        has_diverged=has_diverged,
        has_pgo_profdata_mismatch=has_pgo_mismatch,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_TAG = "LLVM"


def _format_version_pair(installed: str | None, pkgbuild: str | None) -> str:
    inst = installed or "—"
    pkg = pkgbuild or "—"
    if installed and pkgbuild and installed == pkgbuild:
        return f"{inst} (=)"
    return f"{inst} → {pkg}"


def render_preflight(report: LlvmPreflightReport, *, verbose: bool = False) -> str:
    """Render a human-readable pre-flight table.

    Output style matches the action-tag block from
    :func:`sysforge.update._print_summary` (``  [TAG] ...`` with a 17-col
    gutter). Returns the rendered text rather than printing it so callers
    can route through ``log.ui`` / ``print`` themselves.
    """
    if not report.states:
        return ""

    header = f"  [{_TAG}]" + " " * max(1, 17 - len(_TAG) - 2)
    lines: list[str] = []
    lines.append(
        f"{header}LLVM source pre-flight ({len(report.states)} package"
        f"{'s' if len(report.states) != 1 else ''})"
    )

    for s in report.states:
        clean = "clean" if not s.is_dirty else f"DIRTY ({s.dirty_reason})"
        sync = s.divergence
        if s.head_short and s.upstream_short and sync != "up_to_date":
            sync = f"{sync} ({s.head_short}→{s.upstream_short})"
        elif s.head_short and verbose:
            sync = f"{sync} ({s.head_short})"

        ver = _format_version_pair(s.installed_ver, s.pkgbuild_ver)
        mode = s.build_mode or "—"
        line = (
            f"    {s.pkgbase:<24} "
            f"origin={s.source_origin:<7} "
            f"clean={clean:<28} "
            f"sync={sync:<32} "
            f"installed={s.install_origin:<14} "
            f"ver={ver}  mode={mode}"
        )
        lines.append(line)
        if verbose and s.remote_url:
            lines.append(f"      remote: {s.remote_url}")

    if report.blockers:
        lines.append("")
        lines.append(f"{header}blockers:")
        for b in report.blockers:
            lines.append(f"    - {b}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Strict evaluation
# ---------------------------------------------------------------------------

def evaluate_strict(
    report: LlvmPreflightReport, *, allow_dirty: bool = False,
) -> list[str]:
    """Return blocker strings (empty list = OK to proceed).

    Used by ``run toolchain`` to refuse-by-default when the LLVM tree is
    unsafe. ``allow_dirty=True`` suppresses dirty + diverged blockers (set
    by ``--allow-dirty-llvm``); the PGO profdata-mismatch blocker is not
    suppressible — building against a stale profdata silently corrupts the
    output.
    """
    blockers: list[str] = []
    for s in report.states:
        if not allow_dirty:
            if s.is_dirty:
                blockers.append(
                    f"{s.pkgbase}: dirty ({s.dirty_reason or 'unknown'})"
                )
            if s.divergence == "diverged":
                blockers.append(
                    f"{s.pkgbase}: diverged from upstream "
                    f"(HEAD {s.head_short} vs upstream {s.upstream_short})"
                )
        if s.pgo_profdata_mismatch:
            blockers.append(
                f"{s.pkgbase}: PGO profdata version mismatch"
            )
    return blockers
