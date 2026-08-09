# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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
    fetch / update / build — informational render only.
    run toolchain          — render + ``evaluate_strict``; on
                             blockers, refuse-by-default.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.aur import (
    GitFetchOutcome,
    classify_head_vs_upstream,
    git_fetch_and_compare,
    head_reachable_from_remote,
)
from sysforge.primitives.net_policy import NetworkFrozen
from sysforge.primitives.pkgbuild_patcher import is_llvm_pkgbase
from sysforge.primitives.render import arrow, tag_header, version_pair

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
    # pkgbase this package is a split member of (e.g. llvm-libs → "llvm"), or
    # None when it has its own source tree. A split member ships no PKGBUILD dir
    # of its own, so its source-state columns are meaningless; the renderer
    # collapses them to a "(split of <base>)" annotation instead of noise.
    split_of: str | None = None


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
    """Re-derive the reason for a dirty tree so we can render it.

    Tracks the dirty-tree gating actually used by ``purge_src`` so the report
    cannot disagree with what a purge would refuse. Distinguishes
    ``"N commit(s) ahead of upstream"`` (true unpushed work) from
    ``"diverged from upstream (N local / M upstream)"`` (forked histories with
    at least one local-user-authored commit), and treats upstream-only
    divergence (``diverged_upstream``) as clean. Unlike the blanket
    ``git_is_dirty`` rule, a ``no_tracking`` detached HEAD whose commit is
    still reachable from a remote ref is upstream's own history (a release-tag
    checkout), not local work, and is reported clean — matching
    ``purge_src``'s ``head_reachable_from_remote`` escape hatch.
    """
    if not pkgbuild_dir.exists():
        return False, None

    state, n_local, n_upstream = classify_head_vs_upstream(pkgbuild_dir)
    if state in ("not_a_repo", "no_head"):
        return False, None

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "status",
         "--short", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return True, "uncommitted changes"

    if state == "no_tracking":
        # A source=repo checkout pinned to a release tag sits on a detached
        # HEAD (no ``@{u}``) whose commit is still reachable from a remote ref
        # — that is upstream's own history, not local work. ``purge_src``
        # already treats this as clean via the same predicate; mirror it so
        # the preflight report cannot disagree with the purge gating (and so a
        # false "dirty" blocker doesn't wedge the toolchain stage where no
        # --cleansrc-force can ever clear it).
        if head_reachable_from_remote(pkgbuild_dir):
            return False, None
        return True, "no upstream tracking branch"
    if state == "ahead":
        suffix = "s" if n_local != 1 else ""
        return True, f"{n_local} commit{suffix} ahead of upstream"
    if state == "diverged_user":
        return True, (
            f"diverged from upstream ({n_local} local / {n_upstream} upstream)"
        )
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
    from sysforge.primitives.source_sync import get_scheduler, is_vcs_pkgbase
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

    # Map split members → their pkgbase (e.g. llvm-libs → "llvm"). A member with
    # no PKGBUILD dir of its own is covered by whichever in-scope base lists it in
    # its ``pkgname`` array; the renderer uses this to annotate the row instead of
    # showing empty source-state columns. Best-effort — a parse failure just
    # leaves the member unannotated (falls back to the old row).
    split_parent: dict[str, str] = {}
    for base in in_scope:
        base_path = _resolve_local_only(base, config)
        if base_path is None:
            continue
        try:
            members = parse_pkgbuild(base_path).get("globals", {}).get("pkgname", [])
        except (OSError, KeyError, ValueError):
            continue
        if isinstance(members, str):
            members = [members]
        for member in members:
            if member != base:
                split_parent.setdefault(member, base)

    for name in in_scope:
        pkgbuild_path = _resolve_local_only(name, config)
        pkgbuild_dir = pkgbuild_path.parent if pkgbuild_path else None
        # A member with no source tree of its own but covered by a sibling base
        # is a split package — flag it so the renderer collapses its dead columns.
        split_of = split_parent.get(name) if pkgbuild_dir is None else None

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
                try:
                    # is_vcs suppresses the false "local modifications"
                    # warning on an ``llvm-git``-style tree carrying only a
                    # pkgver() bump (2.6.1-B1); the reported divergence
                    # itself is unchanged. ``pkgbase=name`` threads the
                    # authoritative --thaw name through to the freeze check
                    # (3.0.0-F2) rather than relying on the dir-name fallback.
                    outcome = git_fetch_and_compare(
                        pkgbuild_dir, is_vcs=is_vcs_pkgbase(name), pkgbase=name,
                    )
                except NetworkFrozen:
                    # Source freeze (3.0.0-F2): this is a read-only status
                    # probe, not a build — fall back to the cached divergence
                    # the same way the offline branch above does, rather than
                    # letting the refusal escape as an unhandled error.
                    divergence, head_short, upstream_short = (
                        _divergence_from_cache(pkgbuild_dir, cache_all.get(name))
                    )
                else:
                    divergence = _divergence_from_outcome(outcome)
                    # For ``diverged``, head_after is the upstream FETCH_HEAD
                    # — the local HEAD is in head_before. For other statuses
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
            split_of=split_of,
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
# Configured-vs-installed toolchain provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolchainMismatchFinding:
    check_id: str
    severity: str        # "error" | "warning" — drives doctor's exit code
    message: str
    remediation: str


def _load_toolchain_cfg() -> dict | None:
    """Read ``toolchain.toml`` (intent), or None when absent/unparseable."""
    import tomllib

    from sysforge.primitives.paths import TOOLCHAIN_PATH

    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with TOOLCHAIN_PATH.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _toolchain_built_packages() -> set[str]:
    """Pkgbases the toolchain stage built and installed, per build_state.

    The toolchain stage replaces LLVM **in place with stock pkgnames** (it is
    the system compiler — no ``-sysforge`` suffix), so pacman classifies the
    result as a repo package (``install_origin == "repo"``) exactly like a stock
    install. The only authoritative "sysforge built this toolchain" signal is the
    sticky ``owner_stage == "toolchain"`` marker the install-bearing pass stamps
    into ``build_state.toml`` (never auto-demoted — see
    ``BuildState.reconcile_external_installs``). Read-only; never throws (a
    missing/unreadable state dir → empty set, so the caller falls back to
    treating the install as stock).
    """
    try:
        from sysforge.pipeline.state import resolve_state_dir
        from sysforge.primitives.build_state import BuildState

        resolved_dir, _ = resolve_state_dir(None)
        bs = BuildState(resolved_dir)
        return {
            name
            for name, entry in bs.all_packages().items()
            if isinstance(entry, dict) and entry.get("owner_stage") == "toolchain"
        }
    except Exception as e:  # noqa: BLE001 — advisory; absent state is fine
        _log.debug(f"toolchain build_state read skipped: {e}")
        return set()


def _packages_repo_mode_is_pacman() -> bool:
    """True when packages.toml ``[build] repo_mode`` resolves to ``pacman``.

    With ``pgo = false`` the toolchain stage honors this by installing the stock
    LLVM suite from the repos (``install_repo_pkgs``) instead of building — so a
    stock install is the *intended* outcome, not a provenance mismatch. Read
    through the single ``config.resolve_repo_mode`` chokepoint; never throws.
    """
    try:
        import tomllib

        from sysforge.primitives.config import REPO_MODE_PACMAN, resolve_repo_mode
        from sysforge.primitives.paths import resolve_packages_path

        path = resolve_packages_path({})
        if not path.exists():
            return False
        with path.open("rb") as f:
            data = tomllib.load(f)
        return resolve_repo_mode(data.get("build", {})) == REPO_MODE_PACMAN
    except Exception as e:  # noqa: BLE001 — advisory; default to "not pacman"
        _log.debug(f"packages.toml repo_mode read skipped: {e}")
        return False


def detect_toolchain_config_mismatch(
    config: dict | None,
    *,
    toolchain_cfg: dict | None = None,
) -> tuple[ToolchainMismatchFinding, ...]:
    """Report when ``toolchain.toml`` asks for a custom LLVM toolchain but the
    installed LLVM doesn't match the configuration.

    This is **provenance reporting**, built strictly on :func:`collect_llvm_state`
    (the sanctioned LLVM-inspection entry point) — it is *not* a toolchain
    *health* probe (those live in ``pipeline/stages/toolchain.py::_verify_llvm_install``
    and ``toolchain_preflight.py::_probe_cc``; don't add a third). Two classes,
    both gated on ``enabled = true`` + ``compiler = "llvm"`` in toolchain.toml:

      * **stock-repo install** — a custom toolchain build (PGO or single-pass)
        is never published to a pacman sync DB, so an ``install_origin == "repo"``
        LLVM means the configured custom toolchain was never built/installed.
      * **PGO profdata skew** (PGO only) — :func:`collect_llvm_state` flags the
        stored profdata as version-incompatible with the target LLVM.

    Returns an empty tuple when toolchain.toml isn't configured for a custom LLVM
    build, or when the install is consistent with the configuration.

    ``toolchain_cfg`` may be injected (tests); otherwise toolchain.toml is read.
    """
    cfg = toolchain_cfg if toolchain_cfg is not None else _load_toolchain_cfg()
    if not cfg:
        return ()
    # compiler defaults to "gcc" when unset (register-only — owns no LLVM).
    if cfg.get("enabled") is not True or (cfg.get("compiler") or "gcc") != "llvm":
        return ()
    # skip_build = true means "register the installed clang as-is, don't build"
    # (pipeline/stages/toolchain.py). Stock-vs-custom is then a deliberate choice,
    # not a provenance mismatch — and the "run sysforge run toolchain" remediation
    # would be wrong (skip_build doesn't build). Suppress like the gcc path.
    if cfg.get("skip_build", False):
        return ()
    pgo = bool(cfg.get("pgo", True))  # PGO defaults on for the llvm path

    # pgo off + packages.toml repo_mode=pacman: the stage installs the stock
    # LLVM suite from the repos on purpose (no build), so a stock install is the
    # chosen path, not a mismatch. (PGO always builds from source regardless of
    # repo_mode, so this only applies when pgo is off.) Nothing else can fire
    # here — the profdata-skew arm is pgo-only — so return early.
    if not pgo and _packages_repo_mode_is_pacman():
        return ()

    # Lazy import: the lockstep suite is the single source of truth for which
    # LLVM packages move together (CLAUDE.md). Imported here to avoid an
    # import-time cycle with toolchain_preflight.
    from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE

    # Provenance reporting must never throw — a failed snapshot (no pacman, etc.)
    # degrades to "no findings" rather than breaking the kernel build or doctor.
    try:
        report = collect_llvm_state(list(LLVM_LOCKSTEP_SUITE), config)
    except Exception as e:  # noqa: BLE001 — advisory; never fatal
        _log.debug(f"toolchain mismatch check skipped: {e}")
        return ()
    findings: list[ToolchainMismatchFinding] = []

    # A custom/PGO build installs stock-named packages in place, so pacman reads
    # them as repo (install_origin == "repo") — indistinguishable from a genuine
    # stock install by pacman alone. Subtract anything build_state records as a
    # toolchain-owned build: that is sysforge's authoritative "we built it"
    # signal, so it is NOT a stock install (B5). When build_state is empty (the
    # stage was never run) the set is empty and a real stock install still fires.
    built = _toolchain_built_packages()
    stock = sorted(
        s.pkgbase
        for s in report.states
        if s.install_origin == "repo" and s.pkgbase not in built
    )
    if stock:
        kind = "PGO LLVM" if pgo else "custom LLVM"
        findings.append(ToolchainMismatchFinding(
            check_id="toolchain_stock_install",
            severity="error",
            message=(
                f"toolchain.toml requests a {kind} toolchain, but stock repo LLVM "
                f"is installed ({', '.join(stock)}). Builds use the stock "
                "compiler, not the configured custom toolchain."
            ),
            remediation=(
                "run `sysforge run toolchain` to build/install the custom "
                "toolchain, or set `compiler = \"gcc\"` in toolchain.toml"
            ),
        ))
    if pgo and report.has_pgo_profdata_mismatch:
        findings.append(ToolchainMismatchFinding(
            check_id="toolchain_pgo_profdata_skew",
            severity="error",
            message=(
                "PGO profdata version does not match the target LLVM — a rebuild "
                "would not reuse the stored profile."
            ),
            remediation="run `sysforge run toolchain --rebuild-profdata`",
        ))
    return tuple(findings)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_TAG = "LLVM"


def _format_version_pair(installed: str | None, pkgbuild: str | None) -> str:
    """Thin alias over the shared renderer (2.6.1-F9).

    Previously inlined here with a hardcoded ``→``; because every pre-flight
    block is emitted with a bare ``print()`` rather than ``log.ui``, that arrow
    never reached ``downgrade_glyphs`` and survived intact on ``TERM=linux``.
    :func:`render.version_pair` resolves the glyph at format time instead.
    """
    return version_pair(installed, pkgbuild)


def is_actionable_state(s: LlvmPackageState) -> bool:
    """True when an LLVM pre-flight row carries information worth surfacing.

    The pre-flight only guards *source builds*: it exists to warn that a tree
    sysforge is about to build is dirty, diverged, or carries stale PGO
    profdata. A repo-origin package installed from the binary repo with no
    source build_mode has every source-state column empty/``unknown`` — its
    row is pure noise (1.2.0-Q8). A row is actionable when it has an active
    concern (dirty / diverged / profdata-mismatch), was built locally
    (``install_origin == "foreign"``), or sysforge intends to build it from
    source (any non-``pacman`` build_mode). Display-only: the strict
    blocker path (:func:`evaluate_strict`) still sees every state.
    """
    if s.is_dirty or s.divergence == "diverged" or s.pgo_profdata_mismatch:
        return True
    if s.install_origin == "foreign":
        return True
    return bool(s.build_mode) and s.build_mode != "pacman"


def render_preflight(report: LlvmPreflightReport, *, verbose: bool = False) -> str:
    """Render a human-readable pre-flight table.

    Output style matches the action-tag block from
    :func:`sysforge.update._print_summary` (``  [TAG] ...`` with a 17-col
    gutter). Returns the rendered text rather than printing it; callers route
    it through ``log.ui`` (never bare ``print``) so the block reaches the
    unified run-log (2.6.1-F10).

    Non-actionable rows (see :func:`is_actionable_state`) are filtered out
    unless ``verbose`` is set, so repo-origin packages with nothing to report
    don't bury the rows that matter (1.2.0-Q8). When every row is filtered the
    whole block collapses to ``""``.
    """
    shown = (
        report.states if verbose
        else tuple(s for s in report.states if is_actionable_state(s))
    )
    if not shown:
        return ""

    header = tag_header(_TAG)
    lines: list[str] = []
    lines.append(
        f"{header}LLVM source pre-flight ({len(shown)} package"
        f"{'s' if len(shown) != 1 else ''})"
    )

    for s in shown:
        # A split member (llvm-libs from the llvm PKGBUILD) has no source tree of
        # its own — origin/clean/sync/mode all describe a tree it doesn't own — so
        # collapse those dead columns to a "(split of <base>)" note and keep only
        # the real install facts.
        if s.split_of:
            ver = _format_version_pair(s.installed_ver, s.pkgbuild_ver)
            lines.append(
                f"    {s.pkgbase:<24} (split of {s.split_of})"
                f"{' ' * 6}installed={s.install_origin:<14} ver={ver}"
            )
            continue

        clean = "clean" if not s.is_dirty else f"DIRTY ({s.dirty_reason})"
        sync = s.divergence
        if s.head_short and s.upstream_short and sync != "up_to_date":
            sync = f"{sync} ({s.head_short}{arrow()}{s.upstream_short})"
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
