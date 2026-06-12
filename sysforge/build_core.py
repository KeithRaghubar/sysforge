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
    install_built(built_pkg_files, *, always_install=frozenset()) -> tuple[list[Path], bool]
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
)
from sysforge.primitives.aur import repo_packages
# Module-top (not lazy) so tests can monkeypatch
# ``sysforge.build_core.review_target`` to drive gate decisions.
from sysforge.primitives.pkgbuild_review import (
    DECISION_ABORT,
    DECISION_SKIP,
    review_target,
)
from sysforge.ui import progress as _ui_progress

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

        try:
            candidates.sort(key=cmp_to_key(lambda a, b: vercmp(a[0], b[0])))
        except RuntimeError:
            pass
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
) -> None:
    """Pre-install missing repo makedeps, then resolve + build AUR/local deps.

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
        return

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
        except RuntimeError as e:
            _log.error(str(e))
            _log.ui(
                "[SYSFORGE] Warning: makedep pre-install failed — "
                "some builds may fail"
            )

    # AUR/local deps — resolve transitively, build in topo order.
    from sysforge.primitives.aur_resolve import (
        resolve_aur_deps_batch,
        build_resolved_deps,
    )
    try:
        aur_deps = resolve_aur_deps_batch(pkgbuild_paths, config, fetch=True)
        aur_deps = [d for d in aur_deps if d.name not in building_names]
        if aur_deps:
            build_resolved_deps(
                aur_deps,
                profile_conf=profile_conf,
                cc_override=cc,
                cxx_override=cxx,
                ld_override=ld,
                state_dir=state_dir,
            )
    except RuntimeError as e:
        _log.error(f"AUR dep resolution failed: {e}")
        _log.ui(
            "[SYSFORGE] Warning: AUR dep resolution failed — "
            "some builds may fail"
        )


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_built(
    built_pkg_files: list[Path],
    *,
    always_install: "frozenset[str] | set[str]" = frozenset(),
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

    if built_pkg_files:
        if not batch_install_pkgs(built_pkg_files):
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
    pkgdest: Path | None = None,
    review: str = "prompt",
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

    pkgbuild_paths = [t.pkgbuild_path for t in targets if t.pkgbuild_path]
    building_names = {t.pkgbase for t in targets}
    _ui_progress.phase("resolving dependencies")
    with timer.phase("dep prep"):
        prepare_deps(
            pkgbuild_paths,
            config,
            building_names=building_names,
            profile_conf=profile_conf,
            cc=cc,
            cxx=cxx,
            ld=ld,
            state_dir=state_dir,
        )

    # Cleanbuild (-C) prevents a stale $srcdir from a prior failed run causing
    # patch-already-applied errors in prepare(). When the caller opts out we
    # also strip -C/--cleanbuild so a user-passed flag can't re-add it.
    cleanbuild_flags = [] if no_cleanbuild else BATCH_EXTRA_FLAGS
    batch_flags = cleanbuild_flags + (extra_flags or [])
    strip_flags = (
        BATCH_STRIP_FLAGS | {"--cleanbuild", "-C"} if no_cleanbuild
        else BATCH_STRIP_FLAGS
    )

    with _ui_progress.tracker(len(targets), "building") as _tick:
        for target in targets:
            _tick(target.pkgbase)
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
                    ))
                    new_pkgs = sorted(
                        p for p in snapshot_pkg_dir(search_dir)
                        if p.stat().st_mtime >= build_start
                    )
                    outcome.built_pkg_files.extend(new_pkgs)
                    outcome.built_pkgs.append(target.pkgbase)
                except PGOBuildSkipped as e:
                    _log.warn(str(e))
                    outcome.pgo_skipped_pkgs.append(target.pkgbase)
                except AlreadyBuilt:
                    existing = _find_existing_artifacts(
                        search_dir, target.pkgnames, target.pkgbuild_ver,
                    )
                    if existing:
                        _log.info(
                            f"{target.pkgbase}: package already built — "
                            "installing existing artifact"
                        )
                        outcome.built_pkg_files.extend(existing)
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

    # The pkgnames the user explicitly asked to build are always installed,
    # even if not currently on the system (a fresh build). For ``update`` these
    # are already-installed packages, so the union is a no-op there.
    requested = {
        pn for t in targets for pn in (getattr(t, "pkgnames", None) or [])
    }
    if outcome.built_pkg_files:
        _ui_progress.phase("installing built packages")
    with timer.phase("install"):
        outcome.built_pkg_files, outcome.install_failed = install_built(
            outcome.built_pkg_files, always_install=requested
        )
    _ui_progress.phase(None)

    if cache_report:
        emit_session_report()

    return outcome
