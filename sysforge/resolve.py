"""
resolve.py — sysforge resolve subcommand

Inspects rule matching and profile resolution for a given PKGBUILD without
building it. Useful for debugging why a package received a particular profile
or flag set.

Input modes:
  - Path to a PKGBUILD file (explicit): sysforge resolve ~/builds/htop/PKGBUILD
  - Bare package name: looks for <cwd>/<name>/PKGBUILD

Output goes to stdout. Verbosity flags (-vv, -vvv) enable the normal
[PROFILE] / [GROUPS] log lines on stderr alongside the resolve summary.

Public API:
    cmd_resolve(args)
"""
import sys
from pathlib import Path

from sysforge.primitives.config import (
    find_pkgbuild,
    load_config,
    load_conflict_groups,
    load_consumes_inference,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.profile import (
    SYSFORGE_KEYS,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _get_profile_chain(profile_name: str, profiles: dict) -> list[str]:
    """Walk the extends chain and return it root-last."""
    chain: list[str] = []
    visited: set[str] = set()
    name: str | None = profile_name
    while name and name not in visited and name in profiles:
        visited.add(name)
        chain.append(name)
        name = profiles.get(name, {}).get("extends")
    return chain


def _find_winner(matched_rules: list[dict]) -> dict | None:
    """Return the highest-priority rule that specifies a profile, or None."""
    winner = None
    for rule in matched_rules:
        if "profile" not in rule:
            continue
        if winner is None or rule.get("priority", 0) > winner.get("priority", 0):
            winner = rule
    return winner


def _format_conditions(rule: dict) -> str:
    """Format a rule's match conditions as a compact single-line string."""
    CONDITION_KEYS = (
        "pkgnames", "groups", "depends", "makedepends",
        "not_pkgnames", "not_groups", "not_depends", "not_makedepends",
        "depends_any", "makedepends_any", "depends_all", "makedepends_all",
    )
    parts = [f"{k}={rule[k]!r}" for k in CONDITION_KEYS if k in rule]
    return ", ".join(parts) if parts else "(no conditions — always matches)"


def _print_resolve(
    pkgbuild_path: Path,
    pkgmeta: dict,
    matched_rules: list[dict],
    resolved_profile: dict,
    consumes: frozenset,
    groups: list[str],
    config: dict,
    show_flags: bool,
) -> None:
    """Print the resolve summary to stdout."""
    profiles = config.get("profiles", {})
    globals_ = pkgmeta.get("globals", {})

    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "(unknown)")
    if isinstance(pkgname, list):
        pkgname = pkgname[0] if pkgname else "(unknown)"

    winner = _find_winner(matched_rules)
    winning_profile = (
        winner["profile"] if winner
        else config.get("defaults", {}).get("profile", "bare")
    )
    chain = _get_profile_chain(winning_profile, profiles)

    W = 16  # label column width

    print(f"{'Package:':<{W}} {pkgname}")
    print(f"{'PKGBUILD:':<{W}} {pkgbuild_path}")
    print()

    # Matched rules
    if matched_rules:
        print("Matched rules:")
        for rule in sorted(matched_rules, key=lambda r: -r.get("priority", 0)):
            pri = rule.get("priority", 0)
            profile = rule.get("profile", "(no profile)")
            conditions = _format_conditions(rule)
            marker = "  ← winner" if rule is winner else ""
            print(f"  [priority {pri:>3}]  {conditions}  →  {profile}{marker}")
    else:
        default_profile = config.get("defaults", {}).get("profile", "bare")
        print(f"Matched rules:   (none — using default profile: {default_profile!r})")

    print()
    print(f"{'Profile chain:':<{W}} {' → '.join(chain)}")

    build_mode = resolved_profile.get("build_mode")
    if build_mode:
        print(f"{'Build mode:':<{W}} {build_mode}")

    print(f"{'Consumes:':<{W}} {', '.join(sorted(consumes)) if consumes else '(none)'}")
    print(f"{'Groups:':<{W}} {', '.join(groups) if groups else '(none)'}")

    if show_flags:
        print()
        print("Resolved profile:")
        normal = {k: v for k, v in resolved_profile.items() if k not in SYSFORGE_KEYS}
        internal = {k: v for k, v in resolved_profile.items() if k in SYSFORGE_KEYS}
        for k, v in sorted(normal.items()):
            print(f"  {k:<26} = {v!r}")
        if internal:
            print()
            print("  # sysforge-internal keys:")
            for k, v in sorted(internal.items()):
                print(f"  {k:<26} = {v!r}")
    else:
        flag_count = len([k for k in resolved_profile if k not in SYSFORGE_KEYS])
        print(f"\n  ({flag_count} flag key(s) — use --show-flags to expand)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _print_deps(pkgbuild_path: Path, pkgmeta: dict, deps: list) -> None:
    """Print the dependency tree for --deps mode."""
    globals_ = pkgmeta.get("globals", {})
    pkgname = globals_.get("pkgbase") or globals_.get("pkgname", "(unknown)")
    if isinstance(pkgname, list):
        pkgname = pkgname[0] if pkgname else "(unknown)"

    print(f"Package:   {pkgname}")
    print(f"PKGBUILD:  {pkgbuild_path}")
    print()

    aur_deps = [d for d in deps if d.source == "aur"]
    repo_deps = [d for d in deps if d.source == "repo"]
    installed = [d for d in deps if d.source == "installed"]
    unknown = [d for d in deps if d.source == "unknown"]

    if aur_deps:
        print(f"AUR dependencies ({len(aur_deps)}) — build order:")
        for i, dep in enumerate(aur_deps, 1):
            req = ", ".join(dep.required_by)
            path_str = f"  {dep.pkgbuild_path}" if dep.pkgbuild_path else ""
            print(f"  {i:>3}. {dep.name}  (depth {dep.depth}, required by {req}){path_str}")
    else:
        print("AUR dependencies: (none)")

    if repo_deps:
        print(f"\nRepo dependencies ({len(repo_deps)}) — installed via pacman:")
        for dep in sorted(repo_deps, key=lambda d: d.name):
            req = ", ".join(dep.required_by)
            print(f"       {dep.name}  (required by {req})")

    if unknown:
        print(f"\nUnresolved ({len(unknown)}) — not found in repos or AUR:")
        for dep in unknown:
            req = ", ".join(dep.required_by)
            print(f"    !  {dep.name}  (required by {req})")

    print(f"\nSummary: {len(aur_deps)} AUR | {len(repo_deps)} repo | "
          f"{len(installed)} installed | {len(unknown)} unknown")


def cmd_resolve(args) -> None:
    """Entry point for sysforge resolve."""
    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths)

    try:
        pkgbuild_path = find_pkgbuild(args.pkg, config)
    except FileNotFoundError as e:
        print(f"[SYSFORGE] Error: {e}", file=sys.stderr)
        print(f"  Tip: run `sysforge build {args.pkg}` to download the PKGBUILD first.", file=sys.stderr)
        sys.exit(1)

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        print(f"[SYSFORGE] Error parsing PKGBUILD: {e}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "deps", False):
        from sysforge.primitives.aur_resolve import resolve_all_deps
        deps = resolve_all_deps(pkgbuild_path, config, fetch=False)
        _print_deps(pkgbuild_path, pkgmeta, deps)
        return

    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    matched = match_rules(pkgmeta, config.get("rules", []))
    resolved = resolve_profile(pkgmeta, matched, config, conflict_groups)
    consumes = resolve_consumes(resolved, pkgmeta, inference_map)
    groups = resolve_groups(pkgmeta, matched, config.get("defaults", {}))

    _print_resolve(
        pkgbuild_path, pkgmeta, matched, resolved,
        consumes, groups, config, show_flags=args.show_flags,
    )
