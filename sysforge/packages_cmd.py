"""
packages_cmd.py — packages.toml lifecycle management

Implements the `sysforge packages` subcommand namespace:
    list    — show packages in packages.toml
    add     — classify a package, infer pkgbuild_patch, append entry
    remove  — remove a package entry
    sync    — re-validate inferable fields (source, pkgbuild_patch)

Public API:
    cmd_packages_list(args)
    cmd_packages_add(args)
    cmd_packages_remove(args)
    cmd_packages_sync(args)
"""
import sys
import tomllib
from pathlib import Path

import sysforge.log as _log
from sysforge.primitives.config import load_config


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_packages_file(args_packages: str | None) -> Path:
    """Resolve packages.toml path from arg, config, or default."""
    if args_packages:
        return Path(args_packages)
    config = load_config() or {}
    raw = config.get("packages_file")
    if raw:
        return Path(raw).expanduser()
    return Path("/etc/sysforge/packages.toml")


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _entry_toml_block(entry: dict) -> str:
    """Serialise a package entry dict to a TOML [[package]] block string."""
    lines = ["[[package]]", f'name = "{entry["name"]}"', f'source = "{entry["source"]}"']
    for field in ("profile", "pkgbuild_patch", "cache", "requires_hardware"):
        if field not in entry:
            continue
        val = entry[field]
        if isinstance(val, bool):
            lines.append(f"{field} = {'true' if val else 'false'}")
        elif isinstance(val, str):
            lines.append(f'{field} = "{val}"')
        else:
            lines.append(f"{field} = {val!r}")
    return "\n".join(lines)


def _write_packages_toml(path: Path, build: dict, entries: list[dict]) -> None:
    """Atomically rewrite packages.toml from a build section and entry list."""
    lines = ["# Managed by sysforge packages", ""]
    if build:
        lines.append("[build]")
        for k, v in build.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f"{k} = {v!r}")
        lines.append("")
    for entry in entries:
        lines.append(_entry_toml_block(entry))
        lines.append("")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    tmp.rename(path)


# ---------------------------------------------------------------------------
# packages list
# ---------------------------------------------------------------------------

def cmd_packages_list(args):
    path = _resolve_packages_file(getattr(args, "packages", None))
    if not path.exists():
        print(f"[SYSFORGE] No packages.toml at {path}", file=sys.stderr)
        sys.exit(1)

    data = _load_toml(path)
    entries = data.get("package", [])
    if not entries:
        print(f"No packages defined in {path}")
        return

    max_name = max(len(e.get("name", "")) for e in entries)
    max_src = max(len(e.get("source", "")) for e in entries)

    header = f"  {'NAME':<{max_name}}  {'SOURCE':<{max_src}}  FLAGS"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for e in entries:
        name = e.get("name", "")
        source = e.get("source", "")
        flags = []
        if e.get("pkgbuild_patch"):
            flags.append("pkgbuild_patch")
        if e.get("cache") is False:
            flags.append("cache=false")
        if e.get("requires_hardware"):
            flags.append(f"requires_hardware={e['requires_hardware']}")
        if e.get("profile"):
            flags.append(f"profile={e['profile']}")
        flag_str = ", ".join(flags)
        print(f"  {name:<{max_name}}  {source:<{max_src}}  {flag_str}")


# ---------------------------------------------------------------------------
# packages add
# ---------------------------------------------------------------------------

def cmd_packages_add(args):
    pkg = args.pkg
    path = _resolve_packages_file(getattr(args, "packages", None))

    # Check for duplicate
    existing_entries: list[dict] = []
    build_section: dict = {}
    if path.exists():
        data = _load_toml(path)
        existing_entries = data.get("package", [])
        build_section = data.get("build", {})
        if any(e.get("name") == pkg for e in existing_entries):
            print(f"[SYSFORGE] {pkg} is already in {path}", file=sys.stderr)
            sys.exit(1)

    # Classify
    from sysforge.primitives.aur import is_repo_package, aur_info
    if is_repo_package(pkg):
        source = "repo"
    else:
        found = aur_info([pkg])
        if pkg in found:
            source = "aur"
        else:
            print(f"[SYSFORGE] {pkg} not found in pacman repos or AUR", file=sys.stderr)
            sys.exit(1)

    # Infer pkgbuild_patch from PKGBUILD (AUR only)
    pkgbuild_patch = False
    if source == "aur":
        config = load_config() or {}
        raw_dir = build_section.get("pkgbuild_dir") or config.get("paths", {}).get("pkgbuild_dir")
        if raw_dir:
            pkgbuild = Path(raw_dir).expanduser() / pkg / "PKGBUILD"
            if pkgbuild.exists():
                try:
                    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
                    from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                    pkgmeta = parse_pkgbuild(pkgbuild)
                    profile_data = extract_pkgbuild_profile(pkgmeta, pkgbuild)
                    pkgbuild_patch = bool(profile_data)
                except Exception as e:
                    _log.warn("[PACKAGES]", f"Could not infer pkgbuild_patch for {pkg}: {e}")

    # Build entry
    entry: dict = {"name": pkg, "source": source}
    if pkgbuild_patch:
        entry["pkgbuild_patch"] = True

    block = "\n" + _entry_toml_block(entry) + "\n"

    if path.exists():
        with open(path, "a") as f:
            f.write(block)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# packages.toml — managed by sysforge packages\n"
            "\n[build]\n"
            'pkgbuild_dir = "~/builds"\n'
        )
        path.write_text(header + block)

    msg = f"Added {pkg} ({source})"
    if pkgbuild_patch:
        msg += " [pkgbuild_patch=true]"
    print(msg)


# ---------------------------------------------------------------------------
# packages remove
# ---------------------------------------------------------------------------

def cmd_packages_remove(args):
    pkg = args.pkg
    path = _resolve_packages_file(getattr(args, "packages", None))

    if not path.exists():
        print(f"[SYSFORGE] packages.toml not found: {path}", file=sys.stderr)
        sys.exit(1)

    text = path.read_text()
    lines = text.splitlines(keepends=True)

    # Find all [[package]] block start indices
    block_starts = [i for i, l in enumerate(lines) if l.strip() == "[[package]]"]

    target_start = None
    target_end = None
    for idx, start in enumerate(block_starts):
        end = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
        block_lines = lines[start:end]
        if any(l.strip() == f'name = "{pkg}"' for l in block_lines):
            target_start = start
            target_end = end
            break

    if target_start is None:
        print(f"[SYSFORGE] {pkg} not found in {path}", file=sys.stderr)
        sys.exit(1)

    # Also remove blank lines immediately before the block
    remove_start = target_start
    while remove_start > 0 and lines[remove_start - 1].strip() == "":
        remove_start -= 1

    new_lines = lines[:remove_start] + lines[target_end:]
    path.write_text("".join(new_lines))
    print(f"Removed {pkg} from {path}")


# ---------------------------------------------------------------------------
# packages sync
# ---------------------------------------------------------------------------

def cmd_packages_sync(args):
    path = _resolve_packages_file(getattr(args, "packages", None))
    dry_run = getattr(args, "dry_run", False)

    if not path.exists():
        print(f"[SYSFORGE] packages.toml not found: {path}", file=sys.stderr)
        sys.exit(1)

    data = _load_toml(path)
    build_section = data.get("build", {})
    entries = data.get("package", [])

    if not entries:
        print("No packages to sync.")
        return

    from sysforge.primitives.aur import is_repo_package, aur_info
    config = load_config() or {}

    # Batch AUR lookup for non-repo packages
    aur_candidates = [e["name"] for e in entries if not is_repo_package(e["name"])]
    aur_found: set[str] = set()
    if aur_candidates:
        aur_found = set(aur_info(aur_candidates).keys())

    raw_dir = build_section.get("pkgbuild_dir") or config.get("paths", {}).get("pkgbuild_dir")
    pkgbuild_dir = Path(raw_dir).expanduser() if raw_dir else None

    changes: list[str] = []
    new_entries: list[dict] = []

    for entry in entries:
        name = entry["name"]
        new_entry = dict(entry)

        # Re-classify source
        if is_repo_package(name):
            new_source = "repo"
        elif name in aur_found:
            new_source = "aur"
        else:
            _log.warn("[PACKAGES]", f"{name}: not found in repos or AUR — keeping existing source")
            new_source = entry.get("source", "unknown")

        if new_source != entry.get("source"):
            changes.append(f"  {name}: source {entry.get('source')!r} → {new_source!r}")
        new_entry["source"] = new_source

        # Re-check pkgbuild_patch (AUR only, only when PKGBUILD is present)
        if new_source == "aur" and pkgbuild_dir:
            pkgbuild = pkgbuild_dir / name / "PKGBUILD"
            if pkgbuild.exists():
                try:
                    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
                    from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                    pkgmeta = parse_pkgbuild(pkgbuild)
                    profile_data = extract_pkgbuild_profile(pkgmeta, pkgbuild)
                    new_patch = bool(profile_data)
                    old_patch = entry.get("pkgbuild_patch", False)
                    if new_patch != old_patch:
                        changes.append(f"  {name}: pkgbuild_patch {old_patch!r} → {new_patch!r}")
                    if new_patch:
                        new_entry["pkgbuild_patch"] = True
                    else:
                        new_entry.pop("pkgbuild_patch", None)
                except Exception as e:
                    _log.warn("[PACKAGES]", f"Could not check pkgbuild_patch for {name}: {e}")

        new_entries.append(new_entry)

    if not changes:
        print("All entries up to date.")
        return

    print("Changes:" if not dry_run else "Would change:")
    for c in changes:
        print(c)

    if dry_run:
        return

    _write_packages_toml(path, build_section, new_entries)
    print(f"\nUpdated {path}")
    print("Note: comments and section headers in the original file were not preserved.")
