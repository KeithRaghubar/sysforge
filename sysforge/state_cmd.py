# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
state_cmd.py — build_state.toml inspection and repair

Implements the `sysforge state` subcommand namespace:
    list     — tabulate build_state.toml entries
    repair   — re-parse PKGBUILDs for entries with unexpanded shell variables
    orphans  — surface stale .pkg.tar* artifacts in PKGDEST
    failed   — list recorded build failures (with any diagnosed fix)

Separate from `sysforge packages`, which manages override rules in
packages.toml. The split mirrors the rules-vs-state separation described
in DESIGN.md §Package Manifest.

Public API:
    cmd_state_list(args)
    cmd_state_repair(args)
    cmd_state_orphans(args)
"""
from collections import defaultdict
from pathlib import Path

from sysforge import log
from sysforge.primitives.pager import maybe_pager as _maybe_pager
from sysforge.primitives.prompt import prompt_choice


def cmd_state_list(args):
    """Print build_state.toml entries in a tabular layout.

    Also surfaces *untracked foreign packages* — installed AUR-style packages
    (`pacman -Qm`) that have no entry in build_state.toml. Those slipped past
    sysforge (installed manually outside the manager) and won't be rebuilt by
    `sysforge update` from a known PKGBUILD without a fresh fetch.
    """
    from sysforge.primitives.build_state import BuildState
    from sysforge.pipeline.state import resolve_state_dir

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    entries = bs.all_packages()
    use_pager = not getattr(args, "no_pager", False)

    with _maybe_pager(use_pager):
        if not entries:
            print(f"No build state recorded in {state_dir / 'build_state.toml'}")
            _print_untracked_foreign(set())
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
        print(log.bold(header))
        print("  " + log.dim("-" * (len(header) - 2)))
        for name, base, ver, mode, pdir in rows:
            print(
                f"  {name:<{max_name}}  {base:<{max_base}}  "
                f"{ver:<{max_ver}}  {mode:<{max_mode}}  {pdir}"
            )
        print(f"\n  {len(rows)} recorded package(s)")
        _print_untracked_foreign(set(entries.keys()))


def _print_untracked_foreign(tracked: set[str]) -> None:
    """Print the foreign-but-untracked-by-sysforge section, when non-empty."""
    from sysforge.primitives.pacman import get_foreign_packages

    try:
        foreign = get_foreign_packages()
    except Exception:
        return
    untracked = sorted(set(foreign) - tracked)
    if not untracked:
        return
    print(f"\n  Untracked foreign packages ({len(untracked)}):")
    for name in untracked:
        print(f"    {name} {foreign[name]}")
    print(
        "    (installed via `pacman -Qm` but with no build_state.toml entry — "
        "`sysforge update` will pick these up the next time their pkgbase "
        "PKGBUILD is fetched)"
    )


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


def cmd_state_orphans(args):
    """Surface — and optionally remove — stale .pkg.tar* artifacts in PKGDEST.

    Lists only **superseded** artifacts: files whose pkgname is installed
    AND whose version is strictly older than the installed one. These are
    safe to delete because the installed package is, by definition, newer.

    Files whose pkgname is *not* installed are deliberately not surfaced —
    they could be a build kept on purpose (e.g. a kernel artifact for a
    branch with local commits the user wants to keep available for later
    install) and we can't safely tell the difference.

    Without ``--prune`` the command is read-only. With ``--prune`` the user
    is asked y/N (skip with ``--no-confirm``) before the files are removed.
    """
    from sysforge.primitives.pacman import (
        detect_orphan_artifacts,
        get_all_installed_packages,
        get_pkgdest,
    )

    pkgdest = get_pkgdest()
    if pkgdest is None:
        print("PKGDEST not set in /etc/makepkg.conf — nothing to scan.")
        return
    if not pkgdest.is_dir():
        print(f"PKGDEST {pkgdest} does not exist — nothing to scan.")
        return

    installed = get_all_installed_packages()
    result = detect_orphan_artifacts(pkgdest, installed)
    superseded = result.get("superseded", [])
    use_pager = not getattr(args, "no_pager", False)

    if not superseded:
        print(f"No superseded build artifacts in {pkgdest}.")
        return

    total = sum(p.stat().st_size for p in superseded if p.exists())
    with _maybe_pager(use_pager and not getattr(args, "prune", False)):
        print(f"PKGDEST: {pkgdest}\n")
        print(
            f"  Superseded — older than installed ({len(superseded)} file(s), "
            f"{total / 1024 / 1024:.1f} MiB):"
        )
        for p in superseded:
            print(f"    {p.name}")

    if not getattr(args, "prune", False):
        print("\nRun `sysforge state orphans --prune` to delete.")
        return

    if not getattr(args, "no_confirm", False):
        answer = prompt_choice(
            f"\nDelete {len(superseded)} file(s)? [y/N] ", ["y", "yes"],
            default="", retry_on_invalid=False,
        )
        if answer not in {"y", "yes"}:
            print("Aborted.")
            return

    removed = 0
    for path in superseded:
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            print(f"  could not remove {path.name}: {e}")
    print(f"Removed {removed} file(s) from {pkgdest}.")


def cmd_state_failed(args):
    """List recorded build failures from build_state.toml's ``[failures]`` table.

    With ``--clear PKGBASE`` / ``--clear-all`` the matching entries are removed
    instead of listed. Failures otherwise auto-clear on the next successful
    build of the same pkgbase (see BuildState.record).
    """
    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives.build_state import BuildState

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)

    if getattr(args, "clear_all", False):
        names = list(bs.all_failures())
        for pkgbase in names:
            bs.clear_failure(pkgbase)
        bs.save()
        print(f"Cleared {len(names)} recorded failure(s).")
        return

    clear_one = getattr(args, "clear", None)
    if clear_one:
        if bs.clear_failure(clear_one):
            bs.save()
            print(f"Cleared recorded failure for {clear_one!r}.")
        else:
            print(f"No recorded failure for {clear_one!r}.")
        return

    failures = bs.all_failures()
    use_pager = not getattr(args, "no_pager", False)

    with _maybe_pager(use_pager):
        if not failures:
            print(f"No build failures recorded in {state_dir / 'build_state.toml'}")
            return

        rows = []
        for pkgbase, rec in sorted(failures.items()):
            err = str(rec.get("error", "")).replace("\n", " | ")
            rows.append((
                pkgbase,
                rec.get("failed_at", ""),
                rec.get("signature", ""),
                err,
                rec.get("fix_cmd", ""),
            ))

        max_base = max(len("PKGBASE"), *(len(r[0]) for r in rows))
        max_when = max(len("FAILED_AT"), *(len(r[1]) for r in rows))
        max_sig = max(len("SIGNATURE"), *(len(r[2]) for r in rows))

        header = (
            f"  {'PKGBASE':<{max_base}}  {'FAILED_AT':<{max_when}}  "
            f"{'SIGNATURE':<{max_sig}}  ERROR"
        )
        print(log.bold(header))
        print("  " + log.dim("-" * (len(header) - 2)))
        for base, when, sig, err, fix in rows:
            err_short = err if len(err) <= 80 else err[:77] + "..."
            # Colour after padding so the escape codes don't skew column widths.
            print(
                f"  {log.red(f'{base:<{max_base}}')}  {when:<{max_when}}  "
                f"{sig:<{max_sig}}  {err_short}"
            )
            if fix:
                print(
                    f"  {'':<{max_base}}  {'':<{max_when}}  "
                    f"{'':<{max_sig}}  {log.green(f'fix: {fix}')}"
                )
        print(f"\n  {len(rows)} failed package(s)")
        print(
            "  (entries clear on the next successful build; "
            "or use --clear PKGBASE / --clear-all)"
        )


def cmd_state_forget(args):
    """Stop maintaining the named package(s): delete their build_state records.

    build_state is the authority for what ``sysforge update`` rebuilds from
    source. ``forget`` drops a package's record so update no longer tracks it —
    the "hand it back to pacman" escape hatch for the durable-by-default
    tracking model. The *installed* package is left in place; it still carries
    the ``sf-build`` pacman group, so ``pacman -Syu`` won't replace it. To fully
    revert to the stock repo binary, reinstall it explicitly with
    ``pacman -S <pkg>``. (The next update's ``sync_with_installed`` re-seeds a
    plain ``build_mode = "pacman"`` marker, which is inert.)

    A name matching a pkgbase forgets every split-package member sharing it.
    """
    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives.build_state import BuildState

    state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
    bs = BuildState(state_dir)
    all_pkgs = bs.all_packages()

    names = list(getattr(args, "pkgnames", None) or [])
    forgotten: list[str] = []
    missing: list[str] = []
    for name in names:
        # Delete the exact entry plus any split-package siblings whose pkgbase
        # equals the requested name (so `forget llvm` drops llvm-libs/polly too).
        targets = {name} if name in all_pkgs else set()
        targets |= {pn for pn, e in all_pkgs.items() if e.get("pkgbase") == name}
        if not targets:
            missing.append(name)
            continue
        for pn in sorted(targets):
            if bs.delete(pn):
                forgotten.append(pn)

    if forgotten:
        bs.save()
        print(f"Stopped tracking {len(forgotten)} package(s): {', '.join(forgotten)}")
        print("  (installed package left in place; it keeps the sf-build group, so "
              "`pacman -Syu` won't touch it.")
        print("   Reinstall the repo binary with `pacman -S <pkg>` to fully revert.)")
    for name in missing:
        print(f"No build_state record for {name!r} — nothing to forget.")


# ---------------------------------------------------------------------------
# Verb wrappers
# ---------------------------------------------------------------------------

from sysforge.verbs import ExecResult, PreCheckResult, Verb  # noqa: E402


class StateListVerb(Verb):
    """Read-only: tabulate build_state.toml entries + untracked foreign."""

    name = "state-list"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_state_list(args)
        return ExecResult()


class StateRepairVerb(Verb):
    """Re-parse PKGBUILDs to repair build_state entries with shell-var leaks.

    ``--dry-run`` short-circuits the actual write; the sentinel covers only
    the write path so dry runs don't install one.
    """

    name = "state-repair"
    requires_sentinel = True

    def pre_check(self, args) -> PreCheckResult:
        if getattr(args, "dry_run", False):
            # Dry-run is read-only; downgrade to no-sentinel for this run.
            self.requires_sentinel = False
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_state_repair(args)
        return ExecResult()


class StateOrphansVerb(Verb):
    """List (and optionally prune) stale PKGDEST artifacts.

    Sentinel only when ``--prune`` mutates the filesystem.
    """

    name = "state-orphans"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        self.requires_sentinel = bool(getattr(args, "prune", False))
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_state_orphans(args)
        return ExecResult()


class StateFailedVerb(Verb):
    """List recorded build failures (read-only).

    Sentinel only when ``--clear`` / ``--clear-all`` rewrites build_state.toml,
    mirroring StateRepairVerb's write path.
    """

    name = "state-failed"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        self.requires_sentinel = bool(
            getattr(args, "clear", None) or getattr(args, "clear_all", False)
        )
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_state_failed(args)
        return ExecResult()


class StateForgetVerb(Verb):
    """Drop build_state records so `update` stops maintaining the package(s).

    Writes build_state.toml, so it carries a sentinel like the other state
    write paths (repair / failed --clear).
    """

    name = "state-forget"
    requires_sentinel = True

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        cmd_state_forget(args)
        return ExecResult()
