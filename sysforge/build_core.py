# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_core.py — the shared source-build pipeline behind ``build`` and ``update``.

``sysforge build`` and ``sysforge update`` historically carried two separate
dependency-handling + build loops, which drifted: ``build`` left the profile's
``-s``/``-i`` makepkg flags in place (so makepkg ran ``pacman -S`` on deps,
breaking on AUR-only packages) and never pre-installed repo makedeps. This
module is the single entry point both verbs route through, so ``build`` is a
strict subset of ``update``:

    update  → prepare_deps → build loop → install_built   (build_and_install)
              + version check, source-sync scheduling, pacman -Syu, summaries
    build   → prepare_deps → build loop → install_built   (build_and_install)
              + --cleansrc, LLVM preflight

The dependency-prep, per-package makepkg invocation (with ``BATCH_STRIP_FLAGS``
so makepkg never resolves deps itself), and deferred bulk install all live here.
``update`` keeps everything that is genuinely update-specific (the
``--install-only`` artifact scan, toolchain pre-flight, ``pacman -Syu``, and the
result summary) around these calls.

Public API:
    BuildTarget, BuildOutcome
    prepare_deps(pkgbuild_paths, config, ...)
    build_and_install(targets, ...) -> BuildOutcome
    install_built(built_pkg_files, *, always_install=frozenset(), interactive=False)
        -> tuple[list[Path], bool]
    _find_existing_artifacts(...)        # also consumed by update's install_only scan
    _record_build_failure(state_dir, target, exc)
"""
import time
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.timing import PhaseRecord, PhaseTimer
from sysforge.primitives.version import vercmp
from sysforge.primitives.pacman import (
    BATCH_STRIP_FLAGS,
    BATCH_EXTRA_FLAGS,
    snapshot_pkg_dir,
    batch_install_pkgs,
    filter_pkgs_to_installed,
    collect_builddeps,
    filter_missing_deps,
    batch_install_makedeps,
    get_all_installed_packages,
    get_pkgdest,
)
from sysforge.primitives.aur import repo_packages
from sysforge.primitives.already_built import resolve_already_built
# Module-top (not lazy) so tests can monkeypatch
# ``sysforge.build_core.review_target`` to drive gate decisions.
from sysforge.primitives.pkgbuild_review import (
    DECISION_ABORT,
    DECISION_SKIP,
    review_deps,
    review_target,
)
from sysforge.ui import progress as _ui_progress
import contextlib

_log = log.get_logger("BUILD")


# ---------------------------------------------------------------------------
# Lightweight target + outcome structs
# ---------------------------------------------------------------------------

@dataclass
class BuildTarget:
    """A single package to source-build.

    ``update._UpdateResult`` already exposes all of these attributes, so it can
    be passed to :func:`build_and_install` directly (duck-typed); ``BuildVerb``
    constructs ``BuildTarget`` from a parsed PKGBUILD.
    """
    pkgbase: str
    pkgnames: list[str]
    pkgbuild_path: Path
    source: str | None = None
    pkgbuild_ver: str | None = None
    installed_ver: str | None = None


@dataclass
class BuildOutcome:
    built_pkgs: list[str] = field(default_factory=list)
    failed_pkgs: list[str] = field(default_factory=list)
    pgo_skipped_pkgs: list[str] = field(default_factory=list)
    built_pkg_files: list[Path] = field(default_factory=list)
    install_failed: bool = False
    # PKGBUILD review gate results: packages the user dropped at the prompt,
    # and whether the whole run was aborted there (nothing built/installed).
    review_skipped: list[str] = field(default_factory=list)
    aborted: bool = False
    # Wall-clock phase durations (dep prep / per-package builds / install) —
    # aliases the PhaseTimer's records list, so callers without their own
    # timer can still render a report from the outcome.
    phase_records: list[PhaseRecord] = field(default_factory=list)
    # F38: repo + AUR dependency pkgnames installed as a build prerequisite
    # by prepare_deps, surfaced in the end-of-run summary as their own category.
    installed_deps: list[str] = field(default_factory=list)


def target_from_pkgbuild(pkgbuild_path) -> BuildTarget:
    """Build a :class:`BuildTarget` from a PKGBUILD path.

    Derives ``pkgbase``/``pkgnames`` with the same static parse the build
    worker uses (``makepkg_wrapper`` records build state the same way), so a
    split package yields every ``pkgname``. ``source`` is left ``None`` — the
    ``build`` verb has no version-check classification, and ``BuildState.record``
    keeps the prior provenance sticky when a caller passes ``None``.
    """
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    globals_ = parse_pkgbuild(pkgbuild_path).get("globals", {})
    pkgnames = globals_.get("pkgname", [])
    if isinstance(pkgnames, str):
        pkgnames = [pkgnames]
    pkgbase = globals_.get("pkgbase") or (pkgnames[0] if pkgnames else "unknown")
    return BuildTarget(
        pkgbase=pkgbase,
        pkgnames=list(pkgnames),
        pkgbuild_path=Path(pkgbuild_path),
    )


# ---------------------------------------------------------------------------
# Built-artifact discovery + failure recording
# (moved from update.py — both modules and the install_only scan use them)
# ---------------------------------------------------------------------------

def _find_existing_artifacts(
    search_dir: Path,
    pkgnames: list[str],
    pkgbuild_ver: str | None,
    installed_ver: str | None = None,
) -> list[Path]:
    """Locate already-built .pkg.tar artifacts matching pkgnames.

    Two-stage lookup:
      1. Strict glob ``{pkgname}-{pkgbuild_ver}-*.pkg.tar*`` — matches
         non-VCS packages where the static PKGBUILD parse equals the
         filename version exactly.
      2. Fallback ``{pkgname}-*-*-*.pkg.tar*`` + filename parse + vercmp
         to pick the newest. Required for VCS (-git/-svn/...) packages,
         where ``pkgver()`` bumps the version at build time
         (PKGBUILD ``pkgver=0.1.0`` → artifact ``0.1.0.r45.g1234567``)
         so the static ``pkgbuild_ver`` never matches the filename.

    If ``installed_ver`` is provided, the fallback only returns artifacts
    strictly newer than installed — used by ``--install-only`` to avoid
    redundant reinstalls or downgrades.
    """
    from sysforge.primitives.makepkg_wrapper import _parse_built_pkg_filename

    if not search_dir or not Path(search_dir).is_dir():
        return []

    found: list[Path] = []
    for pkgname in pkgnames:
        if pkgbuild_ver:
            strict = [
                p for p in Path(search_dir).glob(
                    f"{pkgname}-{pkgbuild_ver}-*.pkg.tar*"
                )
                if not p.name.endswith(".sig")
            ]
            if strict:
                found.extend(strict)
                continue

        candidates: list[tuple[str, Path]] = []
        for p in Path(search_dir).glob(f"{pkgname}-*-*-*.pkg.tar*"):
            if p.name.endswith(".sig"):
                continue
            parsed = _parse_built_pkg_filename(pkgname, p.name)
            if parsed is None:
                continue
            epoch, ver, rel = parsed
            ver_string = f"{epoch}:{ver}-{rel}" if epoch != "0" else f"{ver}-{rel}"
            if installed_ver is not None:
                try:
                    if vercmp(ver_string, installed_ver) <= 0:
                        continue
                except RuntimeError:
                    continue
            candidates.append((ver_string, p))

        if not candidates:
            continue

        with contextlib.suppress(RuntimeError):
            candidates.sort(key=cmp_to_key(lambda a, b: vercmp(a[0], b[0])))
        found.append(candidates[-1][1])

    return found


def _record_build_failure(state_dir, target, exc) -> None:
    """Persist a build failure to build_state.toml's ``[failures]`` table.

    Opens a fresh BuildState so loop-time success writes (recorded by the
    build worker on disk) aren't clobbered. Pulls the diagnosis signature/fix
    from the exception's ``.diagnosis`` (attached by makepkg_wrapper) when
    present. Best-effort: recording must never turn a build failure into a
    crash. The entry is auto-cleared on the next successful build of the same
    pkgbase (see BuildState.record).
    """
    try:
        diagnosis = getattr(exc, "diagnosis", None) or []
        first = diagnosis[0] if diagnosis else None
        bs_fail = BuildState(state_dir)
        bs_fail.record_failure(
            target.pkgbase,
            error=str(exc),
            pkgver=getattr(target, "pkgbuild_ver", None),
            signature=getattr(first, "signature", None),
            fix_cmd=getattr(first, "fix_cmd", None),
        )
        bs_fail.save()
    except Exception as e:  # pragma: no cover - defensive
        _log.warn(f"Failed to record build failure for {target.pkgbase!r}: {e}")


# ---------------------------------------------------------------------------
# Dependency preparation
# ---------------------------------------------------------------------------

def prepare_deps(
    pkgbuild_paths: list[Path],
    config: dict,
    *,
    building_names: "frozenset[str] | set[str]" = frozenset(),
    profile_conf: str | None = None,
    cc: str | None = None,
    cxx: str | None = None,
    ld: str | None = None,
    state_dir: Path | None = None,
    review: str = "off",
    interactive: bool = False,
    installed_deps_out: list[str] | None = None,
) -> bool:
    """Pre-install missing repo makedeps, then resolve + build AUR/local deps.

    Returns ``True`` to proceed, ``False`` when the user aborted the run at the
    dependency review gate (the caller treats this like a target-gate abort:
    clean return, nothing built or installed by the build loop).

    This is what frees the per-package makepkg invocation from having to sync
    deps itself (``-s`` is stripped below): every repo makedep is installed in
    one ``pacman -S`` transaction, and every AUR/local dependency is built and
    installed via :func:`build_resolved_deps`. ``building_names`` are the
    pkgbases we are about to build ourselves — excluded so we never try to
    resolve a target as its own dependency.

    The repo arm installs **only** packages that exist in a sync repo. AUR-only
    makedeps are filtered out here (``repo_packages``) and left to the AUR arm:
    mixing an AUR name into the ``pacman -S`` transaction makes pacman abort the
    whole transaction with "target not found", installing none of the repo
    makedeps either (the proton-cachyos exit-8 regression).

    Both arms are best-effort: a failure warns and lets the build proceed (a
    genuinely missing dep surfaces as a per-package build failure with a
    diagnosis, rather than aborting the whole batch up front).
    """
    if not pkgbuild_paths:
        return True

    # Repo build deps — one sudo transaction. This collects depends +
    # makedepends + checkdepends, not just makedepends: the per-package makepkg
    # call below runs with -s stripped, and makepkg checks runtime ``depends``
    # before building too, so a missing repo runtime dep would abort the build
    # (exit 8). Restrict to sync-repo packages so AUR deps don't poison the
    # pacman -S transaction (they're built by the AUR arm below).
    build_deps = collect_builddeps(pkgbuild_paths)
    missing_deps = filter_missing_deps(build_deps)
    repo_missing = sorted(repo_packages(missing_deps)) if missing_deps else []
    if repo_missing:
        try:
            batch_install_makedeps(repo_missing)
            if installed_deps_out is not None:
                installed_deps_out.extend(repo_missing)
        except RuntimeError as e:
            _log.error(str(e))
            _log.warn("makedep pre-install failed — some builds may fail")

    # AUR/local deps — resolve transitively, build in topo order.
    from sysforge.primitives.aur_resolve import (
        resolve_aur_deps_batch,
        build_resolved_deps,
    )
    try:
        aur_deps = resolve_aur_deps_batch(pkgbuild_paths, config, fetch=True)
        aur_deps = [d for d in aur_deps if d.name not in building_names]
        # Dependency review gate — batched, all-or-nothing (no per-dep skip:
        # dropping a dep breaks its dependent). Mirrors the target gate's
        # modes; reviewed_commit stamps already exist for dep builds because
        # makepkg_wrapper records them unconditionally.
        buildable = [
            d for d in aur_deps
            if d.source == "aur" and getattr(d, "pkgbuild_path", None) is not None
        ] if review in ("prompt", "auto") else []
        if buildable:
            if state_dir is None:
                from sysforge.pipeline.state import resolve_state_dir
                _dep_state_dir, _ = resolve_state_dir(None)
            else:
                _dep_state_dir = state_dir
            bs_deps = BuildState(_dep_state_dir)
            if review == "prompt":
                # Hand the bottom row back to the terminal before the
                # single-keypress prompt (mirrors the target gate).
                _ui_progress.clear()
            def _review_row(d):
                # buildable was filtered to pkgbuild_path is not None above.
                assert d.pkgbuild_path is not None  # noqa: S101 — buildable filter above guarantees this, not input validation
                return (
                    d.name,
                    d.pkgbuild_path.parent,
                    (bs_deps.get(d.name) or {}).get("reviewed_commit"),
                )

            decision = review_deps(
                [_review_row(d) for d in buildable],
                interactive=(review == "prompt"),
            )
            if decision == DECISION_ABORT:
                return False
        if aur_deps:
            built_dep_names = build_resolved_deps(
                aur_deps,
                profile_conf=profile_conf,
                cc_override=cc,
                cxx_override=cxx,
                ld_override=ld,
                state_dir=state_dir,
                interactive=interactive,
            )
            if installed_deps_out is not None:
                installed_deps_out.extend(built_dep_names)
    except RuntimeError as e:
        _log.error(f"AUR dep resolution failed: {e}")
        _log.warn("AUR dep resolution failed — some builds may fail")
    return True


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_built(
    built_pkg_files: list[Path],
    *,
    always_install: "frozenset[str] | set[str]" = frozenset(),
    interactive: bool = False,
) -> tuple[list[Path], bool]:
    """Dedupe, filter to the install keep-set, and bulk ``pacman -U``.

    Returns ``(deduped_files, install_failed)``. The currently-installed set is
    re-fetched here because makedep + AUR-dep pre-install may have expanded it
    since the caller last looked. Split-pkgbase safety: makepkg emits one
    .pkg.tar per pkgname in the PKGBUILD, but we only install the sub-packages
    the user already has on the system **plus** ``always_install`` — the
    pkgnames the user explicitly asked to build. Without the latter a fresh
    ``sysforge build <new-pkg>`` would build the artifact and then drop it
    (its pkgname isn't installed yet), so the package never gets installed.
    """
    seen: set = set()
    deduped: list[Path] = []
    for p in built_pkg_files:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    built_pkg_files = deduped

    install_failed = False
    if built_pkg_files:
        keep_names = set(get_all_installed_packages().keys()) | set(always_install)
        built_pkg_files, dropped = filter_pkgs_to_installed(
            built_pkg_files, keep_names
        )
        if dropped:
            _log.info(
                f"Skipping install of {len(dropped)} split sub-package(s) "
                "not currently on the system:"
            )
            for path, pn in dropped:
                _log.info(f"  - {pn} ({path.name})")

    if built_pkg_files and not batch_install_pkgs(built_pkg_files, interactive=interactive):
        _log.error("Batch package install failed")
        _log.error("packages were built but not installed")
        install_failed = True

    return built_pkg_files, install_failed


# ---------------------------------------------------------------------------
# Shared BuildOptions factory for pipeline stages
# ---------------------------------------------------------------------------

# Constant BuildOptions fields per install-bearing pipeline stage. A stage
# declares only the fields that are *always* the same for it; per-call values
# (cc_override, update, source, toolchain_variant, extra_flags, …) are passed
# as overrides to make_build_options(). Kernel's ``no_install=True`` is the
# build/install split that lets Gate 2 audit the resolved config pre-install;
# toolchain's ``pgo_managed=True`` marks its makepkg runs as PGO-orchestrated.
_STAGE_BUILD_DEFAULTS: dict[str, dict] = {
    "kernel": {"owner_stage": "kernel", "no_install": True},
    "toolchain": {"pgo_managed": True},
    "packages": {},
}


def make_build_options(stage: str, options, **overrides):
    """Assemble a ``BuildOptions`` for a pipeline stage's makepkg invocation.

    Maps the fields common to every stage's run-options object — ``no_pkg_logs``
    → ``pkg_log``, plus ``persist_log`` / ``state_dir`` / ``abi_check`` — then
    layers in the stage's constant defaults from ``_STAGE_BUILD_DEFAULTS`` and
    finally the caller's per-call ``overrides`` (which win over both). Fields
    that differ per stage or per package — ``profile_conf``, ``log_dir``,
    ``update``, ``cc_override`` / ``cxx_override`` / ``ld_override``, ``source``,
    ``toolchain_variant``, ``extra_flags``, … — are passed explicitly as
    overrides by the caller; anything a stage omits keeps its ``BuildOptions``
    default.

    This centralizes the three install-bearing stages' (``kernel`` /
    ``toolchain`` / ``packages``) hand-assembly so a stage-wide default lives in
    exactly one place. ``abi_check`` is read via ``getattr`` so a run-options
    object without the attribute degrades to ``False`` rather than raising.
    """
    from sysforge.primitives.makepkg_wrapper import BuildOptions

    if stage not in _STAGE_BUILD_DEFAULTS:
        raise ValueError(f"unknown build stage {stage!r}")
    fields: dict = {
        "pkg_log": not options.no_pkg_logs,
        "persist_log": options.persist_log,
        "state_dir": options.state_dir,
        "abi_check": getattr(options, "abi_check", False),
    }
    fields.update(_STAGE_BUILD_DEFAULTS[stage])
    fields.update(overrides)
    return BuildOptions(**fields)


# ---------------------------------------------------------------------------
# Build + install
# ---------------------------------------------------------------------------

def _order_targets_by_intra_deps(targets) -> tuple[list, dict[str, set[str]]]:
    """Topologically order batch targets so a member that build-depends on
    another member builds after it.

    The recursive AUR resolver only orders *missing* deps — a batch sibling
    that is already installed (at a stale version) never creates an edge, so
    an alphabetical batch can configure against the old installed version of
    a sibling whose new version sits unbuilt later in the batch (the Vulkan
    1.4.354 headers/loader failure). Edges are derived from each target's
    ``depends`` + ``makedepends`` + ``checkdepends`` matched against every
    other target's ``pkgname``s and ``provides`` (version constraints
    stripped; soname provides like ``libvulkan.so`` participate — the parse
    is purely intra-batch, nothing external is queried).

    Returns ``(ordered_targets, intra_deps)`` where ``intra_deps`` maps a
    pkgbase to the batch pkgbases it depends on — the build loop uses it to
    install a freshly built dep before its dependent configures. On a
    dependency cycle the original order is kept (warned), matching the
    pre-ordering behaviour.
    """
    from graphlib import CycleError, TopologicalSorter

    from sysforge.primitives.aur_resolve import _looks_unresolved, _strip_version
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

    if len(targets) < 2:
        return list(targets), {}

    by_pkgbase = {t.pkgbase: t for t in targets}
    provider_of: dict[str, str] = {}
    metas: dict[str, dict] = {}
    for t in targets:
        try:
            globals_ = parse_pkgbuild(t.pkgbuild_path).get("globals", {})
        except Exception as e:
            _log.debug(f"intra-batch dep parse failed for {t.pkgbase}: {e}")
            globals_ = {}
        metas[t.pkgbase] = globals_
        for pn in (getattr(t, "pkgnames", None) or []):
            provider_of.setdefault(pn, t.pkgbase)
        for prov in globals_.get("provides", []) or []:
            if _looks_unresolved(prov):
                continue
            provider_of.setdefault(_strip_version(prov), t.pkgbase)

    intra_deps: dict[str, set[str]] = {}
    for t in targets:
        dep_bases: set[str] = set()
        for key in ("depends", "makedepends", "checkdepends"):
            for spec in metas[t.pkgbase].get(key, []) or []:
                if _looks_unresolved(spec):
                    continue
                dep_base = provider_of.get(_strip_version(spec))
                if dep_base is not None and dep_base != t.pkgbase:
                    dep_bases.add(dep_base)
        if dep_bases:
            intra_deps[t.pkgbase] = dep_bases

    if not intra_deps:
        return list(targets), {}

    sorter = TopologicalSorter()
    for t in targets:  # insertion order keeps unrelated members stable
        sorter.add(t.pkgbase, *sorted(intra_deps.get(t.pkgbase, ())))
    try:
        ordered_names = list(sorter.static_order())
    except CycleError as e:
        _log.warn(
            f"intra-batch dependency cycle ({' -> '.join(e.args[1])}) — "
            "keeping original build order"
        )
        return list(targets), intra_deps

    ordered = [by_pkgbase[n] for n in ordered_names if n in by_pkgbase]
    if [t.pkgbase for t in ordered] != [t.pkgbase for t in targets]:
        _log.info(
            "Reordered batch for intra-batch deps: "
            + " ".join(t.pkgbase for t in ordered)
        )
    return ordered, intra_deps


def resolve_cleanbuild_flags(
    *, no_cleanbuild: bool, extra_flags: list | None, pgo_mode: str | None
) -> tuple[list, frozenset]:
    """Compute the makepkg ``(batch_flags, strip_flags)`` for the build loop.

    The single home for cleanbuild policy across ``build`` and ``update``:

    - Normally ``-C`` (cleanbuild: wipe ``$srcdir`` before building) is on so a
      stale ``$srcdir`` from a prior failed run can't cause
      patch-already-applied errors. ``no_cleanbuild`` opts out, and we also add
      ``-C``/``--cleanbuild`` to ``strip_flags`` so a user-passed flag can't
      re-add it.
    - PGO (``--pgo=record``/``--pgo=use``) forces a full clean build (``-C -c``)
      regardless of ``no_cleanbuild`` (F24): an instrumentation- or
      profile-use pass must never reuse stale object files left by a
      *differently*-instrumented prior run, so the cleanbuild opt-out cannot
      apply and ``-C``/``-c`` are never stripped.
    """
    if pgo_mode:
        # -C wipes $srcdir before the build (no cross-pass object reuse); -c
        # cleans work dirs after. Forced on, never stripped.
        return ["-C", "-c"] + (extra_flags or []), BATCH_STRIP_FLAGS
    cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
    batch_flags = cleanbuild_flags + (extra_flags or [])
    strip_flags = (
        BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild
        else BATCH_STRIP_FLAGS
    )
    return batch_flags, strip_flags


def build_and_install(
    targets,
    *,
    config: dict,
    sync_source: bool,
    interactive: bool = False,
    no_cleanbuild: bool = False,
    profile_conf: str | None = None,
    cc: str | None = None,
    cxx: str | None = None,
    ld: str | None = None,
    state_dir: Path | None = None,
    pkg_log: bool = True,
    persist_log: bool = False,
    log_dir: Path | None = None,
    cache_report: bool = False,
    abi_check: bool = False,
    extra_flags: list | None = None,
    active_variant: str | None = None,
    toolchain_fingerprint: str | None = None,
    pkgdest: Path | None = None,
    review: str = "prompt",
    pgo_mode: str | None = None,
    timer: PhaseTimer | None = None,
) -> BuildOutcome:
    """Resolve deps, build every target, then bulk-install — the shared core.

    Each target's makepkg call runs with ``strip_flags=BATCH_STRIP_FLAGS``
    (drops ``-s``/``--syncdeps``/``-i``/``--install``) and ``force_batch`` when
    non-interactive, so makepkg never touches pacman — deps were already
    handled by :func:`prepare_deps`, and the built artifacts are installed in
    one transaction at the end.

    ``sync_source`` is the one deliberate caller difference: ``update`` passes
    ``False`` (Phase 2 already synced sources via the scheduler); ``build``
    passes ``not --no-update`` to keep its inline per-package source sync, which
    already routes through ``source_sync.get_scheduler()`` inside ``run()``.

    ``review`` selects the PKGBUILD review gate mode (one home, both verbs):
    ``"prompt"`` presents each target whose clone HEAD differs from its
    recorded ``reviewed_commit`` for review *before* dep prep and the build
    loop, so a skip never installs that package's makedeps and an abort
    leaves nothing built or installed; ``"auto"`` runs the same comparison
    but auto-accepts changes with a logged notice (the ``update`` default);
    ``"off"`` skips the gate entirely (``--no-review`` /
    ``[build] review = false``).

    ``pgo_mode`` (``"record"``/``"use"``/``None``) drives mesa instrumentation
    PGO (`build --pgo`). Threaded into every target's ``BuildOptions`` but a
    no-op for non-mesa pkgbases (the wrapper gates on ``is_mesa_pkgbase``), so a
    mixed batch only profiles the mesa target. ``use`` earns the ``-sysforge``
    rename (``build_mode = "pgo_mesa"``).
    """
    outcome = BuildOutcome()
    if timer is None:
        timer = PhaseTimer()
    # Alias (not copy) so records appended after any return still surface.
    outcome.phase_records = timer.records
    if not targets:
        return outcome

    # Imported here (not at module top) so tests patching
    # ``sysforge.primitives.makepkg_wrapper.run`` are observed.
    from sysforge.primitives.makepkg_wrapper import (
        BuildOptions,
        run as build_run,
        PGOBuildSkipped,
        AlreadyBuilt,
    )
    from sysforge.primitives.cache_probe import reset_session, emit_session_report
    reset_session()

    # ── PKGBUILD review gate ──────────────────────────────────────────────
    # For the `build` path (sync_source=True) the wrapper's inline sync hasn't
    # run yet, so pre-sync each target through the scheduler — the wrapper's
    # later request dedups against the scheduler cache, and the gate sees the
    # post-fetch HEAD instead of reviewing a stale clone. Best-effort: real
    # sync failures still surface through the wrapper's own request.
    review_active = review in ("prompt", "auto")
    if review_active and sync_source:
        from sysforge.primitives.source_sync import SyncRequest, get_scheduler
        _ui_progress.phase("syncing sources")
        scheduler = get_scheduler()
        for t in targets:
            try:
                scheduler.request(SyncRequest(
                    pkgbase=t.pkgbase,
                    pkgbuild_dir=Path(t.pkgbuild_path).parent,
                    source=getattr(t, "source", None) or "aur",
                ))
            except Exception as e:
                _log.debug(f"review pre-sync {t.pkgbase}: {e}")
    bs_review: BuildState | None = None
    if review_active:
        if state_dir is None:
            from sysforge.pipeline.state import resolve_state_dir
            _review_state_dir, _ = resolve_state_dir(None)
        else:
            _review_state_dir = state_dir
        bs_review = BuildState(_review_state_dir)
        if review == "prompt":
            # The prompt reads single keypresses — hand the bottom row back
            # to the terminal first (mirrors makepkg_wrapper's prompts).
            _ui_progress.clear()
        kept = []
        for t in targets:
            entry = None
            for pn in (getattr(t, "pkgnames", None) or []):
                entry = bs_review.get(pn)
                if entry is not None:
                    break
            decision = review_target(
                t.pkgbase,
                Path(t.pkgbuild_path).parent,
                (entry or {}).get("reviewed_commit"),
                interactive=(review == "prompt"),
            )
            if decision == DECISION_ABORT:
                _log.ui(
                    "[SYSFORGE] Aborted at PKGBUILD review — "
                    "nothing was built or installed."
                )
                outcome.aborted = True
                return outcome
            if decision == DECISION_SKIP:
                _log.ui(f"[SYSFORGE] {t.pkgbase}: skipped at PKGBUILD review")
                outcome.review_skipped.append(t.pkgbase)
                continue
            kept.append(t)  # accept | clean | no_git
        targets = kept
        if not targets:
            return outcome

    # Pre-build: optional snapshot + learned time estimate (1.2.0-F21).
    from sysforge.primitives import snapshot as _snapshot
    from sysforge.primitives import build_estimate as _estimate
    _snapshot.ensure_pre_build_snapshot(config, interactive=interactive)
    _target_names = [pn for t in targets for pn in (getattr(t, "pkgnames", None) or [])]
    if review_active:
        _bs_est = bs_review  # reuse the review-gate reader
    else:
        from sysforge.pipeline.state import resolve_state_dir
        _est_state_dir = state_dir or resolve_state_dir(None)[0]
        _bs_est = BuildState(_est_state_dir)
    _est_line = _estimate.format_estimate(_target_names, _bs_est)
    _est_seconds, _est_known, _ = _estimate.estimate_seconds(_target_names, _bs_est)
    if _est_line:
        _log.ui(f"[SYSFORGE] {_est_line}")
    _actual_start = time.monotonic()

    # Artifacts land in PKGDEST when the system makepkg.conf sets one — the
    # snapshot/AlreadyBuilt scans must look there, not in the PKGBUILD dir.
    # Resolved here (not per caller) so `build` and `update` cannot drift:
    # `update` passes its own resolved value; a caller passing None gets the
    # same system-conf lookup.
    if pkgdest is None:
        pkgdest = get_pkgdest()

    # ── Intra-batch dependency ordering ───────────────────────────────────
    # A batch member whose new version is a build dep of another member must
    # build *and install* first — the dependent would otherwise configure
    # against the stale installed version (prepare_deps only handles missing
    # deps; installed siblings never create an edge there).
    targets, intra_deps = _order_targets_by_intra_deps(targets)

    # The pkgnames the user explicitly asked to build are always installed,
    # even if not currently on the system (a fresh build). For ``update`` these
    # are already-installed packages, so the union is a no-op there.
    requested = {
        pn for t in targets for pn in (getattr(t, "pkgnames", None) or [])
    }

    pkgbuild_paths = [t.pkgbuild_path for t in targets if t.pkgbuild_path]
    building_names = {t.pkgbase for t in targets}
    _ui_progress.phase("resolving dependencies")
    with timer.phase("dep prep"):
        proceed = prepare_deps(
            pkgbuild_paths,
            config,
            building_names=building_names,
            profile_conf=profile_conf,
            cc=cc,
            cxx=cxx,
            ld=ld,
            state_dir=state_dir,
            review=review,
            interactive=interactive,
            installed_deps_out=outcome.installed_deps,
        )
    if not proceed:
        _log.ui(
            "[SYSFORGE] Aborted at dependency PKGBUILD review — "
            "nothing was built or installed."
        )
        outcome.aborted = True
        return outcome

    # Cleanbuild policy (one home): -C by default, opt-out via no_cleanbuild,
    # and a forced -C -c under --pgo so a PGO pass never reuses stale objects.
    batch_flags, strip_flags = resolve_cleanbuild_flags(
        no_cleanbuild=no_cleanbuild, extra_flags=extra_flags, pgo_mode=pgo_mode)

    built_files_by_pkgbase: dict[str, list[Path]] = {}
    # Files handed to the just-in-time install (skipped by the final bulk
    # install) vs. the subset it actually installed (filter-survivors, kept
    # for outcome reporting).
    jit_handled: set[Path] = set()
    jit_files: list[Path] = []

    with _ui_progress.tracker(len(targets), "building") as _tick:
        for target in targets:
            _tick(target.pkgbase)
            # ── Just-in-time install of intra-batch deps ──────────────────
            # Deferred bulk install would leave this target configuring
            # against the stale installed version of a sibling we just
            # rebuilt — install the sibling's artifacts now. A failed sibling
            # only warns: the dependent may still build against the
            # installed version, and its own failure gets recorded normally.
            dep_bases = intra_deps.get(target.pkgbase, set())
            if dep_bases:
                failed_deps = dep_bases & set(outcome.failed_pkgs)
                if failed_deps:
                    _log.warn(
                        f"{target.pkgbase}: intra-batch dep(s) "
                        f"{', '.join(sorted(failed_deps))} failed to build — "
                        "building against the installed version"
                    )
                pending_bases = [
                    dep for dep in sorted(dep_bases)
                    if any(
                        f not in jit_handled
                        for f in built_files_by_pkgbase.get(dep, [])
                    )
                ]
                pending = [
                    f for dep in pending_bases
                    for f in built_files_by_pkgbase.get(dep, [])
                    if f not in jit_handled
                ]
                if pending:
                    _log.info(
                        f"{target.pkgbase}: installing intra-batch dep(s) "
                        f"before building: {', '.join(pending_bases)}"
                    )
                    # Overlay the JIT install onto the build counter without
                    # advancing it, then hand the line back to "building".
                    _tick.note(
                        f"installing {len(pending_bases)} intra-batch dep(s) "
                        f"for {target.pkgbase}"
                    )
                    with timer.phase(f"install deps: {target.pkgbase}"):
                        installed_files, jit_failed = install_built(
                            pending, always_install=requested,
                            interactive=interactive,
                        )
                    _tick.resume()
                    jit_handled.update(pending)
                    jit_files.extend(installed_files)
                    if jit_failed:
                        outcome.install_failed = True
            search_dir = pkgdest if pkgdest else target.pkgbuild_path.parent
            build_start = time.time()
            with timer.phase(f"build: {target.pkgbase}"):
                try:
                    build_run(target.pkgbuild_path, options=BuildOptions(
                        pkg_log=pkg_log,
                        persist_log=persist_log,
                        log_dir=log_dir,
                        profile_conf=profile_conf,
                        cc_override=cc,
                        cxx_override=cxx,
                        ld_override=ld,
                        cache_report=False,
                        abi_check=abi_check,
                        init_session=(
                            not outcome.built_pkgs and not outcome.failed_pkgs
                        ),
                        update=sync_source,
                        state_dir=state_dir,
                        extra_flags=batch_flags,
                        strip_flags=strip_flags,
                        interactive=interactive,
                        force_batch=not interactive,
                        source=target.source,
                        toolchain_variant=active_variant,
                        toolchain_fingerprint=toolchain_fingerprint,
                        pgo_mode=pgo_mode,
                    ))
                    new_pkgs = sorted(
                        p for p in snapshot_pkg_dir(search_dir)
                        if p.stat().st_mtime >= build_start
                    )
                    outcome.built_pkg_files.extend(new_pkgs)
                    built_files_by_pkgbase[target.pkgbase] = new_pkgs
                    outcome.built_pkgs.append(target.pkgbase)
                except PGOBuildSkipped as e:
                    _log.warn(str(e))
                    outcome.pgo_skipped_pkgs.append(target.pkgbase)
                except AlreadyBuilt:
                    # 2.5.1-F2: policy-routed. "reuse" is trivially REUSE today;
                    # the seam call is the one home any future policy lands in.
                    resolve_already_built("reuse", interactive=interactive)
                    existing = _find_existing_artifacts(
                        search_dir, target.pkgnames, target.pkgbuild_ver,
                    )
                    if existing:
                        _log.info(
                            f"{target.pkgbase}: package already built — "
                            "installing existing artifact"
                        )
                        outcome.built_pkg_files.extend(existing)
                        built_files_by_pkgbase[target.pkgbase] = list(existing)
                        outcome.built_pkgs.append(target.pkgbase)
                    else:
                        msg = (
                            f"makepkg reported already built but no matching "
                            f".pkg.tar found in {search_dir}"
                        )
                        _log.error(f"{target.pkgbase}: {msg}")
                        outcome.failed_pkgs.append(target.pkgbase)
                        _record_build_failure(state_dir, target, msg)
                except (RuntimeError, SystemExit) as e:
                    _log.error(f"Build failed for {target.pkgbase!r}: {e}")
                    outcome.failed_pkgs.append(target.pkgbase)
                    _record_build_failure(state_dir, target, e)

    # Post-build feedback: estimated vs actual (only when history existed).
    if _est_known > 0:
        _actual = int(time.monotonic() - _actual_start)
        _log.ui(f"[SYSFORGE] {_estimate.format_estimate_vs_actual(_est_seconds, _actual)}")

    # Final bulk install: everything built this run except files the
    # just-in-time path already installed (re-running pacman -U on those
    # would only re-trigger hooks). ``install_failed`` accumulates across
    # both paths.
    remaining = [f for f in outcome.built_pkg_files if f not in jit_handled]
    if remaining:
        _ui_progress.phase("installing built packages")
    with timer.phase("install"):
        installed_now, final_failed = install_built(
            remaining, always_install=requested, interactive=interactive,
        )
    outcome.built_pkg_files = jit_files + installed_now
    outcome.install_failed = outcome.install_failed or final_failed
    _ui_progress.phase(None)

    if cache_report:
        emit_session_report()

    return outcome
