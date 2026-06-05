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
from sysforge.primitives.build_state import BuildState, group_by_pkgbase
from sysforge.primitives.version import format_version, vercmp
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.vcs_pkgver import evaluate_vcs_pkgver, peek_upstream_commit
from sysforge.primitives.aur import fetch_aur_name_cache, aur_info
from sysforge.primitives.source_sync import (
    SyncRequest, SyncResult,
    STATUS_DIVERGED, STATUS_FAILED, STATUS_PURGE_REFUSED, STATUS_RATE_LIMITED,
    get_scheduler,
)
from sysforge.primitives.config import (
    load_config, load_sysforge_toml,
    load_conflict_groups, load_consumes_inference,
)
from sysforge.primitives.llvm_state import (
    collect_llvm_state,
    render_preflight as render_llvm_preflight,
)
from sysforge.primitives.profile import (
    match_rules, resolve_profile, resolve_consumes,
)
from sysforge.primitives.toolchain_preflight import (
    collect_required_toolchains,
    run_preflight as run_toolchain_preflight,
    render_preflight as render_toolchain_preflight,
    auto_remediate as auto_remediate_toolchain,
)
from sysforge.primitives.paths import resolve_packages_path
from sysforge.primitives.stage_ownership import load_stage_ownership
from sysforge.primitives.makepkg_wrapper import expand_makepkg_flags
from sysforge.primitives.pacman import (
    get_pkgdest,
    get_all_installed_packages,
    get_foreign_packages,
    get_pkgbase,
    checkupdates_map,
)
from sysforge import build_core
from sysforge.build_core import _find_existing_artifacts
from sysforge.pipeline.state import (
    PipelineState,
    get_toolchain_variant,
    resolve_state_dir,
)
from sysforge.packages_cmd import entry_is_inert
from sysforge.update_result import _UpdateResult


_VCS_SUFFIXES = ("-git", "-svn", "-hg", "-bzr")


# Escape sequences to restore the terminal after an interrupted child that
# entered alt-screen / hid the cursor / left SGR state set (e.g. a pager
# killed by Ctrl-C). Written verbatim to stdout from the cmd_update finally
# block when stdout is a TTY.
_TERMINAL_RESET = "\x1b[?1049l\x1b[?25h\x1b[0m"


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

# Sync statuses that block the package from proceeding to build, and the
# user-facing action each maps to in the update summary. Statuses absent
# from this map (UP_TO_DATE, FETCHED, CLONED, DIVERGED, SKIPPED_OFFLINE,
# SKIPPED_NO_TRACKING) are non-blocking — the build proceeds against the
# local PKGBUILD.
_SYNC_STATUS_TO_ACTION = {
    STATUS_FAILED: "PULL_FAILED",
    STATUS_RATE_LIMITED: "RATE_LIMITED",
    STATUS_PURGE_REFUSED: "PURGE_REFUSED",
}
_SYNC_BLOCKING_STATUSES = frozenset(_SYNC_STATUS_TO_ACTION)


def _is_vcs(pkgbase: str) -> bool:
    return any(pkgbase.endswith(s) for s in _VCS_SUFFIXES)


# ---------------------------------------------------------------------------
# packages.toml loader
# ---------------------------------------------------------------------------

def _load_overrides(path: Path) -> tuple[dict, dict[str, dict]]:
    """Load packages.toml and return (build_cfg, overrides_by_name).

    `overrides_by_name` is keyed by package name; each value is the raw
    [[package]] entry dict. Entries are *overrides* applied to the live
    install set — they do not declare what should be installed.

    Normalises the deprecated `update_repo_profiled = true` key into
    `repo_mode = "profiled"` (with a one-shot warning) and emits a warn
    for any inert override entry (no behavior-changing field set) — those
    have no effect and should be removed.

    Returns ({}, {}) if the file does not exist or cannot be parsed.
    """
    if not path.exists():
        return {}, {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        build_cfg = dict(data.get("build", {}))
        if "update_repo_profiled" in build_cfg:
            _log.warn(
                "[build] update_repo_profiled is deprecated; "
                'use [build] repo_mode = "profiled" instead'
            )
            if build_cfg.pop("update_repo_profiled") is True:
                build_cfg.setdefault("repo_mode", "profiled")
        overrides: dict[str, dict] = {}
        for entry in data.get("package", []):
            name = entry.get("name")
            if not name:
                continue
            overrides[name] = entry
            if entry_is_inert(entry):
                _log.warn(
                    f"{name}: inert override (no behavior-changing field) — "
                    "has no effect; remove or add pkgbuild_patch/cache/reason"
                )
        return build_cfg, overrides
    except Exception:
        return {}, {}


# ---------------------------------------------------------------------------
# Phase 1: Package set assembly
# ---------------------------------------------------------------------------

def _assemble_package_set(
    args, bs: BuildState, config: dict,
    build_cfg: dict, overrides_by_name: dict[str, dict],
) -> tuple[dict[str, dict], set[str]]:
    """Phase 1: build the unified {pkgname: entry} dict from the live install set.

    Iteration scope:
      - Every installed foreign package (`pacman -Qm`) — always.
      - Installed repo packages whose override entry sets a behavior-changing
        field (``pkgbuild_patch``, ``cache``, ``reason``). A bare
        ``source = "repo"`` entry is inert metadata and is *not* a trigger
        (matches the `sysforge packages add` validator).
      - When ``[build] repo_mode = "profiled"`` in packages.toml, every
        installed repo package is iterated as well (the version-check phase
        compares against ``pkgctl repo clone``-resolved PKGBUILDs from the
        Arch packaging repo). Designed for users who maintain a fully
        profiled system and want repo-side version drift surfaced alongside
        AUR drift in a single ``sysforge update`` run. The deprecated
        ``update_repo_profiled = true`` is normalised to this by the loader.

    `overrides_by_name` is applied as an overlay (`source`, `pkgbuild_patch`,
    `cache`, `reason`); installed packages with no override use defaults.
    Override entries whose package is not currently installed are inert
    rules and are not iterated.

    Returns (packages, unrecorded_names).
    """
    build_state_pkgs = bs.all_packages()

    pkgbuild_src_dir_raw = (
        build_cfg.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )
    pkgbuild_src_dir_base = Path(pkgbuild_src_dir_raw).expanduser() if pkgbuild_src_dir_raw else None

    foreign = set(get_foreign_packages().keys())
    # Behavior-changing overrides (pkgbuild_patch / cache / reason) are what
    # pull a non-foreign package into update scope. `source` alone is inert
    # metadata per the `packages add` contract.
    behavior_overridden = {
        name for name, ov in overrides_by_name.items()
        if not entry_is_inert(ov)
    }
    repo_mode_profiled = build_cfg.get("repo_mode") == "profiled"

    # Live install set: every installed foreign + every non-foreign package
    # carrying a behavior-changing override. With repo_mode = "profiled",
    # also pull in every installed repo package.
    all_installed = get_all_installed_packages()
    target_names = {n for n in all_installed
                    if n in foreign
                    or n in behavior_overridden
                    or (repo_mode_profiled and n not in foreign)}

    # Stage-owned packages: skipped by default so `sysforge update` doesn't
    # double-process kernel-owned packages alongside `sysforge run kernel`.
    # Authoritative source is ``owner_stage`` recorded in build_state by the
    # owning stage; the kernel.toml bootstrap fallback covers the first run
    # before any stamp exists. ``--include-stage-owned`` overrides the skip
    # (also includes packages explicitly named on the command line).
    include_stage_owned = bool(getattr(args, "include_stage_owned", False))
    filter_names: list[str] = getattr(args, "pkgnames", None) or []
    explicit_set = set(filter_names)
    stage_owned: dict[str, str] = {}
    if not include_stage_owned:
        for name in target_names:
            owner = (build_state_pkgs.get(name) or {}).get("owner_stage")
            if owner:
                stage_owned[name] = owner
        # Config bootstrap fallback: before a stage has stamped owner_stage —
        # and for entries written by older code predating the field — infer
        # ownership from the stage configs (kernel.toml / toolchain.toml). The
        # snapshot reads each config once; ``owner_of`` matches both the package
        # name and its resolved pkgbase so split packages (e.g. llvm-libs/polly
        # under pkgbase llvm, or kernel split members) are classified correctly.
        # pkgbase is the one recorded in build_state by makepkg, falling back to
        # pacman's offline `%BASE%` lookup for packages built before that field
        # existed. See primitives/stage_ownership.py for the ownership rules.
        ownership = load_stage_ownership()
        if ownership.any_active:
            for name in target_names:
                if name in stage_owned:
                    continue
                entry = build_state_pkgs.get(name) or {}
                base = entry.get("pkgbase") or get_pkgbase(name) or name
                owner = ownership.owner_of(name, base)
                if owner:
                    stage_owned[name] = owner
        # Explicit names on the command line are an opt-in for that package.
        for name in list(stage_owned):
            if name in explicit_set:
                del stage_owned[name]
        if stage_owned:
            by_stage: dict[str, list[str]] = {}
            for name, stage in stage_owned.items():
                by_stage.setdefault(stage, []).append(name)
            for stage, names in by_stage.items():
                _log.info(
                    f"skipping {len(names)} {stage}-stage package(s): "
                    f"{', '.join(sorted(names))} — run `sysforge run {stage}` "
                    "to update (or pass --include-stage-owned)"
                )
            target_names -= set(stage_owned)

    packages: dict[str, dict] = {}
    unrecorded_names: set[str] = set()

    def _resolve_source(name: str, override: dict, bs_entry: dict | None) -> str | None:
        """build_state > override > pacman-foreign inference (non-foreign → repo).

        build_state is consulted first so a previously-built package keeps
        its recorded origin across runs (no flipping if the override is
        removed or pacman reclassifies). Override and inference are the
        fallback for packages with no build record yet.
        """
        if bs_entry is not None:
            bs_source = bs_entry.get("source")
            if bs_source:
                return bs_source
        ov = override.get("source")
        if ov:
            return ov
        if name not in foreign:
            # Non-foreign installed package routed through sysforge update —
            # must come from a repo, so pkgctl is the sync path.
            return "repo"
        return None

    def _resolve_repo_class(name: str, source: str | None) -> str | None:
        """Sub-classify repo-source packages: "source" vs "pacman".

        Only meaningful when ``source == "repo"``. Returns:
          - ``"source"`` if the package has a behavior-changing override
            (``pkgbuild_patch`` / ``cache`` / ``reason``) — it goes through
            pkgctl-clone + makepkg, same as before.
          - ``"pacman"`` if it has no override and is in scope only because
            ``repo_mode = "profiled"`` is set. These skip source sync and
            get version-checked via the batched ``checkupdates`` call;
            upgrades are deferred to a single ``sudo pacman -Syu`` at the
            end of the update.
          - ``None`` for non-repo sources (aur/git) — those follow the
            existing path.
        """
        if source != "repo":
            return None
        if name in behavior_overridden:
            return "source"
        return "pacman"

    for name in target_names:
        override = overrides_by_name.get(name, {})
        bs_entry = build_state_pkgs.get(name)
        resolved_source = _resolve_source(name, override, bs_entry)
        resolved_repo_class = _resolve_repo_class(name, resolved_source)

        if bs_entry is not None and bs_entry.get("build_mode", "profiled") != "pacman":
            pkg = dict(bs_entry)
            if resolved_source and "source" not in pkg:
                pkg["source"] = resolved_source
            if resolved_repo_class:
                pkg["repo_class"] = resolved_repo_class
            packages[name] = pkg
        else:
            unrecorded_names.add(name)
            pkgdir = str(pkgbuild_src_dir_base / name) if pkgbuild_src_dir_base else ""
            entry: dict = {
                "pkgbase": name,
                "pkgbuild_dir": pkgdir,
            }
            if resolved_source:
                entry["source"] = resolved_source
            if resolved_repo_class:
                entry["repo_class"] = resolved_repo_class
            packages[name] = entry

    # Resolve pkgbase for unrecorded packages from pacman's local DB first.
    # Works offline for any installed package (repo or foreign) — including
    # custom-built split packages that aren't in AUR (e.g. linux-custom-headers
    # → pkgbase linux-custom). Falls through to AUR RPC below for entries
    # where %BASE% wasn't recorded.
    if unrecorded_names and pkgbuild_src_dir_base:
        for name in unrecorded_names:
            real_base = get_pkgbase(name)
            if real_base and real_base != name:
                packages[name]["pkgbase"] = real_base
                packages[name]["pkgbuild_dir"] = str(pkgbuild_src_dir_base / real_base)

    # AUR RPC fallback for unrecorded packages whose pkgbase still equals their
    # pkgname (local DB had no %BASE% — older pacman or stripped metadata).
    offline = getattr(args, "offline", False)
    if unrecorded_names and pkgbuild_src_dir_base and not offline:
        aur_unrecorded = [n for n in unrecorded_names
                          if packages[n].get("source") != "repo"
                          and packages[n].get("pkgbase") == n]
        if aur_unrecorded:
            aur_results = aur_info(aur_unrecorded)
            for name in aur_unrecorded:
                info = aur_results.get(name)
                if info and info.get("PackageBase") and info["PackageBase"] != name:
                    real_base = info["PackageBase"]
                    packages[name]["pkgbase"] = real_base
                    packages[name]["pkgbuild_dir"] = str(pkgbuild_src_dir_base / real_base)

    # Filter to specific packages when names are given on the command line
    filter_names: list[str] = getattr(args, "pkgnames", None) or []
    if filter_names:
        unknown = [n for n in filter_names if n not in packages]
        if unknown:
            for name in unknown:
                _log.warn(f"{name}: not in update scope (not installed, or repo package without an override) — skipping")
        filter_set = set(filter_names)
        packages = {k: v for k, v in packages.items() if k in filter_set}

    return packages, unrecorded_names


# ---------------------------------------------------------------------------
# Phase 2: Source sync (pull + clone + cleansrc + recovery)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase 3: Version check (called from ThreadPoolExecutor)
# ---------------------------------------------------------------------------

_UNRESOLVED_EXPANSION = re.compile(r"[${}]")


def _check_one_pkgbase(
    pkgbase: str,
    pkgnames: list[str],
    entry: dict,
    sync_failures: dict[str, tuple[str, str]],
    all_installed: dict[str, str],
    unrecorded_names: set[str],
    skip_sync_check: bool,
    rpc_version_by_base: dict[str, str],
    force_devel: bool = False,
    built_upstream_commit: str | None = None,
    pacman_updates_map: dict[str, str] | None = None,
) -> _UpdateResult | None:
    """Check a single pkgbase and return an _UpdateResult, or None on skip.

    Pacman-class repo packages (``entry["repo_class"] == "pacman"``) take a
    fast path: no PKGBUILD parse, no clone — installed-vs-checkupdates
    vercmp only. The slow PKGBUILD-parse path below runs for AUR/git and
    override-tagged repo packages.
    """
    has_record = not any(pn in unrecorded_names for pn in pkgnames)
    source = entry.get("source")

    # Pacman fast-path: checkupdates already told us if a repo upgrade is
    # pending. The pkgbuild_dir doesn't need to exist (we never source-build
    # this class), so this branch precedes the directory existence check.
    if entry.get("repo_class") == "pacman":
        installed_ver: str | None = None
        for pn in pkgnames:
            ver = all_installed.get(pn)
            if ver is not None:
                installed_ver = ver
                break
        if installed_ver is None:
            return None
        if pacman_updates_map is None:
            # checkupdates unavailable; can't decide. Surface once, skip
            # action so the package shows up in the summary as deferred.
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames,
                action="SKIPPED_NO_CHECKUPDATES",
                installed_ver=installed_ver, pkgbuild_ver=None,
                pkgbuild_path=None, has_build_record=has_record, source=source,
            )
        # checkupdates lists each pkgname (not pkgbase) that needs upgrade.
        # Pick the newest mapped version across this pkgbase's pkgnames.
        new_ver: str | None = None
        for pn in pkgnames:
            mapped = pacman_updates_map.get(pn)
            if mapped is None:
                continue
            if new_ver is None or vercmp(mapped, new_ver) > 0:
                new_ver = mapped
        if new_ver is None:
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="UP_TO_DATE",
                installed_ver=installed_ver, pkgbuild_ver=installed_ver,
                pkgbuild_path=None, has_build_record=has_record, source=source,
            )
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="NEEDS_PACMAN_UPGRADE",
            installed_ver=installed_ver, pkgbuild_ver=new_ver,
            pkgbuild_path=None, has_build_record=has_record, source=source,
        )

    # VCS fast-path: without ``--devel`` we never resolve or rebuild VCS
    # packages, so the PKGBUILD parse, the pkgbuild_dir existence probe, and
    # the sync-failure log are all wasted work. Return DEVEL straight from
    # the installed version. Mirrors the source-sync filter in
    # ``_sync_sources`` — both edges of the build pipeline ignore VCS dirs
    # entirely when ``--devel`` is absent.
    if _is_vcs(pkgbase) and not force_devel:
        devel_installed_ver = next(
            (all_installed[pn] for pn in pkgnames if pn in all_installed), None,
        )
        if devel_installed_ver is None:
            return None
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL",
            installed_ver=devel_installed_ver, pkgbuild_ver=None,
            pkgbuild_path=None, has_build_record=has_record, source=source,
        )

    pkgbuild_dir = Path(entry["pkgbuild_dir"])

    if not pkgbuild_dir.is_dir():
        _log.warn(f"{pkgbase}: pkgbuild_dir {pkgbuild_dir} not found — skipping")
        return None

    pkgbuild_path = pkgbuild_dir / "PKGBUILD"
    if not pkgbuild_path.exists():
        _log.warn(f"{pkgbase}: PKGBUILD not found at {pkgbuild_path} — skipping")
        return None

    if not skip_sync_check and pkgbase in sync_failures:
        status, msg = sync_failures[pkgbase]
        _log.error(msg)
        action = _SYNC_STATUS_TO_ACTION.get(status, "PULL_FAILED")
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action=action,
            installed_ver=None, pkgbuild_ver=None, pkgbuild_path=pkgbuild_path,
            has_build_record=has_record, source=source,
        )

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        _log.warn(f"{pkgbase}: failed to parse PKGBUILD: {e} — skipping")
        return None

    globals_ = pkgmeta.get("globals", {})
    pkgbuild_ver = format_version(globals_)

    # Static PKGBUILD parser can't evaluate bash parameter expansion
    # (${var//-/_}, ${var/[a-z]/.sfx}, etc.). When pkgver still contains
    # shell metacharacters, fall back to the AUR RPC version we already
    # cached in source_meta.toml — it's the authoritative released version
    # and is vercmp-ready (already includes pkgrel and any epoch prefix).
    if _UNRESOLVED_EXPANSION.search(pkgbuild_ver):
        rpc_ver = rpc_version_by_base.get(pkgbase)
        if rpc_ver:
            pkgbuild_ver = rpc_ver
        else:
            _log.warn(
                f"{pkgbase}: pkgver '{pkgbuild_ver}' has unresolved shell "
                "expansion and no cached RPC version — skipping"
            )
            return None

    # Live-install-set iteration guarantees every pkgbase reaching here has
    # at least one installed sub-package; pick that version for vercmp.
    installed_ver: str | None = None
    for pn in pkgnames:
        ver = all_installed.get(pn)
        if ver is not None:
            installed_ver = ver
            break
    assert installed_ver is not None, f"{pkgbase}: no installed pkgname in {pkgnames}"

    # VCS packages under --devel: static pkgver is just the seed; the real
    # version comes from running pkgver() against the fetched upstream
    # sources. The --devel-off case short-circuits at the top of this
    # function (no PKGBUILD parse, no source sync) — anything reaching this
    # branch is opt-in --devel work.
    if _is_vcs(pkgbase):
        # Cheap short-circuit: if the upstream HEAD still matches the SHA we
        # built last time (recorded in build_state.toml), skip the full
        # ``makepkg -od --nobuild`` resolve. peek_upstream_commit returns
        # None for multi-git-source / unparseable PKGBUILDs / network errors,
        # and we fall through to the canonical path in that case.
        if built_upstream_commit is not None:
            current_commit = peek_upstream_commit(pkgbuild_dir)
            if current_commit is not None and current_commit == built_upstream_commit:
                return _UpdateResult(
                    pkgbase=pkgbase, pkgnames=pkgnames, action="UP_TO_DATE",
                    installed_ver=installed_ver, pkgbuild_ver=installed_ver,
                    pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                    source=source,
                )

        resolved = evaluate_vcs_pkgver(pkgbuild_dir)
        if resolved is None:
            _log.warn(
                f"{pkgbase}: pkgver() evaluation failed — skipping rebuild "
                "(re-run --devel after the upstream/network issue clears)"
            )
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL_EVAL_FAILED",
                installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
                pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                source=source,
            )

        try:
            cmp = vercmp(resolved, installed_ver)
        except RuntimeError as e:
            _log.warn(f"{pkgbase}: vercmp failed on resolved {resolved!r}: {e} — skipping")
            return _UpdateResult(
                pkgbase=pkgbase, pkgnames=pkgnames, action="DEVEL_EVAL_FAILED",
                installed_ver=installed_ver, pkgbuild_ver=resolved,
                pkgbuild_path=pkgbuild_path, has_build_record=has_record,
                source=source,
            )

        if cmp > 0:
            action = "NEEDS_REBUILD"
        elif cmp == 0:
            action = "UP_TO_DATE"
        else:
            action = "DOWNGRADE"
            _log.warn(f"{pkgbase}: resolved {resolved} is older than installed {installed_ver}")
        return _UpdateResult(
            pkgbase=pkgbase, pkgnames=pkgnames, action=action,
            installed_ver=installed_ver, pkgbuild_ver=resolved,
            pkgbuild_path=pkgbuild_path, has_build_record=has_record,
            source=source,
        )

    try:
        cmp = vercmp(pkgbuild_ver, installed_ver)
    except RuntimeError as e:
        _log.warn(f"{pkgbase}: version comparison failed: {e} — skipping")
        return None

    # Drift observability: when the installed version matches what sysforge
    # built last time but the on-disk PKGBUILD now describes a different
    # version, surface that upstream PKGBUILD has moved. The action is still
    # whatever vercmp says — this is purely informational (-v only).
    bs_pkgver = entry.get("pkgver")
    bs_pkgrel = entry.get("pkgrel")
    bs_epoch = entry.get("epoch", "0")
    if bs_pkgver is not None and bs_pkgrel is not None:
        bs_ver = f"{bs_epoch}:{bs_pkgver}-{bs_pkgrel}" if bs_epoch and bs_epoch != "0" \
            else f"{bs_pkgver}-{bs_pkgrel}"
        if installed_ver == bs_ver and pkgbuild_ver != bs_ver:
            _log.info(
                f"{pkgbase}: PKGBUILD on disk ({pkgbuild_ver}) differs from "
                f"last built ({bs_ver}) — upstream PKGBUILD has moved"
            )

    if cmp > 0:
        action = "NEEDS_REBUILD"
    elif cmp == 0:
        action = "UP_TO_DATE"
    else:
        action = "DOWNGRADE"
        _log.warn(f"{pkgbase}: PKGBUILD {pkgbuild_ver} is older than installed {installed_ver}")

    return _UpdateResult(
        pkgbase=pkgbase, pkgnames=pkgnames, action=action,
        installed_ver=installed_ver, pkgbuild_ver=pkgbuild_ver,
        pkgbuild_path=pkgbuild_path, has_build_record=has_record,
        source=source,
    )


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


def _consume_pacman_hook_sentinels(silent: bool = False) -> None:
    """Surface kernel/toolchain reminders dropped by pacman PostTransaction
    hooks since the last `sysforge update` run, then unlink them.

    The buildstate sentinel is consumed silently — its only purpose is to
    nudge the build_state.toml resync that already runs in cmd_update.

    silent=True suppresses the kernel/toolchain warnings but still unlinks.
    Used at the end of cmd_update so sentinels dropped by sysforge's own
    Phase 5 (pacman -U) and Phase 6.5 (pacman -Syu) transactions don't
    re-fire as "stale" reminders on the next invocation.
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
    buildstate = _SENTINEL_DIR / "buildstate"
    if buildstate.exists():
        try:
            buildstate.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_update(args) -> None:
    """Entry point for `sysforge update`."""

    _consume_pacman_hook_sentinels()
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


def _cmd_update_body(args) -> None:
    # ── Phase 0: Init ─────────────────────────────────────────────────────
    install_only = getattr(args, "install_only", False)
    offline = getattr(args, "offline", False) or install_only
    if install_only:
        args.offline = True
    if not offline:
        fetch_aur_name_cache()

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    # Active toolchain variant — stamped onto every rebuild via BuildOptions
    # and used below to surface drift between installed packages' recorded
    # variant and what's active now. ``"system"`` means the toolchain stage
    # has never run on this state dir; treat as a benign no-op (no stamp).
    active_variant = get_toolchain_variant(PipelineState(state_dir))

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

    # ── Phase 1: Package set assembly ─────────────────────────────────────
    packages, unrecorded_names = _assemble_package_set(
        args, bs, config, build_cfg, overrides_by_name,
    )

    if not packages:
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
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                results.append(result)

    results.sort(key=lambda r: r.pkgbase)

    # ── Phase 4: Summary + dry-run gate ───────────────────────────────────
    _print_summary(results, args)

    # ── Phase 4.25: Toolchain-variant drift ───────────────────────────────
    # Compare each result's recorded toolchain_variant (from build_state)
    # against the active toolchain. Drift means "the installed binary was
    # produced under a different compiler identity than is active now".
    # Pure pacman-mode entries have no variant and are never drift candidates.
    # Surface as a one-line summary (always) and a full list under
    # --explain-drift; opt-in rebuild via --rebuild-on-toolchain-drift.
    drifted: list[tuple[str, str]] = []  # (pkgbase, recorded_variant)
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
                if rec_variant and rec_variant != active_variant:
                    drifted.append((r.pkgbase, rec_variant))
                if rec_variant is not None:
                    break

    if drifted:
        sample = ", ".join(f"{pb} ({rv})" for pb, rv in drifted[:3])
        more = f" (+{len(drifted) - 3} more)" if len(drifted) > 3 else ""
        _log.ui(
            f"toolchain drift: {len(drifted)} package(s) built under a "
            f"different variant than active ({active_variant}): {sample}{more}. "
            "Pass --rebuild-on-toolchain-drift to rebuild, or "
            "--explain-drift to list."
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
                f"different toolchain variant than active "
                f"({active_variant}):"
            )
            for pkgbase, rec_variant in sorted(drifted):
                print(f"  {pkgbase:<40}  recorded={rec_variant}")
        return

    if getattr(args, "dry_run", False):
        return

    if getattr(args, "rebuild_on_toolchain_drift", False) and drifted:
        drifted_bases = {pb for pb, _ in drifted}
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
        print("[SYSFORGE] Nothing to rebuild.")
        return

    pkgdest = get_pkgdest()
    built_pkg_files: list = []
    built_pkgs: list[str] = []
    failed_pkgs: list[str] = []
    pgo_skipped_pkgs: list[str] = []
    install_failed = False

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
        from sysforge.ui import progress as _ui_progress
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
            persist_log=getattr(args, "persist_log", False),
            log_dir=Path(args.log_dir) if getattr(args, "log_dir", None) else None,
            cache_report=getattr(args, "cache_report", False),
            extra_flags=(
                expand_makepkg_flags(args.makepkg)
                if getattr(args, "makepkg", None) else None
            ),
            active_variant=active_variant,
            pkgdest=pkgdest,
        )
        built_pkgs = outcome.built_pkgs
        failed_pkgs = outcome.failed_pkgs
        pgo_skipped_pkgs = outcome.pgo_skipped_pkgs
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
        rc = _subprocess.run(cmd).returncode
        if rc != 0:
            _log.error(f"pacman -Syu exited {rc}")
            pacman_upgrade_failed = True

    # (The build cache-probe session report is emitted inside
    # build_core.build_and_install when --cache-report is set.)

    # Sync failures from cleansrc refusals count as build failures.
    failed_pkgs.extend(sorted(cleansrc_failures))

    skipped = len(results) - len(to_build) - len(pending_pacman_upgrade)
    if install_only:
        skipped += len(to_build) - len(built_pkgs) - len(failed_pkgs)
    built_label = "installed" if install_only else "built"
    _log.ui((
        f"\n[SYSFORGE] Update complete: "
        f"{len(built_pkgs)} {built_label}, {len(failed_pkgs)} failed, {skipped} skipped"
        + (f", {len(pgo_skipped_pkgs)} pgo-skipped" if pgo_skipped_pkgs else "")
        + (f", {len(pacman_upgrade_pkgs)} pacman-upgraded" if pacman_upgrade_pkgs else "")
        + (" (pacman -Syu FAILED)" if pacman_upgrade_failed else "")
        + "."
    ))
    if built_pkgs:
        _label = "Installed:" if install_only else "Built:"
        _log.ui(f"  {_label:<13}{' '.join(built_pkgs)}")
    if pacman_upgrade_pkgs:
        suffix = " (transaction FAILED)" if pacman_upgrade_failed else ""
        _log.ui(f"  Pacman-Syu:  {' '.join(pacman_upgrade_pkgs)}{suffix}")
    if failed_pkgs:
        _log.ui(f"  Failed:      {' '.join(failed_pkgs)}")
    if cleansrc_failures:
        _log.ui(
            f"  --cleansrc refused {len(cleansrc_failures)} package(s) with local work; "
            "commit/push or resolve manually before retrying."
        )
    if pgo_skipped_pkgs:
        _log.ui(
            f"  PGO-skipped: {' '.join(pgo_skipped_pkgs)}"
            " (run 'sysforge run toolchain' to rebuild profdata)"
        )

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
# Summary display
# ---------------------------------------------------------------------------

# (tag, count_label, line_template) per action. line_template is formatted
# with the _UpdateResult fields plus a trailing {star} ("" or " *"). Order
# here is the order each action appears in summary header + per-package
# section.
_ACTION_FORMATS: dict[str, tuple[str, str, str]] = {
    "NEEDS_REBUILD":     ("NEEDS_REBUILD",     "need rebuild",
                          "{pkgbase}: {installed_ver} → {pkgbuild_ver}{star}"),
    "NEEDS_PACMAN_UPGRADE": ("NEEDS_PACMAN", "need pacman upgrade",
                          "{pkgbase}: {installed_ver} → {pkgbuild_ver} (pacman -Syu){star}"),
    "UP_TO_DATE":        ("UP_TO_DATE",        "up to date",
                          "{pkgbase}: {pkgbuild_ver}{star}"),
    "DEVEL":             ("DEVEL",             "devel",
                          "{pkgbase}: skipped (use --devel to rebuild){star}"),
    "DEVEL_EVAL_FAILED": ("DEVEL_EVAL_FAILED", "devel-eval-failed",
                          "{pkgbase}: pkgver() resolution failed (skipped){star}"),
    "DOWNGRADE":         ("DOWNGRADE",         "downgrade",
                          "{pkgbase}: installed {installed_ver} > pkgbuild {pkgbuild_ver} (skipped){star}"),
    "PULL_FAILED":       ("PULL_FAILED",       "pull failed",
                          "{pkgbase}: git pull failed (skipped){star}"),
    "RATE_LIMITED":      ("RATE_LIMITED",      "rate-limited",
                          "{pkgbase}: AUR rate-limited (skipped, retry later){star}"),
    "PURGE_REFUSED":     ("PURGE_REFUSED",     "purge refused",
                          "{pkgbase}: --cleansrc refused (local work present, skipped){star}"),
    "SKIPPED_NO_CHECKUPDATES": ("NO_CHECKUPDATES", "skipped (no checkupdates)",
                          "{pkgbase}: checkupdates unavailable, install pacman-contrib{star}"),
}

# Actions that are always printed per-package regardless of verbosity.
# Everything else only appears under -v / verbose mode.
_ALWAYS_VERBOSE_ACTIONS = frozenset({
    "NEEDS_REBUILD", "NEEDS_PACMAN_UPGRADE", "DOWNGRADE",
})


def _print_summary(results: list[_UpdateResult], args) -> None:
    if not results:
        print("[SYSFORGE] No packages to check.")
        return

    verbose = bool(getattr(args, "verbose", 0))

    # Totals header
    counts: dict[str, int] = {}
    no_record_count = 0
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
        if not r.has_build_record:
            no_record_count += 1

    parts = [f"{len(results)} packages"]
    for action, (_tag, label, _tmpl) in _ACTION_FORMATS.items():
        n = counts.get(action, 0)
        if n:
            parts.append(f"{n} {label}")
    if no_record_count:
        parts.append(f"{no_record_count} no build record")

    print(f"\n  Checking {', '.join(parts)}")
    print()

    for r in results:
        if not verbose and r.action not in _ALWAYS_VERBOSE_ACTIONS:
            continue
        fmt = _ACTION_FORMATS.get(r.action)
        if fmt is None:
            continue
        tag, _label, tmpl = fmt
        star = " *" if not r.has_build_record else ""
        line = tmpl.format(
            pkgbase=r.pkgbase,
            installed_ver=r.installed_ver,
            pkgbuild_ver=r.pkgbuild_ver,
            star=star,
        )
        print(f"  [{tag}]{' ' * max(1, 17 - len(tag) - 2)}{line}")

    if not verbose and any(r.action not in _ALWAYS_VERBOSE_ACTIONS for r in results):
        print("  (run with -v to list each skipped/up-to-date package)")
    if no_record_count:
        print("\n  * = no build record")
    print()


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
