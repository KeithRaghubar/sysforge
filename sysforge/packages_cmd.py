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

from sysforge import log
_log = log.get_logger("PACKAGES")
from sysforge.primitives.config import load_config
from sysforge.primitives.paths import resolve_packages_path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_packages_file(args_packages: str | None) -> Path:
    """Resolve packages.toml path from arg, config, or default."""
    if args_packages:
        return Path(args_packages)
    config = load_config() or {}
    return resolve_packages_path(config)


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def entry_toml_block(entry: dict) -> str:
    """Serialise a package entry dict to a TOML [[package]] block string."""
    lines = ["[[package]]", f'name = "{entry["name"]}"', f'source = "{entry["source"]}"']
    for key in ("pkgbuild_patch", "cache", "reason"):
        if key not in entry:
            continue
        val = entry[key]
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, str):
            lines.append(f'{key} = "{val}"')
        else:
            lines.append(f"{key} = {val!r}")
    return "\n".join(lines)


def _classify_and_build_entries(
    names: list[str],
    build_section: dict,
    *,
    reason: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Classify names as repo / AUR and infer pkgbuild_patch for AUR pkgs.

    Shared by `cmd_packages_add` and `append_explicit_entries`. Runs one
    batched `repo_packages` + one batched `aur_info` RPC for the input set.
    Returns (entries, unknown_names) where entries is the list of dicts
    ready for `entry_toml_block`, and unknown_names is the subset that was
    in neither pacman repos nor AUR (caller decides whether to warn or fatal).
    """
    if not names:
        return [], []

    from sysforge.primitives.aur import repo_packages, aur_info
    repo_pkgs = repo_packages(names)
    aur_candidates = [p for p in names if p not in repo_pkgs]
    aur_found = set(aur_info(aur_candidates).keys()) if aur_candidates else set()

    sources: dict[str, str] = {p: "repo" for p in repo_pkgs}
    for p in aur_candidates:
        if p in aur_found:
            sources[p] = "aur"

    unknown = [p for p in names if p not in sources]

    config = load_config() or {}
    raw_dir = (
        build_section.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )

    entries: list[dict] = []
    for pkg in names:
        if pkg not in sources:
            continue
        source = sources[pkg]
        entry: dict = {"name": pkg, "source": source}
        if source == "aur" and raw_dir:
            pkgbuild = Path(raw_dir).expanduser() / pkg / "PKGBUILD"
            if pkgbuild.exists():
                try:
                    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
                    from sysforge.primitives.pkgbuild_patcher import extract_pkgbuild_profile
                    pkgmeta = parse_pkgbuild(pkgbuild)
                    profile_data = extract_pkgbuild_profile(pkgmeta, pkgbuild)
                    if profile_data:
                        entry["pkgbuild_patch"] = True
                except Exception as e:
                    _log.warn(f"Could not infer pkgbuild_patch for {pkg}: {e}")
        if reason is not None:
            entry["reason"] = reason
        entries.append(entry)

    return entries, unknown


def append_dependency_entries(
    dep_names: list[str],
    packages_file: str | None = None,
) -> list[str]:
    """Append AUR deps to packages.toml with reason='dependency'.

    Skips entries that already exist.  Returns list of names actually added.
    The AUR resolver only yields AUR deps, so source is forced to "aur"
    without re-running the classifier.
    """
    path = _resolve_packages_file(packages_file)

    existing_names: set[str] = set()
    if path.exists():
        data = _load_toml(path)
        existing_names = {e.get("name") for e in data.get("package", [])}

    to_add = [n for n in dep_names if n not in existing_names]
    if not to_add:
        return []

    entries = [{"name": n, "source": "aur", "reason": "dependency"} for n in to_add]
    blocks = "".join("\n" + entry_toml_block(e) + "\n" for e in entries)

    with open(path, "a") as f:
        f.write(blocks)

    for name in to_add:
        _log.ui(f"Tracked dependency: {name} → {path}")

    return to_add


def append_explicit_entries(
    pkg_names: list[str],
    packages_file: str | None = None,
) -> list[str]:
    """Append explicitly-installed packages to packages.toml.

    Called by `_cmd_build` after a successful `sysforge build <pkg> -i` to
    auto-track the manifest entry. Idempotent: silently skips names already
    present (regardless of their existing `reason`). Best-effort: any
    failure is logged and swallowed — a successful build/install must
    never be rolled back by manifest bookkeeping.

    Source classification (repo vs aur) and `pkgbuild_patch` inference
    follow the same path as `sysforge packages add`. Names that classify
    as neither repo nor AUR get a WARN; the user can run `packages add`
    manually for those.

    For split packages, callers pass the user-supplied name (typically the
    pkgbase or the pkgname they typed on the CLI). Matches `packages add`
    semantics.

    Returns the list of names actually appended.
    """
    try:
        path = _resolve_packages_file(packages_file)

        existing_entries: list[dict] = []
        build_section: dict = {}
        if path.exists():
            data = _load_toml(path)
            existing_entries = data.get("package", [])
            build_section = data.get("build", {})
        elif not path.parent.exists():
            # Implicit bookkeeping shouldn't materialise new directories.
            _log.warn(
                f"packages.toml parent {path.parent} missing; "
                f"skipping auto-track of {pkg_names}"
            )
            return []

        existing_names = {e.get("name") for e in existing_entries}
        to_classify = [n for n in pkg_names if n not in existing_names]
        if not to_classify:
            return []

        entries, unknown = _classify_and_build_entries(to_classify, build_section)

        for name in unknown:
            _log.warn(
                f"Could not classify {name} as repo or AUR; skipping packages.toml track. "
                f"Run `sysforge packages add {name}` manually."
            )

        if not entries:
            return []

        blocks = "".join("\n" + entry_toml_block(e) + "\n" for e in entries)
        if path.exists():
            with open(path, "a") as f:
                f.write(blocks)
        else:
            path.write_text(
                "# packages.toml — managed by sysforge packages\n" + blocks
            )

        added = [e["name"] for e in entries]
        for name in added:
            _log.ui(f"Tracked explicit: {name} → {path}")
        return added
    except Exception as e:
        _log.warn(f"Could not auto-track {pkg_names} in packages.toml: {e}")
        return []


# ---------------------------------------------------------------------------
# packages list
# ---------------------------------------------------------------------------

def cmd_packages_list(args):
    if getattr(args, "diagnose", False):
        _diagnose_manifest(args)
        return
    if getattr(args, "state", False):
        _list_build_state(args)
        return

    path = _resolve_packages_file(getattr(args, "packages", None))
    if not path.exists():
        _log.fatal(f"No packages.toml at {path}")

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
        if e.get("reason") == "dependency":
            flags.append("dep")
        flag_str = ", ".join(flags)
        print(f"  {name:<{max_name}}  {source:<{max_src}}  {flag_str}")


def _list_build_state(args):
    """Print build_state.toml entries in the same tabular style as the manifest."""
    from sysforge.primitives.build_state import BuildState
    from sysforge.pipeline.state import resolve_state_dir

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    entries = bs.all_packages()
    if not entries:
        print(f"No build state recorded in {state_dir / 'build_state.toml'}")
        return

    rows = []
    for pkgname, rec in sorted(entries.items()):
        pkgver = rec.get("pkgver", "")
        pkgrel = rec.get("pkgrel", "")
        ver = f"{pkgver}-{pkgrel}" if pkgrel else pkgver
        epoch = rec.get("epoch", "0")
        if epoch and epoch != "0":
            ver = f"{epoch}:{ver}"
        rows.append((
            pkgname,
            rec.get("pkgbase", ""),
            ver,
            rec.get("build_mode", ""),
            rec.get("pkgbuild_dir", ""),
        ))

    max_name = max(len(r[0]) for r in rows)
    max_base = max(len(r[1]) for r in rows)
    max_ver = max(len(r[2]) for r in rows)
    max_mode = max(len(r[3]) for r in rows)

    header = (
        f"  {'NAME':<{max_name}}  {'PKGBASE':<{max_base}}  "
        f"{'VERSION':<{max_ver}}  {'MODE':<{max_mode}}  PKGBUILD_DIR"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, base, ver, mode, pdir in rows:
        print(
            f"  {name:<{max_name}}  {base:<{max_base}}  "
            f"{ver:<{max_ver}}  {mode:<{max_mode}}  {pdir}"
        )
    print(f"\n  {len(rows)} recorded package(s)")


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

    entries_to_write, unknown = _classify_and_build_entries(to_process, build_section)
    for p in unknown:
        print(f"[SYSFORGE] {p} not found in pacman repos or AUR", file=sys.stderr)
        had_error = True
    if not entries_to_write:
        sys.exit(1)

    blocks_text = "".join("\n" + entry_toml_block(e) + "\n" for e in entries_to_write)
    if path.exists():
        with open(path, "a") as f:
            f.write(blocks_text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# packages.toml — managed by sysforge packages\n"
            "\n[build]\n"
            'pkgbuild_src_dir = "~/src"\n'
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
        _log.fatal(f"packages.toml not found: {path}")

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
        _log.fatal(f"{pkg} not found in {path}")

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


def _probe_pkgbuild_dir(d: Path) -> str:
    """Return an update.py-equivalent status string for a candidate pkgbuild dir.

    Mirrors the silent-skip conditions in sysforge.update and
    sysforge.primitives.aur.git_fetch_and_compare so the diagnostic matches reality.
    """
    import subprocess
    if not d.exists():
        return "DIR_MISSING"
    if not (d / "PKGBUILD").exists():
        return "NO_PKGBUILD"
    r = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if r.returncode != 0:
        return "NOT_GIT"
    r = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--abbrev-ref",
         "--symbolic-full-name", "@{u}"],
        capture_output=True,
    )
    if r.returncode != 0:
        return "NO_UPSTREAM"
    return "OK"


# Statuses that cause `sysforge update` to silently do nothing for a package.
_SILENT_FAILURE_STATUSES = {"DIR_MISSING", "NO_PKGBUILD", "NOT_GIT", "NO_UPSTREAM"}


def _diagnose_manifest(args):
    """Print per-package directory/git status as `sysforge update` would see it.

    Walks every entry in packages.toml, resolves its pkgbuild_src_dir exactly like
    update.py does (build_state entry if present, else pkgbuild_src_dir_base / name),
    and probes that path.  Packages that resolve to DIR_MISSING / NO_PKGBUILD /
    NOT_GIT / NO_UPSTREAM are the silent-failure buckets — update.py will either
    skip them with a buried warning or treat them as UP_TO_DATE against a stale
    local PKGBUILD.  Use this when `sysforge update` is missing packages you
    know have upstream changes.
    """
    from sysforge.primitives.build_state import BuildState
    from sysforge.pipeline.state import resolve_state_dir

    path = _resolve_packages_file(getattr(args, "packages", None))
    if not path.exists():
        _log.fatal(f"No packages.toml at {path}")

    data = _load_toml(path)
    build_cfg = data.get("build", {})
    entries = [e for e in data.get("package", []) if "name" in e]
    if not entries:
        print(f"No packages defined in {path}")
        return

    config = load_config() or {}
    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    build_state_pkgs = bs.all_packages()

    raw_dir = (
        build_cfg.get("pkgbuild_src_dir")
        or config.get("paths", {}).get("pkgbuild_src_dir")
    )
    pkgbuild_src_dir_base = Path(raw_dir).expanduser() if raw_dir else None

    rows: list[tuple[str, str, str, str, str]] = []
    for entry in entries:
        name = entry["name"]
        source = entry.get("source", "")
        bs_entry = build_state_pkgs.get(name)
        # Pacman-mode superset markers carry no pkgbuild_dir — treat them as
        # UNRECORDED so the probe falls back to pkgbuild_src_dir_base / name.
        # Missing build_mode defaults to profiled (legacy pre-superset records).
        if bs_entry is not None and bs_entry.get("build_mode", "profiled") != "pacman":
            tracking = "TRACKED"
            resolved_dir = Path(bs_entry.get("pkgbuild_dir", ""))
        else:
            tracking = "UNRECORDED"
            if pkgbuild_src_dir_base is None:
                rows.append((name, source, tracking, "NO_PKGBUILD_BASE", ""))
                continue
            resolved_dir = pkgbuild_src_dir_base / name
        status = _probe_pkgbuild_dir(resolved_dir)
        rows.append((name, source, tracking, status, str(resolved_dir)))

    only_problems = getattr(args, "problems_only", False)
    display_rows = [
        r for r in rows if not only_problems or r[3] in _SILENT_FAILURE_STATUSES
    ]

    if not display_rows:
        if only_problems:
            print(f"All {len(rows)} package(s) resolve cleanly.")
        else:
            print(f"No packages to display from {path}")
        return

    max_name = max(len(r[0]) for r in display_rows)
    max_src = max(len(r[1]) for r in display_rows)
    max_track = max(len(r[2]) for r in display_rows)
    max_status = max(len(r[3]) for r in display_rows)

    header = (
        f"  {'NAME':<{max_name}}  {'SOURCE':<{max_src}}  "
        f"{'TRACK':<{max_track}}  {'STATUS':<{max_status}}  PKGBUILD_DIR"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for name, source, tracking, status, pdir in display_rows:
        print(
            f"  {name:<{max_name}}  {source:<{max_src}}  "
            f"{tracking:<{max_track}}  {status:<{max_status}}  {pdir}"
        )

    # Summary counts, always computed from the full row set (not display_rows)
    # so --problems-only still shows the clean-package total.
    counts: dict[str, int] = {}
    for _, _, _, status, _ in rows:
        counts[status] = counts.get(status, 0) + 1

    print()
    print(f"  {len(rows)} package(s) in {path.name}")
    for status in ("OK", *sorted(s for s in counts if s != "OK")):
        if status in counts:
            marker = " " if status == "OK" else "!"
            print(f"    {marker} {status:<16} {counts[status]}")

    silent = sum(counts.get(s, 0) for s in _SILENT_FAILURE_STATUSES)
    if silent:
        print()
        print(
            f"  {silent} package(s) would be silently skipped or "
            f"stale-compared by `sysforge update`."
        )


def cmd_packages_repair_state(args):
    """Re-parse PKGBUILDs for build_state entries containing unexpanded shell vars
    and rewrite them with correctly expanded pkgname/pkgbase/pkgver/pkgrel/epoch.

    An entry is considered broken if its key or any of pkgbase/pkgver/pkgrel/epoch
    contains a literal ``$``.  Broken entries are grouped by pkgbuild_dir; the
    PKGBUILD for each dir is re-parsed through the current (variable-expanding)
    parser, and all entries under that dir are replaced with the expected set.
    build_mode, flags_string, and built_at are carried over from the first old
    entry in the group so true build history is preserved.

    Skips a group when the PKGBUILD is missing or when re-parse still yields
    ``$`` in any relevant field (e.g. shell parameter expansion the parser
    cannot statically resolve).
    """
    from collections import defaultdict
    from sysforge.primitives.build_state import BuildState
    from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
    from sysforge.pipeline.state import resolve_state_dir

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    entries = bs.all_packages()
    if not entries:
        print(f"No build state at {state_dir / 'build_state.toml'}")
        return

    dry_run = getattr(args, "dry_run", False)

    def _has_var(s) -> bool:
        return isinstance(s, str) and "$" in s

    def _entry_broken(name: str, rec: dict) -> bool:
        if _has_var(name):
            return True
        return any(_has_var(rec.get(k, "")) for k in ("pkgbase", "pkgver", "pkgrel", "epoch"))

    # Group entries by their pkgbuild_dir so split packages are repaired together.
    by_dir: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for name, rec in entries.items():
        by_dir[rec.get("pkgbuild_dir", "")].append((name, rec))

    plans: list[tuple[str, list[str], list[tuple[str, dict]]]] = []
    skipped_missing: list[tuple[str, list[str]]] = []
    skipped_unresolvable: list[tuple[str, list[str], str]] = []

    for pdir, group in sorted(by_dir.items()):
        if not any(_entry_broken(n, r) for n, r in group):
            continue

        if not pdir:
            skipped_missing.append(("(empty pkgbuild_dir)", [n for n, _ in group]))
            continue
        pkgbuild_path = Path(pdir) / "PKGBUILD"
        if not pkgbuild_path.exists():
            skipped_missing.append((pdir, [n for n, _ in group]))
            continue

        try:
            pkgmeta = parse_pkgbuild(pkgbuild_path)
        except Exception as e:
            skipped_unresolvable.append((pdir, [n for n, _ in group], f"parse failed: {e}"))
            continue

        g = pkgmeta.get("globals", {})
        expected_names = g.get("pkgname", [])
        if isinstance(expected_names, str):
            expected_names = [expected_names]
        if not expected_names:
            skipped_unresolvable.append((pdir, [n for n, _ in group], "no pkgname in PKGBUILD"))
            continue

        expected_pkgbase = g.get("pkgbase") or expected_names[0]
        expected_pkgver = g.get("pkgver", "")
        expected_pkgrel = g.get("pkgrel", "1")
        expected_epoch = g.get("epoch", "0")

        if (any(_has_var(n) for n in expected_names)
                or _has_var(expected_pkgbase)
                or _has_var(expected_pkgver)
                or _has_var(expected_pkgrel)
                or _has_var(expected_epoch)):
            unresolved_fields = [
                f"pkgname={expected_names}" if any(_has_var(n) for n in expected_names) else "",
                f"pkgbase={expected_pkgbase!r}" if _has_var(expected_pkgbase) else "",
                f"pkgver={expected_pkgver!r}" if _has_var(expected_pkgver) else "",
                f"pkgrel={expected_pkgrel!r}" if _has_var(expected_pkgrel) else "",
                f"epoch={expected_epoch!r}" if _has_var(expected_epoch) else "",
            ]
            reason = "expansion incomplete: " + ", ".join(f for f in unresolved_fields if f)
            skipped_unresolvable.append((pdir, [n for n, _ in group], reason))
            continue

        # Preserve build history from the first old entry — split packages
        # that built together share build_mode/flags_string/built_at.
        template = group[0][1]
        build_mode = template.get("build_mode")
        flags_string = template.get("flags_string")
        built_at = template.get("built_at")

        delete_keys = [n for n, _ in group]
        create_records: list[tuple[str, dict]] = []
        for name in expected_names:
            rec = {
                "pkgver": expected_pkgver,
                "pkgrel": expected_pkgrel,
                "epoch": expected_epoch,
                "pkgbase": expected_pkgbase,
                "pkgbuild_dir": pdir,
            }
            if build_mode is not None:
                rec["build_mode"] = build_mode
            if flags_string is not None:
                rec["flags_string"] = flags_string
            if built_at is not None:
                rec["built_at"] = built_at
            create_records.append((name, rec))

        plans.append((pdir, delete_keys, create_records))

    if not plans and not skipped_missing and not skipped_unresolvable:
        print("No broken entries found in build_state.toml.")
        return

    if plans:
        label = "Would apply" if dry_run else "Applying"
        print(f"{label} repairs for {len(plans)} PKGBUILD dir(s):\n")
        for pdir, dels, creates in plans:
            print(f"  {pdir}")
            for k in dels:
                print(f"    - delete  {k!r}")
            for name, rec in creates:
                ver = rec["pkgver"]
                if rec.get("pkgrel"):
                    ver = f"{ver}-{rec['pkgrel']}"
                if rec.get("epoch", "0") != "0":
                    ver = f"{rec['epoch']}:{ver}"
                print(f"    + create  {name!r:<40}  pkgbase={rec['pkgbase']!r}  version={ver}")
            print()

    if skipped_missing:
        print(f"Skipped {len(skipped_missing)} group(s) — PKGBUILD not found:")
        for pdir, names in skipped_missing:
            print(f"  {pdir}  ({', '.join(repr(n) for n in names)})")
        print()

    if skipped_unresolvable:
        print(f"Skipped {len(skipped_unresolvable)} group(s) — cannot resolve statically:")
        for pdir, names, reason in skipped_unresolvable:
            print(f"  {pdir}  ({', '.join(repr(n) for n in names)})")
            print(f"    reason: {reason}")
        print()

    if not plans:
        return
    if dry_run:
        print("Dry run — no changes written.  Re-run without --dry-run to apply.")
        return

    total_created = 0
    for _, dels, creates in plans:
        for k in dels:
            bs.delete(k)
        for name, rec in creates:
            bs.record(
                pkgname=name,
                pkgver=rec["pkgver"],
                pkgrel=rec["pkgrel"],
                epoch=rec["epoch"],
                pkgbase=rec["pkgbase"],
                pkgbuild_dir=Path(rec["pkgbuild_dir"]),
                build_mode=rec.get("build_mode"),
                flags_string=rec.get("flags_string"),
                built_at=rec.get("built_at"),
            )
            total_created += 1
    bs.save()
    print(f"Repaired {total_created} entry(ies) in {bs.path}")


def cmd_packages_sync(args):
    path = _resolve_packages_file(getattr(args, "packages", None))
    dry_run = getattr(args, "dry_run", False)

    if not path.exists():
        _log.fatal(f"packages.toml not found: {path}")

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

    raw_dir = build_section.get("pkgbuild_src_dir") or config.get("paths", {}).get("pkgbuild_src_dir")
    pkgbuild_src_dir = Path(raw_dir).expanduser() if raw_dir else None

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
            _log.warn(f"{name}: not found in repos or AUR — keeping existing source")
            new_source = entry.get("source", "unknown")

        if new_source != entry.get("source"):
            change_display.append(f"  {name}: source {entry.get('source')!r} → {new_source!r}")
            entry_changes["source"] = new_source

        # Re-check pkgbuild_patch (AUR only, only when PKGBUILD is present)
        if new_source == "aur" and pkgbuild_src_dir:
            pkgbuild = pkgbuild_src_dir / name / "PKGBUILD"
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
                    _log.warn(f"Could not check pkgbuild_patch for {name}: {e}")

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
