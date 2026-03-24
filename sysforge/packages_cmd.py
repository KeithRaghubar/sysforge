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
import re as _re
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


def entry_toml_block(entry: dict) -> str:
    """Serialise a package entry dict to a TOML [[package]] block string."""
    lines = ["[[package]]", f'name = "{entry["name"]}"', f'source = "{entry["source"]}"']
    for field in ("pkgbuild_patch", "cache"):
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
        flag_str = ", ".join(flags)
        print(f"  {name:<{max_name}}  {source:<{max_src}}  {flag_str}")


# ---------------------------------------------------------------------------
# packages add
# ---------------------------------------------------------------------------

def cmd_packages_add(args):
    pkgs = args.pkgs
    path = _resolve_packages_file(getattr(args, "packages", None))

    existing_entries: list[dict] = []
    build_section: dict = {}
    if path.exists():
        data = _load_toml(path)
        existing_entries = data.get("package", [])
        build_section = data.get("build", {})

    existing_names = {e.get("name") for e in existing_entries}
    had_error = False
    to_process = []
    for pkg in pkgs:
        if pkg in existing_names:
            print(f"[SYSFORGE] {pkg} is already in {path}", file=sys.stderr)
            had_error = True
        else:
            to_process.append(pkg)

    if not to_process:
        sys.exit(1)

    # Classify — batch repo + AUR lookup
    from sysforge.primitives.aur import repo_packages, aur_info
    repo_pkgs = repo_packages(to_process)
    aur_candidates = [p for p in to_process if p not in repo_pkgs]
    aur_found = set(aur_info(aur_candidates).keys()) if aur_candidates else set()

    sources: dict[str, str] = {p: "repo" for p in repo_pkgs}
    for p in aur_candidates:
        if p in aur_found:
            sources[p] = "aur"
        else:
            print(f"[SYSFORGE] {p} not found in pacman repos or AUR", file=sys.stderr)
            had_error = True

    to_add = [p for p in to_process if p in sources]
    if not to_add:
        sys.exit(1)

    # Infer pkgbuild_patch for AUR packages
    config = load_config() or {}
    raw_dir = build_section.get("pkgbuild_dir") or config.get("paths", {}).get("pkgbuild_dir")

    entries_to_write: list[dict] = []
    for pkg in to_add:
        source = sources[pkg]
        pkgbuild_patch = False
        if source == "aur" and raw_dir:
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
        entry: dict = {"name": pkg, "source": source}
        if pkgbuild_patch:
            entry["pkgbuild_patch"] = True
        entries_to_write.append(entry)

    blocks_text = "".join("\n" + entry_toml_block(e) + "\n" for e in entries_to_write)
    if path.exists():
        with open(path, "a") as f:
            f.write(blocks_text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# packages.toml — managed by sysforge packages\n"
            "\n[build]\n"
            'pkgbuild_dir = "~/builds"\n'
        )
        path.write_text(header + blocks_text)

    for entry in entries_to_write:
        msg = f"Added {entry['name']} ({entry['source']})"
        if entry.get("pkgbuild_patch"):
            msg += " [pkgbuild_patch=true]"
        print(msg)

    if had_error:
        sys.exit(1)


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

def _sync_toml_inplace(path: Path, changes_by_name: dict) -> None:
    """Apply field-level updates to [[package]] blocks without touching comments.

    changes_by_name: {pkg_name: {field: new_value}}
        new_value=None means remove the field line.
    """
    lines = path.read_text().splitlines(keepends=True)

    def _find_block(name):
        starts = [i for i, l in enumerate(lines) if l.strip() == "[[package]]"]
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
            for l in lines[start:end]:
                m = _re.match(r'\s*name\s*=\s*"([^"]+)"', l)
                if m and m.group(1) == name:
                    return start, end
        return None, None

    for pkg_name, field_changes in changes_by_name.items():
        for field, new_val in field_changes.items():
            start, end = _find_block(pkg_name)
            if start is None:
                continue
            field_abs = None
            for j, l in enumerate(lines[start:end]):
                if _re.match(rf'\s*{_re.escape(field)}\s*=', l):
                    field_abs = start + j
                    break
            if new_val is None:
                if field_abs is not None:
                    lines.pop(field_abs)
            elif field_abs is not None:
                if isinstance(new_val, bool):
                    lines[field_abs] = f'{field} = {"true" if new_val else "false"}\n'
                else:
                    lines[field_abs] = f'{field} = "{new_val}"\n'
            else:
                # Insert after last non-blank line in block
                start2, end2 = _find_block(pkg_name)
                if start2 is None or end2 is None:
                    continue
                last_content = start2
                for j in range(start2, end2):
                    if lines[j].strip():
                        last_content = j
                if isinstance(new_val, bool):
                    new_line = f'{field} = {"true" if new_val else "false"}\n'
                else:
                    new_line = f'{field} = "{new_val}"\n'
                lines.insert(last_content + 1, new_line)

    path.write_text("".join(lines))


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

    from sysforge.primitives.aur import repo_packages, aur_info
    config = load_config() or {}

    # Single batch repo check, then batch AUR lookup for non-repo packages
    all_names = [e["name"] for e in entries]
    repo_set = repo_packages(all_names)
    aur_candidates = [n for n in all_names if n not in repo_set]
    aur_found: set[str] = set()
    if aur_candidates:
        aur_found = set(aur_info(aur_candidates).keys())

    raw_dir = build_section.get("pkgbuild_dir") or config.get("paths", {}).get("pkgbuild_dir")
    pkgbuild_dir = Path(raw_dir).expanduser() if raw_dir else None

    change_display: list[str] = []
    changes_by_name: dict[str, dict] = {}

    for entry in entries:
        name = entry["name"]
        entry_changes: dict = {}

        # Re-classify source
        if name in repo_set:
            new_source = "repo"
        elif name in aur_found:
            new_source = "aur"
        else:
            _log.warn("[PACKAGES]", f"{name}: not found in repos or AUR — keeping existing source")
            new_source = entry.get("source", "unknown")

        if new_source != entry.get("source"):
            change_display.append(f"  {name}: source {entry.get('source')!r} → {new_source!r}")
            entry_changes["source"] = new_source

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
                        change_display.append(f"  {name}: pkgbuild_patch {old_patch!r} → {new_patch!r}")
                        # None signals removal of the field line
                        entry_changes["pkgbuild_patch"] = True if new_patch else None
                except Exception as e:
                    _log.warn("[PACKAGES]", f"Could not check pkgbuild_patch for {name}: {e}")

        if entry_changes:
            changes_by_name[name] = entry_changes

    if not change_display:
        print("All entries up to date.")
        return

    print("Changes:" if not dry_run else "Would change:")
    for c in change_display:
        print(c)

    if dry_run:
        return

    _sync_toml_inplace(path, changes_by_name)
    print(f"\nUpdated {path}")
