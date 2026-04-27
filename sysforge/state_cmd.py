"""
state_cmd.py — build_state.toml inspection and repair

Implements the `sysforge state` subcommand namespace:
    list    — tabulate build_state.toml entries
    repair  — re-parse PKGBUILDs for entries with unexpanded shell variables

Separate from `sysforge packages`, which manages override rules in
packages.toml. The split mirrors the rules-vs-state separation described
in DESIGN.md §Package Manifest.

Public API:
    cmd_state_list(args)
    cmd_state_repair(args)
"""
from collections import defaultdict
from pathlib import Path


def cmd_state_list(args):
    """Print build_state.toml entries in a tabular layout."""
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


def cmd_state_repair(args):
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
