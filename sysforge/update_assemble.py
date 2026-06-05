"""Phase 1 of ``update``: package-set assembly.

``_assemble_package_set`` turns the live install set into the unified
``{pkgname: entry}`` dict the rest of the update pipeline iterates. It owns
the scope rules (which installed packages are in play), the stage-ownership
skip, source/repo-class resolution, and pkgbase backfill — but it takes the
already-loaded ``BuildState`` / config / overrides as arguments, so it stays
free of config I/O and of the orchestrator's other phases.

``update.py`` re-exports ``_assemble_package_set`` so existing
``from sysforge.update import _assemble_package_set`` call sites and direct
tests are unchanged.
"""

from pathlib import Path

from sysforge import log
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.pacman import (
    get_all_installed_packages,
    get_foreign_packages,
    get_pkgbase,
)
from sysforge.primitives.aur import aur_info
from sysforge.primitives.stage_ownership import load_stage_ownership
from sysforge.packages_cmd import entry_is_inert

_log = log.get_logger("UPDATE")


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
