# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update.py — check for and rebuild outdated sysforge-managed packages

Iteration scope is the live install set: every installed AUR package
(`pacman -Qm`) plus repo packages selected by an override in packages.toml.
packages.toml entries apply as overrides where present (see
DESIGN.md §Package Manifest); installed packages with no entry use defaults.

Compares installed package versions against the latest PKGBUILD versions in
pkgbuild_src_dir (after source sync), then rebuilds packages where the
PKGBUILD is newer than what is installed.

VCS packages (-git, -svn, -hg, -bzr) carry a static seed pkgver that does
not reflect upstream HEAD; their real version is computed by pkgver() at
build time. Without --devel they are flagged DEVEL and skipped. With
--devel each VCS pkgbase has its pkgver() resolved up-front (a one-shot
makepkg --nobuild pass) and vercmp'd against the installed version, so
only packages whose upstream actually advanced are rebuilt.

Phases:
    0. Init — load state, config, packages.toml overrides, open unified log
    1. Package set assembly — walk pacman -Qm + override-tagged repo
    2. Source sync — batched RPC + shallow fetch via SourceSyncScheduler
    3. Version check — parallel PKGBUILD parsing + vercmp
    4. Summary + dry-run gate
    5+6. Build + install — delegated to build_core.build_and_install (the
         shared engine behind `build` and `update`: makedep pre-install, AUR
         dep build, makepkg with -s/-i stripped, deferred bulk install). The
         --install-only artifact-scan path stays here and reuses
         build_core.install_built.

Public API:
    cmd_update(args)
"""
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sysforge import log
_log = log.get_logger("UPDATE")
from sysforge.ui import progress as _ui_progress  # noqa: E402
from sysforge.primitives.build_state import (
    BuildState,
    group_by_pkgbase,
    BUILD_MODE_SOURCE,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.aur import fetch_aur_name_cache
from sysforge.primitives.source_sync import (
    STATUS_PURGE_REFUSED, get_scheduler,
)
from sysforge.primitives.config import (
    expand_package_groups, load_config, load_conflict_groups,
    load_consumes_inference, load_sysforge_toml, resolve_flag_default,
)
from sysforge.primitives.llvm_state import (
    collect_llvm_state,
    render_preflight as render_llvm_preflight,
)
from sysforge.primitives.profile import (
    match_rules, resolve_profile, resolve_consumes,
)
from sysforge.primitives.flag_drift import (
    STATUS_PARSE_ERROR,
    resolve_flag_drift,
)
from sysforge.primitives.toolchain_preflight import (
    collect_required_toolchains,
    run_preflight as run_toolchain_preflight,
    render_preflight as render_toolchain_preflight,
    auto_remediate as auto_remediate_toolchain,
)
from sysforge.primitives.paths import resolve_packages_path
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags
from sysforge.primitives.timing import PhaseTimer, render_report
from sysforge.primitives.pacman import (
    get_pkgdest,
    get_all_installed_packages,
    checkupdates_map,
)
from sysforge import build_core
from sysforge.build_core import _find_existing_artifacts
from sysforge.pipeline.state import (
    PipelineState,
    get_toolchain_fingerprint,
    get_toolchain_variant,
    resolve_state_dir,
)
from sysforge.packages_cmd import entry_is_inert
from sysforge.update_result import _UpdateResult
from sysforge.update_summary import _print_summary, _print_result_summary, ResultSummary
from sysforge.update_version import _check_one_pkgbase
from sysforge.update_assemble import _assemble_package_set
from sysforge.update_sync import _sync_sources


# Escape sequences to restore the terminal after an interrupted child that
# entered alt-screen / hid the cursor / left SGR state set (e.g. a pager
# killed by Ctrl-C). Written verbatim to stdout from the cmd_update finally
# block when stdout is a TTY.
_TERMINAL_RESET = "\x1b[?1049l\x1b[?25h\x1b[0m"


def _resolve_drift_axes(args, update_cfg=None) -> tuple[bool, bool, bool]:
    """Resolve (all, toolchain, flag) rebuild-on-drift from CLI + [update].

    ``--rebuild-on-drift`` (or ``[update] rebuild_on_drift``) is the umbrella
    that opts into both axes; the per-axis CLI flags / config keys still win
    independently. One home for this precedence — see
    ``resolve_flag_default``.

    ``[update]`` lives in sysforge.toml (alongside ``[build]``/``[safety]``),
    not profiles.toml — loaded via ``load_sysforge_toml()`` unless a config
    dict is injected directly (tests).
    """
    if update_cfg is None:
        update_cfg = load_sysforge_toml().get("update", {})
    ucfg = dict(update_cfg or {})
    rebuild_all = resolve_flag_default(args, "rebuild_on_drift", ucfg, "rebuild_on_drift")
    rebuild_tc = resolve_flag_default(
        args, "rebuild_on_toolchain_drift", ucfg, "rebuild_on_toolchain_drift") or rebuild_all
    rebuild_fl = resolve_flag_default(
        args, "rebuild_on_flag_drift", ucfg, "rebuild_on_flag_drift") or rebuild_all
    return rebuild_all, rebuild_tc, rebuild_fl


def _suppress_pagers_in_env(interactive: bool) -> None:
    """Force PAGER/GIT_PAGER/SYSTEMD_PAGER/LESS to non-paging values.

    Applied for the lifetime of cmd_update when ``--interactive`` is not
    set, so no subprocess (pacman post-install hooks, git, systemd tools
    invoked by hooks, makepkg subshells, meson configure, etc.) inherits
    a $PAGER that would put the terminal into alt-screen mode.

    Override (not setdefault): an exported ``PAGER=less`` from the user's
    .zshrc is a preference for *interactive shells*, not consent to be
    paged in the middle of a batch update. The only opt-in for paging is
    ``--interactive``, which short-circuits this function entirely.
    """
    if interactive:
        return
    os.environ["PAGER"] = "cat"
    os.environ["GIT_PAGER"] = "cat"
    os.environ["SYSTEMD_PAGER"] = "cat"
    os.environ["LESS"] = "-RFX"


def _toolchain_preflight_for_batch(to_build, config, args) -> bool:
    """Run the toolchain preflight against ``to_build``.

    Returns ``True`` when the batch may proceed (everything green or all
    failures cleared by auto-remediation), ``False`` when the user should
    fix something and re-invoke (and a fix-it block has been printed).
    """
    if getattr(args, "no_toolchain_preflight", False):
        return True

    inference_map = load_consumes_inference()
    conflict_groups = load_conflict_groups()
    rules = config.get("rules", [])

    # Matches `RUSTUP_TOOLCHAIN=stable`, `export RUSTUP_TOOLCHAIN=nightly`,
    # `RUSTUP_TOOLCHAIN="1.78"`, etc. Picked up from the build()/check() body
    # so the preflight probes the toolchain the PKGBUILD will actually use,
    # not the workstation's default. First pin in any function body wins —
    # if a PKGBUILD pins different toolchains across functions, that's
    # already ambiguous behaviour we'd defer to makepkg anyway.
    _rustup_pin_re = re.compile(r"RUSTUP_TOOLCHAIN[ \t]*=[ \t]*[\"']?([^\s\"';#]+)")

    per_pkg: dict[str, frozenset[str]] = {}
    lib32: set[str] = set()
    rust_pins: dict[str, str] = {}
    compilers: set[str] = set()
    for r in to_build:
        if r.pkgbuild_path is None:
            continue
        # Wide except: preflight is best-effort. If any per-package resolution
        # blows up (malformed PKGBUILD, missing profile, etc.) we skip that
        # package and let the actual build path surface the real error.
        try:
            pkgmeta = parse_pkgbuild(r.pkgbuild_path)
            matched = match_rules(pkgmeta, rules)
            resolved = resolve_profile(pkgmeta, matched, config, conflict_groups)
            consumes = resolve_consumes(resolved, pkgmeta, inference_map)
        except Exception as e:
            _log.warn(f"preflight: skipping {r.pkgbase} — {e}")
            continue
        per_pkg[r.pkgbase] = consumes
        # The resolved compiler(s) for this package — checked for executability
        # by the preflight so a broken toolchain (e.g. a clang that can't run)
        # aborts before any build instead of failing each package at compiler
        # detection.
        for key in ("CC", "CXX"):
            val = resolved.get(key)
            if val:
                compilers.add(val)
        # split-package PKGBUILDs may have multiple pkgnames; if any of them
        # is a lib32-* name, the whole pkgbase needs the i686 cross target.
        if any(str(n).startswith("lib32-") for n in r.pkgnames):
            lib32.add(r.pkgbase)
        functions = pkgmeta.get("functions") or {}
        for body in (functions.get("build", ""), functions.get("check", "")):
            m = _rustup_pin_re.search(body or "")
            if m:
                rust_pins[r.pkgbase] = m.group(1)
                break

    required = collect_required_toolchains(
        per_pkg, frozenset(lib32), rust_pins, frozenset(compilers)
    )
    if not required:
        return True

    report = run_toolchain_preflight(required)
    if report.failed:
        non_interactive = getattr(args, "non_interactive", False) or \
            getattr(args, "noconfirm", False)
        report = auto_remediate_toolchain(report, non_interactive=non_interactive)
    if report.failed:
        print(render_toolchain_preflight(report), file=sys.stderr)
        return False

    rendered = render_toolchain_preflight(report)
    if rendered:
        print(rendered)
    return True


# ---------------------------------------------------------------------------
# packages.toml loader
# ---------------------------------------------------------------------------

def _load_overrides(path: Path) -> tuple[dict, dict[str, dict]]:
    """Load packages.toml and return (build_cfg, overrides_by_name).

    `overrides_by_name` is keyed by package name; each value is the raw
    [[package]] entry dict. Entries are *overrides* applied to the live
    install set — they do not declare what should be installed.

    Emits a warn for any inert override entry (no behavior-changing field
    set) — those have no effect and should be removed.

    Returns ({}, {}) if the file does not exist or cannot be parsed.
    """
    if not path.exists():
        return {}, {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        build_cfg = dict(data.get("build", {}))
        overrides: dict[str, dict] = {}
        for entry in expand_package_groups(data):
            name = entry.get("name")
            if not name:
                continue
            overrides[name] = entry
            # Group-derived entries without defaults are legitimately inert at
            # steady-state (their meaning is the bootstrap install set) — only
            # hand-written [[package]] entries get the cleanup nudge.
            if entry_is_inert(entry) and "group" not in entry:
                _log.warn(
                    f"{name}: inert override (no behavior-changing field) — "
                    "has no effect; remove or add enable_build_from_source/cache/reason"
                )
        return build_cfg, overrides
    except Exception:
        return {}, {}


# ---------------------------------------------------------------------------
# Phase 5 helpers
# ---------------------------------------------------------------------------
# Built-artifact discovery (_find_existing_artifacts) and failure recording
# (_record_build_failure) live in sysforge.build_core, the shared build engine
# behind both `build` and `update`. They are imported at the top of this module.


# ---------------------------------------------------------------------------
# Pacman hook sentinels (libalpm PostTransaction reminder consumption)
# ---------------------------------------------------------------------------

# Path is fixed by the libalpm hooks shipped under /usr/share/libalpm/hooks/
# (see PKGBUILD package() and tools/pacman-hook-helper.sh). The matching
# tmpfiles.d entry creates the directory on package install; this function
# tolerates a missing dir for installs that predate the hooks.
_SENTINEL_DIR = Path("/var/lib/sysforge/sentinels")

_SENTINEL_REMINDERS = {
    "kernel": (
        "Kernel package(s) changed since last sysforge run — review whether "
        "kernel-dependent AUR packages (nvidia-dkms, *-headers, vbox modules) "
        "need a `sysforge update`."
    ),
    "toolchain": (
        "Toolchain package(s) (llvm/clang/gcc) changed since last sysforge "
        "run — packages built against the prior toolchain may want a rebuild; "
        "PGO profdata cached under toolchain.toml `pgo_store` may be stale."
    ),
}


def _consume_pacman_hook_sentinels(
    silent: bool = False, reminders_only: bool = False
) -> None:
    """Surface kernel/toolchain reminders dropped by pacman PostTransaction
    hooks since the last `sysforge update` run, then unlink them.

    The buildstate + self-install sentinels feed the external-install demotion
    reconcile (see ``_reconcile_external_demotions``). The start-of-run call
    passes ``reminders_only=True`` so those two survive for the reconcile step;
    the end-of-run call (default) clears them along with any kernel/toolchain
    sentinels sysforge's own Phase 5 (pacman -U) / Phase 6.5 (pacman -Syu)
    transactions just dropped, so they don't re-fire on the next invocation.

    silent=True suppresses the kernel/toolchain warnings but still unlinks.
    """
    if not _SENTINEL_DIR.is_dir():
        return
    for kind, reminder in _SENTINEL_REMINDERS.items():
        path = _SENTINEL_DIR / kind
        if path.exists():
            if not silent:
                _log.warn(reminder)
            try:
                path.unlink()
            except OSError:
                pass
    if reminders_only:
        return
    from sysforge.primitives.install_reconcile import clear_reconcile_sentinels
    clear_reconcile_sentinels(_SENTINEL_DIR)


def _reconcile_external_demotions(bs: BuildState) -> None:
    """Demote source-built packages reinstalled externally via ``pacman -S``.

    Reads the buildstate + self-install sentinels (the diff is the set of
    externally-installed packages), demotes any matching ``source_built``
    build_state entry to a ``pacman`` marker, saves if anything changed, then
    unlinks both sentinels. Best-effort — a sentinel/IO error never aborts the
    update. Stage-owned packages are exempt (handled in BuildState).
    """
    from sysforge.primitives.install_reconcile import (
        clear_reconcile_sentinels,
        external_install_targets,
    )
    try:
        external = external_install_targets(_SENTINEL_DIR)
        if external:
            demoted = bs.reconcile_external_installs(external)
            if demoted:
                bs.save()
                _log.info(
                    "demoted "
                    f"{len(demoted)} source-built package(s) reinstalled from "
                    f"the repo: {', '.join(sorted(demoted))}"
                )
    except Exception as e:
        _log.info(f"external-install reconcile skipped (non-fatal): {e}")
    finally:
        clear_reconcile_sentinels(_SENTINEL_DIR)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""

    # reminders_only: leave the buildstate + self-install sentinels in place so
    # the body's _reconcile_external_demotions can read them; it unlinks them.
    _consume_pacman_hook_sentinels(reminders_only=True)
    _suppress_pagers_in_env(getattr(args, "interactive", False))

    try:
        _cmd_update_body(args)
    finally:
        # Defensive: if any subprocess (pacman hook, makepkg subshell, etc.)
        # entered alt-screen mode and died without restoring, emit the reset
        # so the caller's scrollback isn't lost. No-op when stdout isn't a
        # TTY (CI, pipes).
        if sys.stdout.isatty():
            try:
                sys.stdout.write(_TERMINAL_RESET)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass


def _detect_stage_owned_updates(
    stage_owned_packages, *, all_installed, sync_failures,
    rpc_version_by_base, pacman_updates_map, skip_sync_check, offline,
) -> list[tuple[str, str | None, str | None, str]]:
    """Advisory-only: which stage-owned packages have a newer upstream version.

    Reuses the same per-pkgbase check the walk runs. Respects the offline gate
    exactly as the main walk does — offline skips the network check, so no
    advisory is produced. Best-effort: a None/failed check omits that package.
    """
    if offline or not stage_owned_packages:
        return []
    pkgbase_map, pkgbase_entry = group_by_pkgbase(stage_owned_packages)
    advisories: list[tuple[str, str | None, str | None, str]] = []
    for pkgbase, pkgnames in sorted(pkgbase_map.items()):
        entry = pkgbase_entry[pkgbase]
        owner = entry.get("owner_stage", "")
        try:
            r = _check_one_pkgbase(
                pkgbase, pkgnames, entry, sync_failures, all_installed,
                set(), skip_sync_check, rpc_version_by_base, False, None,
                pacman_updates_map,
            )
        except Exception:
            continue
        if r is not None and r.action in ("NEEDS_REBUILD", "NEEDS_PACMAN_UPGRADE"):
            advisories.append((pkgbase, r.installed_ver, r.pkgbuild_ver, owner))
    return advisories


def _build_result_summary(
    *, results, built_pkgs, failed_pkgs, pacman_upgrade_pkgs,
    installed_deps, pgo_skipped_pkgs, cleansrc_failures,
    install_only, pacman_upgrade_failed, skipped, stage_owned_updates,
) -> ResultSummary:
    """Assemble a ``ResultSummary`` from ``update``'s per-run state.

    Lives here (not in ``update_summary.py``) because it reads ``results``,
    keeping the renderer pure.
    """
    versions = {
        r.pkgbase: (r.installed_ver, r.pkgbuild_ver) for r in results
    }
    return ResultSummary(
        built_pkgs=list(built_pkgs),
        failed_pkgs=list(failed_pkgs),
        pacman_upgrade_pkgs=list(pacman_upgrade_pkgs),
        installed_deps=list(installed_deps),
        pgo_skipped_pkgs=list(pgo_skipped_pkgs),
        cleansrc_failures=list(cleansrc_failures),
        install_only=install_only,
        pacman_upgrade_failed=pacman_upgrade_failed,
        skipped=skipped,
        versions=versions,
        stage_owned_updates=list(stage_owned_updates),
    )


def _emit_timings(timer: PhaseTimer, args) -> None:
    """Render the phase wall-clock report under [UPDATE].

    Always written at info level (lands in the unified log); promoted to UI
    output when --timings is set."""
    timer.stop()  # close any open start()-phase on early-exit paths
    emit = _log.ui if getattr(args, "timings", False) else _log.info
    for line in render_report(timer, title="Phase timings"):
        emit(line)


def _cmd_update_body(args) -> None:
    # ── Phase 0: Init ─────────────────────────────────────────────────────
    _ui_progress.phase("loading state and config")
    timer = PhaseTimer()
    install_only = getattr(args, "install_only", False)
    offline = getattr(args, "offline", False) or install_only
    if install_only:
        args.offline = True
    if not offline:
        fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # External-install demotion: a source-built package reinstalled from the
    # repo via `pacman -S` (buildstate hook target, minus sysforge's own
    # pacman -U self-install targets) is demoted back to a plain pacman marker
    # so this run doesn't rebuild it from source and undo the user's switch.
    _reconcile_external_demotions(bs)

    # Active toolchain variant — stamped onto every rebuild via BuildOptions
    # and used below to surface drift between installed packages' recorded
    # variant and what's active now. ``"system"`` means the toolchain stage
    # has never run on this state dir; treat as a benign no-op (no stamp).
    _pstate = PipelineState(state_dir)
    active_variant = get_toolchain_variant(_pstate)
    # Companion to active_variant (Q9): identity of the active toolchain's
    # compiler, computed once. Stamped onto every rebuild below and compared
    # against each package's recorded fingerprint to catch a same-variant
    # toolchain rebuild (fresh codegen, unchanged soname). None when variant is
    # "system" (no toolchain stage run — nothing to compare).
    active_fingerprint = get_toolchain_fingerprint(_pstate)

    # Superset sync: build_state.toml carries an entry for every installed
    # package (pacman-mode marker for those sysforge didn't build), so that
    # every `pacman -Q` name has a known state and zombie entries left by
    # prior parser runs (e.g. literal ``$_pkgname`` keys) are pruned.
    all_installed = get_all_installed_packages()
    try:
        sync_result = bs.sync_with_installed(all_installed)
    except OSError as e:
        _log.warn(f"build_state sync failed: {e}")
    else:
        if isinstance(sync_result, tuple) and len(sync_result) == 2:
            added, removed = sync_result
            if added or removed:
                bs.save()
                _log.info(f"build_state sync: +{added} pacman-mode, -{removed} stale")

    # Unified log — always on, always truncate.
    unified_log_active = not getattr(args, "dry_run", False)
    unified_log_path = (Path(args.log_dir) if getattr(args, "log_dir", None) else state_dir) / "sysforge-update.log"
    if unified_log_active:
        try:
            log.open_unified_log(unified_log_path, purge=True)
            _log.info(f"Unified log: {unified_log_path}")
        except OSError as e:
            unified_log_active = False
            _log.warn(f"Cannot write unified log to {unified_log_path}: {e} — logging to terminal only")

    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths) or {}
    if getattr(args, "packages", None):
        config["packages_file"] = args.packages

    packages_path = resolve_packages_path(config)
    build_cfg, overrides_by_name = _load_overrides(packages_path)

    # PKGBUILD review gate mode (runs inside build_core.build_and_install):
    # "auto" by default — changed sources are auto-accepted with a logged
    # notice so a plain `update` stays unattended. --review opts into the
    # interactive diff prompt; --no-review / [build] review = false in
    # packages.toml skips the gate entirely.
    if getattr(args, "no_review", False) or build_cfg.get("review", True) is False:
        review_mode = "off"
    elif getattr(args, "review", False):
        review_mode = "prompt"
    else:
        review_mode = "auto"

    # ── Phase 1: Package set assembly ─────────────────────────────────────
    _ui_progress.phase("assembling package set")
    packages, unrecorded_names, stage_owned_packages = _assemble_package_set(
        args, bs, config, build_cfg, overrides_by_name,
    )

    if not packages:
        # Keep going when profiled build_state entries exist: Phase 4.3's
        # build-state-wide fold still owes them drift detection (repo-class
        # packages recorded by `sysforge build` with no override). Every
        # phase in between no-ops on an empty package set.
        _has_source_built_entries = any(
            e.get("build_mode") == BUILD_MODE_SOURCE for e in bs.all_packages().values()
        )
        if not _has_source_built_entries:
            print(
                "[SYSFORGE] No installed packages in scope (no foreign packages, "
                "and no repo packages with overrides in packages.toml).",
                file=sys.stderr,
            )
            return

    pkgbase_map, pkgbase_entry = group_by_pkgbase(packages)

    # ── Phase 1.5: LLVM safety pre-flight (informational) ─────────────────
    if not getattr(args, "no_llvm_preflight", False):
        llvm_report = collect_llvm_state(list(pkgbase_map.keys()), config)
        if llvm_report.states:
            print(render_llvm_preflight(llvm_report))

    # Authoritative pkgbuild versions for packages whose PKGBUILDs use bash
    # parameter expansion the static parser can't evaluate. The scheduler's
    # SourceMetaCache holds the latest AUR RPC Version per pkgbase.
    rpc_version_by_base = {
        pb: meta["rpc_version"]
        for pb, meta in get_scheduler().cache.all().items()
        if meta.get("rpc_version")
    }

    # ── Phase 2: Source sync ──────────────────────────────────────────────
    with timer.phase("source sync"):
        sync_failures = _sync_sources(pkgbase_map, pkgbase_entry, args)

    # Pacman fast-path: one ``checkupdates`` call covers every pacman-class
    # repo package in scope. Only run it when at least one pacman-class
    # entry exists — skip the subprocess otherwise so default-mode runs
    # (``repo_mode = "pacman"``) pay nothing for this feature.
    has_pacman_class = any(
        e.get("repo_class") == "pacman" for e in pkgbase_entry.values()
    )
    pacman_updates_map: dict[str, str] | None
    if has_pacman_class and not offline:
        pacman_updates_map = checkupdates_map()
        if pacman_updates_map is None:
            _log.warn(
                "checkupdates unavailable — pacman-class repo packages will "
                "be reported as SKIPPED_NO_CHECKUPDATES; install pacman-contrib"
            )
    else:
        # Offline or no pacman-class packages — pass an empty dict so the
        # worker takes the "no upgrade pending" branch (UP_TO_DATE).
        pacman_updates_map = {} if has_pacman_class else None

    # ── Phase 3: Version check ────────────────────────────────────────────
    skip_sync_check = offline
    results: list[_UpdateResult] = []

    force_devel = getattr(args, "devel", False)
    # Per-pkgbase upstream-commit cache for the --devel ls-remote short-circuit.
    # Read once before fan-out so worker threads don't all touch BuildState.
    # Field is only populated for single-git-source VCS packages that have
    # been successfully built since the optimisation landed; missing → None
    # means the worker falls back to the full evaluate_vcs_pkgver path.
    built_commit_by_base: dict[str, str | None] = {}
    if force_devel:
        for pkgbase, pkgnames in pkgbase_map.items():
            for pn in pkgnames:
                rec = bs.get(pn)
                if rec is not None:
                    sha = rec.get("built_upstream_commit")
                    if sha:
                        built_commit_by_base[pkgbase] = sha
                        break
    timer.start("version check")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _check_one_pkgbase, pkgbase, pkgnames,
                pkgbase_entry[pkgbase], sync_failures, all_installed,
                unrecorded_names, skip_sync_check, rpc_version_by_base,
                force_devel,
                built_commit_by_base.get(pkgbase),
                pacman_updates_map,
            ): pkgbase
            for pkgbase, pkgnames in sorted(pkgbase_map.items())
        }
        with _ui_progress.tracker(len(futures), "version check") as _tick:
            for fut in as_completed(futures):
                _tick(futures[fut])
                result = fut.result()
                if result is not None:
                    results.append(result)

    results.sort(key=lambda r: r.pkgbase)
    timer.stop()

    # ── Phase 4: Summary + dry-run gate ───────────────────────────────────
    _print_summary(results, args)

    _ui_progress.phase("checking toolchain and flag drift")
    timer.start("drift detection")
    # ── Phase 4.25: Toolchain-variant drift ───────────────────────────────
    # Compare each result's recorded toolchain_variant (from build_state)
    # against the active toolchain. Drift means "the installed binary was
    # produced under a different compiler identity than is active now".
    # Pure pacman-mode entries have no variant and are never drift candidates.
    # Surface as a one-line summary (always) and a full list under
    # --explain-drift; opt-in rebuild via --rebuild-on-toolchain-drift.
    # (pkgbase, recorded_variant, reason). The reason distinguishes the two
    # cases the drift check now folds together: a different variant name, or a
    # same-variant toolchain rebuild caught by the fingerprint (Q9).
    drifted: list[tuple[str, str, str]] = []
    if active_variant != "system":
        seen_bases: set[str] = set()
        for r in results:
            if r.pkgbase in seen_bases:
                continue
            seen_bases.add(r.pkgbase)
            # Pick the recorded variant from any one pkgname under this base
            # — toolchain stamping is per-pkgname but the build that produced
            # them is per-pkgbase, so all entries in a split package agree.
            for name in r.pkgnames:
                rec = bs.get(name) or {}
                rec_variant = rec.get("toolchain_variant")
                if rec_variant is None:
                    continue
                if rec_variant != active_variant:
                    drifted.append((
                        r.pkgbase, rec_variant,
                        f"built under a different variant than active "
                        f"({active_variant})",
                    ))
                else:
                    # Same variant name: flag only when both fingerprints are
                    # present and differ. A missing recorded fingerprint (built
                    # before Q9) is never flagged — compared only when both sides
                    # exist. This is the same-variant, same-soname, different-
                    # codegen case (e.g. a fresh-profdata PGO rebuild).
                    rec_fp = rec.get("toolchain_fingerprint")
                    if (rec_fp and active_fingerprint
                            and rec_fp != active_fingerprint):
                        drifted.append((
                            r.pkgbase, rec_variant,
                            f"toolchain rebuilt since build (same variant: "
                            f"{rec_variant})",
                        ))
                break

    if drifted:
        sample = ", ".join(f"{pb} ({rv})" for pb, rv, _ in drifted[:3])
        more = f" (+{len(drifted) - 3} more)" if len(drifted) > 3 else ""
        _log.ui(
            f"toolchain drift: {len(drifted)} package(s) built under a "
            f"different toolchain than active ({active_variant}): {sample}{more}. "
            "Pass --rebuild-on-toolchain-drift to rebuild, or "
            "--explain-drift to list."
        )

    # ── Phase 4.3: Flag drift ─────────────────────────────────────────────
    # Re-resolve the current profile for each profiled package and diff the
    # serialized flags against what was recorded at build time. Same
    # detect-and-report contract as toolchain drift above: surfaced always,
    # listed under --explain-drift, rebuilt only via --rebuild-on-flag-drift.
    # This is the canonical home for flag-drift detection (the shared engine
    # is resolve_flag_drift in primitives/flag_drift.py).
    # Conflict groups are loaded lazily so a run with no profiled packages
    # pays nothing.
    flag_drifted: list[tuple[str, list[str]]] = []  # (pkgbase, diff lines)
    _flag_seen: set[str] = set()
    _flag_cgroups = None
    for r in results:
        if r.pkgbase in _flag_seen:
            continue
        _flag_seen.add(r.pkgbase)
        entry = None
        for name in r.pkgnames:
            entry = bs.get(name)
            if entry is not None:
                break
        if not entry or entry.get("build_mode") != BUILD_MODE_SOURCE:
            continue
        if _flag_cgroups is None:
            _flag_cgroups = load_conflict_groups()
        fd = resolve_flag_drift(entry, config, _flag_cgroups)
        if fd.status == STATUS_PARSE_ERROR:
            _log.warn(
                f"flag drift: {r.pkgbase} — failed to parse PKGBUILD: {fd.error}"
            )
            continue
        if fd.drifted:
            flag_drifted.append((r.pkgbase, fd.diffs))

    # Build-state-wide coverage (absorbed from the removed `converge` verb):
    # profiled build_state entries outside this run's package walk — e.g.
    # stage-owned (kernel/toolchain) packages filtered from the walk, or
    # entries excluded by a `sysforge update <pkg>` name filter — still get
    # drift detection. (A plain source-built package is now in the walk under
    # the build_state-is-authority model, so it is promotable directly.)
    # Detect/report and --explain-drift only: promotion to NEEDS_REBUILD needs
    # an entry in this run's walk, so out-of-walk drifters get a `sysforge
    # build` / owning-stage hint instead.
    _fold_filter = set(getattr(args, "pkgnames", None) or [])
    fold_drifted: set[str] = set()
    _fold_map, _fold_entry = group_by_pkgbase(bs.all_packages())
    for pkgbase, pkgnames in sorted(_fold_map.items()):
        if pkgbase in _flag_seen:
            continue
        _flag_seen.add(pkgbase)
        if _fold_filter and not (_fold_filter & ({pkgbase} | set(pkgnames))):
            continue
        entry = _fold_entry[pkgbase]
        if entry.get("build_mode") != BUILD_MODE_SOURCE:
            continue
        if _flag_cgroups is None:
            _flag_cgroups = load_conflict_groups()
        fd = resolve_flag_drift(entry, config, _flag_cgroups)
        if fd.status == STATUS_PARSE_ERROR:
            _log.warn(
                f"flag drift: {pkgbase} — failed to parse PKGBUILD: {fd.error}"
            )
            continue
        if fd.drifted:
            flag_drifted.append((pkgbase, fd.diffs))
            fold_drifted.add(pkgbase)

    if flag_drifted:
        sample = ", ".join(pb for pb, _ in flag_drifted[:3])
        more = f" (+{len(flag_drifted) - 3} more)" if len(flag_drifted) > 3 else ""
        _log.ui(
            f"flag drift: {len(flag_drifted)} package(s) resolve to different "
            f"flags than when built: {sample}{more}. "
            "Pass --rebuild-on-flag-drift to rebuild, or --explain-drift to list."
        )

    if getattr(args, "explain_drift", False):
        if not drifted:
            print(
                f"[SYSFORGE] No toolchain drift. Active variant: "
                f"{active_variant}."
            )
        else:
            print(
                f"[SYSFORGE] {len(drifted)} package(s) built under a "
                f"different toolchain than active "
                f"({active_variant}):"
            )
            for pkgbase, rec_variant, reason in sorted(drifted):
                print(f"  {pkgbase:<40}  recorded={rec_variant}  ({reason})")
        if not flag_drifted:
            print("[SYSFORGE] No flag drift.")
        else:
            print(
                f"[SYSFORGE] {len(flag_drifted)} package(s) resolve to "
                "different flags than when built:"
            )
            for pkgbase, diffs in sorted(flag_drifted):
                print(f"  {pkgbase}")
                for line in diffs:
                    print(line)
        _emit_timings(timer, args)
        return

    if getattr(args, "dry_run", False):
        _emit_timings(timer, args)
        return

    # --rebuild-on-drift (or [update] rebuild_on_drift) is the umbrella that
    # opts into both drift axes; CLI flags still win over config.
    _rebuild_all_drift, _rebuild_tc_drift, _rebuild_fl_drift = _resolve_drift_axes(args)

    if _rebuild_tc_drift and drifted:
        drifted_bases = {pb for pb, _, _ in drifted}
        promoted = 0
        unbuildable = 0
        for r in results:
            if r.pkgbase in drifted_bases and r.action == "UP_TO_DATE":
                if r.pkgbuild_path is None:
                    unbuildable += 1
                    continue
                r.action = "NEEDS_REBUILD"
                promoted += 1
        if promoted:
            _log.ui(
                f"--rebuild-on-toolchain-drift: promoted {promoted} "
                "UP_TO_DATE package(s) to NEEDS_REBUILD"
            )
        if unbuildable:
            _log.warn(
                f"--rebuild-on-toolchain-drift: {unbuildable} drifted "
                "package(s) have no resolvable PKGBUILD (likely pacman-class) "
                "— skipped"
            )

    if _rebuild_fl_drift and flag_drifted:
        flag_bases = {pb for pb, _ in flag_drifted}
        promoted = 0
        unbuildable = 0
        for r in results:
            if r.pkgbase in flag_bases and r.action == "UP_TO_DATE":
                if r.pkgbuild_path is None:
                    unbuildable += 1
                    continue
                r.action = "NEEDS_REBUILD"
                promoted += 1
        if promoted:
            _log.ui(
                f"--rebuild-on-flag-drift: promoted {promoted} "
                "UP_TO_DATE package(s) to NEEDS_REBUILD"
            )
        if unbuildable:
            _log.warn(
                f"--rebuild-on-flag-drift: {unbuildable} drifted "
                "package(s) have no resolvable PKGBUILD — skipped"
            )
        if fold_drifted:
            names = ", ".join(sorted(fold_drifted)[:3])
            more = f" (+{len(fold_drifted) - 3} more)" if len(fold_drifted) > 3 else ""
            _log.warn(
                f"--rebuild-on-flag-drift: {len(fold_drifted)} drifted "
                f"package(s) are outside this run's package walk and were not "
                f"queued: {names}{more}. Rebuild with `sysforge build <pkg>` "
                "(or the owning pipeline stage)."
            )

    timer.stop()

    # ── Phase 5: Build ────────────────────────────────────────────────────
    # _check_one_pkgbase already resolved VCS pkgver() under --devel and set
    # NEEDS_REBUILD / UP_TO_DATE / DEVEL_EVAL_FAILED accordingly. The plain
    # NEEDS_REBUILD filter here therefore picks up genuinely-stale -git
    # packages and excludes up-to-date ones.
    to_build = [r for r in results if r.action == "NEEDS_REBUILD"]
    pending_pacman_upgrade = [
        r for r in results if r.action == "NEEDS_PACMAN_UPGRADE"
    ]

    # Exclude packages that failed source sync (cleansrc refusal, etc.)
    cleansrc_failures = {k: msg for k, (status, msg) in sync_failures.items()
                         if status == STATUS_PURGE_REFUSED}
    if sync_failures:
        to_build = [r for r in to_build if r.pkgbase not in sync_failures]

    if not to_build and not pending_pacman_upgrade:
        _ui_progress.phase(None)
        print("[SYSFORGE] Nothing to rebuild.")
        _emit_timings(timer, args)
        return

    pkgdest = get_pkgdest()
    built_pkg_files: list = []
    built_pkgs: list[str] = []
    failed_pkgs: list[str] = []
    pgo_skipped_pkgs: list[str] = []
    review_skipped_pkgs: list[str] = []
    install_failed = False
    outcome = None  # only set on the build_and_install path (Task 3: F38)

    if not to_build:
        # Nothing to source-build, but pacman-class upgrades are pending —
        # skip Phase 5 (source build) and fall through to Phase 6.5
        # (pacman -Syu) below. Bypass install_only/normal-build branching.
        pass
    elif install_only:
        # Skip the whole build loop: no makedep batching, no AUR-dep resolution,
        # no makepkg invocation. For each result the version-check filter has
        # already proved is newer than installed, look for a matching artifact
        # at exactly that pkgbuild_ver in PKGDEST and queue it for install.
        with _ui_progress.tracker(len(to_build), "scanning") as _tick:
            for result in to_build:
                _tick(result.pkgbase)
                search_dir = pkgdest if pkgdest else (
                    result.pkgbuild_path.parent if result.pkgbuild_path else None
                )
                existing = _find_existing_artifacts(
                    search_dir, result.pkgnames, result.pkgbuild_ver,
                    installed_ver=result.installed_ver,
                ) if search_dir else []
                if existing:
                    _log.info(
                        f"{result.pkgbase}: queuing pre-built artifact "
                        f"({existing[0].name})"
                    )
                    built_pkg_files.extend(existing)
                    built_pkgs.append(result.pkgbase)
                else:
                    _log.info(
                        f"{result.pkgbase}: [SKIP] no built artifact for "
                        f"{result.pkgbuild_ver} in {search_dir}"
                    )
        # Install the queued pre-built artifacts (no build happened).
        built_pkg_files, install_failed = build_core.install_built(built_pkg_files)
        if not built_pkg_files and built_pkgs:
            _log.warn("No .pkg.tar.* files eligible to install — nothing to do")
    else:
        # ── Phase 4.5: Toolchain pre-flight ───────────────────────────────
        # Probes rust/cmake/meson availability (incl. rustup cross targets
        # for lib32-* packages) before any makepkg runs. Auto-remediates a
        # missing `rustup target add ...` when run interactively; otherwise
        # prints a fix block and aborts the batch.
        _ui_progress.phase("toolchain preflight")
        if not _toolchain_preflight_for_batch(to_build, config, args):
            print("[SYSFORGE] Toolchain pre-flight failed — aborting batch.",
                  file=sys.stderr)
            sys.exit(1)

        # ── Phase 5 + 6: dep prep, build loop, bulk install ───────────────
        # The shared build engine behind both `build` and `update`. `update`
        # passes sync_source=False because Phase 2 already synced sources via
        # the scheduler. `to_build` elements (_UpdateResult) are duck-typed as
        # BuildTargets (pkgbase / pkgnames / pkgbuild_path / source / ...).
        outcome = build_core.build_and_install(
            to_build,
            config=config,
            sync_source=False,
            interactive=getattr(args, "interactive", False),
            no_cleanbuild=getattr(args, "no_cleanbuild", False),
            profile_conf=getattr(args, "profile_conf", None),
            state_dir=state_dir,
            pkg_log=not getattr(args, "no_pkg_log", False),
            persist_log=resolve_flag_default(args, "persist_log", build_cfg, "persist_log"),
            log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
            cache_report=resolve_flag_default(args, "cache_report", build_cfg, "cache_report"),
            extra_flags=(
                expand_makepkg_flags(args.makepkg)
                if getattr(args, "makepkg", None) else None
            ),
            active_variant=active_variant,
            toolchain_fingerprint=active_fingerprint,
            pkgdest=pkgdest,
            review=review_mode,
            timer=timer,
        )
        if outcome.aborted:
            # User aborted at the PKGBUILD review gate — build_core already
            # printed the abort line; nothing was built or installed.
            return
        built_pkgs = outcome.built_pkgs
        failed_pkgs = outcome.failed_pkgs
        pgo_skipped_pkgs = outcome.pgo_skipped_pkgs
        review_skipped_pkgs = outcome.review_skipped
        built_pkg_files = outcome.built_pkg_files
        install_failed = outcome.install_failed
        if not built_pkg_files and built_pkgs:
            _log.warn("No .pkg.tar.* files eligible to install — nothing to do")

    # ── Phase 6.5: Bulk pacman upgrade (pacman-class repo packages) ────────
    # Source-built artifacts are installed first so the `IgnoreGroup =
    # sf-build` line in /etc/pacman.conf (added by `sysforge setup`) keeps
    # `pacman -Syu` from clobbering them with upstream repo binaries. We
    # invoke a single transaction even though the version-check already
    # listed specific packages — running -Syu is what the user would do
    # by hand and stays consistent with `pacman -Syu` semantics.
    # Hand the bottom row back before pacman -Syu — it's fully interactive
    # (confirmation prompt, its own progress bars).
    _ui_progress.phase(None)
    pacman_upgrade_pkgs = sorted(r.pkgbase for r in pending_pacman_upgrade)
    pacman_upgrade_failed = False
    if pacman_upgrade_pkgs and not offline:
        noconfirm = getattr(args, "noconfirm", False)
        cmd = ["sudo", "pacman", "-Syu"]
        if noconfirm:
            cmd.append("--noconfirm")
        _log.info(
            f"Running pacman -Syu for {len(pacman_upgrade_pkgs)} repo "
            f"package(s): {' '.join(pacman_upgrade_pkgs)}"
        )
        import subprocess as _subprocess
        with timer.phase("pacman -Syu"):
            rc = _subprocess.run(cmd).returncode
        if rc != 0:
            _log.error(f"pacman -Syu exited {rc}")
            pacman_upgrade_failed = True

    # (The build cache-probe session report is emitted inside
    # build_core.build_and_install when --cache-report is set.)

    # Sync failures from cleansrc refusals count as build failures.
    failed_pkgs.extend(sorted(cleansrc_failures))

    # review_skipped_pkgs are inside to_build (they reached the gate), so add
    # them back into the skipped count.
    skipped = (len(results) - len(to_build) - len(pending_pacman_upgrade)
               + len(review_skipped_pkgs))
    if install_only:
        skipped += len(to_build) - len(built_pkgs) - len(failed_pkgs)
    installed_deps = outcome.installed_deps if outcome is not None else []
    stage_owned_updates = _detect_stage_owned_updates(
        stage_owned_packages,
        all_installed=all_installed,
        sync_failures=sync_failures,
        rpc_version_by_base=rpc_version_by_base,
        pacman_updates_map=pacman_updates_map,
        skip_sync_check=skip_sync_check,
        offline=offline,
    )
    summary = _build_result_summary(
        results=results,
        built_pkgs=built_pkgs,
        failed_pkgs=failed_pkgs,
        pacman_upgrade_pkgs=pacman_upgrade_pkgs,
        installed_deps=installed_deps,
        pgo_skipped_pkgs=pgo_skipped_pkgs,
        cleansrc_failures=sorted(cleansrc_failures),
        install_only=install_only,
        pacman_upgrade_failed=pacman_upgrade_failed,
        skipped=skipped,
        stage_owned_updates=stage_owned_updates,
    )
    # Route through _log.ui (not bare print) so the end-of-run summary is
    # mirrored into the unified log the way the old inline block was.
    _print_result_summary(summary, emit=_log.ui)

    _emit_timings(timer, args)

    if unified_log_active:
        log.close_unified_log(
            success=(not failed_pkgs and not install_failed
                     and not pacman_upgrade_failed),
            persist=True,
        )
        _log.ui(f"[SYSFORGE] Unified log: {unified_log_path}")

    # Clear sentinels that our own Phase 5 / Phase 6.5 pacman transactions
    # may have dropped this run; the start-of-cmd_update consume already
    # surfaced anything left by transactions outside sysforge.
    _consume_pacman_hook_sentinels(silent=True)


# ---------------------------------------------------------------------------
# Verb wrapper
# ---------------------------------------------------------------------------

from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class UpdateVerb(Verb):
    """Check for and rebuild outdated sysforge-managed packages.

    ``--install-only`` is incompatible with build-tuning flags; that
    conflict is enforced in ``pre_check`` so the verb short-circuits
    before any state mutation.

    Sentinel: yes. ``cmd_update`` issues ``sudo pacman -U`` against built
    artifacts at the end of the run; an interrupt between build and
    install can leave the live system mismatched, so the sentinel covers
    the whole run.
    """

    name = "update"
    requires_sentinel = True

    def pre_check(self, args) -> PreCheckResult:
        if getattr(args, "install_only", False):
            conflicts = [
                ("--makepkg", getattr(args, "makepkg", None)),
                ("--no-cleanbuild", getattr(args, "no_cleanbuild", False)),
                ("--cleansrc", getattr(args, "cleansrc", False)),
                ("--cleansrc-force", getattr(args, "cleansrc_force", False)),
                ("--interactive", getattr(args, "interactive", False)),
                ("--cache-report", getattr(args, "cache_report", False)),
            ]
            bad = [name for name, val in conflicts if val]
            if bad:
                return PreCheckResult(
                    blocker=(
                        f"--install-only is incompatible with: {', '.join(bad)} "
                        "(no rebuild happens, so build-tuning flags have no effect)"
                    ),
                    exit_code=1,
                )
        # Dry runs and offline-only paths don't mutate the live system —
        # skip the sentinel for those cases.
        if getattr(args, "dry_run", False):
            self.requires_sentinel = False
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_update(args)
        return ExecResult()
