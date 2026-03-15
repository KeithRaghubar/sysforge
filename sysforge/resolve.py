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
    load_config,
    load_conflict_groups,
    load_consumes_inference,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.profile import (
    _SYSFORGE_KEYS,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# PKGBUILD lookup
# ---------------------------------------------------------------------------

def _find_pkgbuild(pkg: str) -> Path:
    """
    Resolve a PKGBUILD path from the pkg argument.

    - If pkg is an existing file path → use it directly.
    - If pkg is a bare name → try <cwd>/<name>/PKGBUILD.
    - Otherwise raise FileNotFoundError with a helpful message.
    """
    p = Path(pkg)

    if p.exists():
        return p.resolve()

    candidate = Path.cwd() / pkg / "PKGBUILD"
    if candidate.exists():
        return candidate.resolve()

    raise FileNotFoundError(
        f"PKGBUILD not found for {pkg!r}.\n"
        f"  Searched:\n"
        f"    {p}\n"
        f"    {candidate}\n"
        f"  Pass a full path to a PKGBUILD file, or cd into the directory\n"
        f"  containing the package and run: sysforge resolve <name>"
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
        normal = {k: v for k, v in resolved_profile.items() if k not in _SYSFORGE_KEYS}
        internal = {k: v for k, v in resolved_profile.items() if k in _SYSFORGE_KEYS}
        for k, v in sorted(normal.items()):
            print(f"  {k:<26} = {v!r}")
        if internal:
            print()
            print("  # sysforge-internal keys:")
            for k, v in sorted(internal.items()):
                print(f"  {k:<26} = {v!r}")
    else:
        flag_count = len([k for k in resolved_profile if k not in _SYSFORGE_KEYS])
        print(f"\n  ({flag_count} flag key(s) — use --show-flags to expand)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def cmd_resolve(args) -> None:
    """Entry point for sysforge resolve."""
    try:
        pkgbuild_path = _find_pkgbuild(args.pkg)
    except FileNotFoundError as e:
        print(f"[SYSFORGE] Error: {e}", file=sys.stderr)
        sys.exit(1)

    config_paths = [Path(args.profile_conf)] if getattr(args, "profile_conf", None) else None
    config = load_config(config_paths=config_paths)
    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        print(f"[SYSFORGE] Error parsing PKGBUILD: {e}", file=sys.stderr)
        sys.exit(1)

    matched = match_rules(pkgmeta, config.get("rules", []))
    resolved = resolve_profile(pkgmeta, matched, config, conflict_groups)
    consumes = resolve_consumes(resolved, pkgmeta, inference_map)
    groups = resolve_groups(pkgmeta, matched, config.get("defaults", {}))

    _print_resolve(
        pkgbuild_path, pkgmeta, matched, resolved,
        consumes, groups, config, show_flags=args.show_flags,
    )
