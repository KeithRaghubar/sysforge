# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/shared.py — cross-verb operations with a single home.

Verb modules (``sysforge/*_cmd.py``) had started importing each other:
``revert_cmd`` and ``uninstall_cmd`` both pulled ``cmd_state_forget`` out of
``state_cmd``, and ``build_cmd`` reached into ``packages_cmd`` for the
``packages.toml`` writer. Each was defensible alone; together they were the
seed of a fourth informal layer with no guard, and the direction of those edges
was arbitrary — nothing makes ``state_cmd`` the natural owner of "stop
maintaining this package" just because ``sysforge state forget`` is the verb
that spells it.

So the *home* moves here rather than the operation being duplicated, preserving
the one-home invariants (§``sysforge/CLAUDE.md``: one ``packages.toml`` writer;
demotion reuses one forget path). Verb modules import from ``verbs/``,
``primitives/`` and ``pipeline/`` — never from a sibling ``*_cmd.py``, which
``tests/test_module_layering.py`` now enforces (3.2.0-F8).

This module is for operations *shared between verbs*. Single-verb logic stays in
its own ``*_cmd.py``; small generic utilities go in ``verbs/helpers.py``.

Two clusters live here:

* **packages.toml entry shape and writer** — ``OVERRIDE_FIELDS``,
  ``entry_toml_block``, ``entry_is_inert``, ``rewrite_packages_toml``. Every
  mutation goes through the writer so header comments and surrounding
  whitespace survive and inert entries are pruned on the same write.
* **build_state demotion** — ``forget_packages``, the "hand it back to pacman"
  operation behind ``state forget``, ``revert`` and ``uninstall``.
* **revert to stock** — ``plan_revert`` and ``apply_reverts``, behind
  ``revert-to-stock`` and ``self-uninstall`` (3.5.0-F19).
* **pacman.conf IgnoreGroup** — the ``sf-build`` entry ``setup`` adds and
  ``self-uninstall`` takes out.
"""
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives import atomic_write, install_reconcile, pacman, profile
from sysforge.primitives.build_state import BuildState, is_source_build_mode
from sysforge.primitives.opt_out import VIA_REVERT
from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX
from sysforge.primitives.config import (
    PKG_KEY_BUILD_FROM_SOURCE,
    _LEGACY_PKG_KEY_BUILD_FROM_SOURCE,
)

_log = log.get_logger("PACKAGES")


# ---------------------------------------------------------------------------
# packages.toml entry shape
# ---------------------------------------------------------------------------

# Behavior-changing override fields. `source` is metadata (it pins routing
# but doesn't change build behavior), so it doesn't count toward the
# "at least one override" rule for `add` validation or auto-prune.
OVERRIDE_FIELDS = (PKG_KEY_BUILD_FROM_SOURCE, "cache", "reason")


def entry_toml_block(entry: dict) -> str:
    """Serialise a package entry dict to a TOML [[package]] block string."""
    lines = ["[[package]]", f'name = "{entry["name"]}"']
    if "source" in entry:
        lines.append(f'source = "{entry["source"]}"')
    for key in OVERRIDE_FIELDS:
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


def entry_is_inert(entry: dict) -> bool:
    """An entry is inert if it has no behavior-changing override field set.

    Deliberately legacy-agnostic. ``rewrite_packages_toml`` runs the legacy-key
    migration over the raw lines *before* parsing entries and judging inertness,
    so on the write path a pre-rename ``pkgbuild_patch`` entry already carries
    ``enable_build_from_source`` by the time it reaches here. The other callers
    (``update.py``, ``update_assemble.py``) pass raw ``expand_package_groups``
    output, where a stale ``pkgbuild_patch`` is simply *not* an override — which
    is correct as of 3.0.0, since the read-side alias was removed (2.6.1-F5).
    Either way this predicate needs no legacy-key knowledge.
    """
    return not any(k in entry for k in OVERRIDE_FIELDS)


# ---------------------------------------------------------------------------
# Line-level packages.toml writers
#
# Every mutation goes through `rewrite_packages_toml` so we (a) preserve
# header comments and surrounding whitespace and (b) auto-prune inert
# entries on the same write.
# ---------------------------------------------------------------------------

def _split_blocks(lines: list[str]) -> tuple[list[str], list[tuple[int, int]]]:
    """Locate [[package]] blocks. Returns (lines, [(start, end), ...])."""
    block_starts = [i for i, line in enumerate(lines) if line.strip() == "[[package]]"]
    blocks: list[tuple[int, int]] = []
    for idx, start in enumerate(block_starts):
        end = block_starts[idx + 1] if idx + 1 < len(block_starts) else len(lines)
        blocks.append((start, end))
    return lines, blocks


def _block_entry(lines: list[str], start: int, end: int) -> dict:
    """Parse a single [[package]] block range into an entry dict."""
    snippet = "".join(lines[start:end])
    try:
        parsed = tomllib.loads(snippet)
    except tomllib.TOMLDecodeError:
        return {}
    pkgs = parsed.get("package", [])
    return pkgs[0] if pkgs else {}


# Written when packages.toml does not exist yet, so the first writer leaves a
# self-documenting file with its [build] section (3.5.0-B48).
PACKAGES_TOML_HEADER = (
    "# packages.toml: managed by sysforge packages\n"
    "\n[build]\n"
    'pkgbuild_src_dir = "~/src"\n'
)


def rewrite_packages_toml(path: Path, *, append: str = "", drop_name: str | None = None) -> None:
    """Apply changes to packages.toml at the line level, preserving comments.

    - `append`: text to append (typically a new [[package]] block, with
      its own leading newline).
    - `drop_name`: if set, remove the existing [[package]] block whose `name`
      matches (never the appended one, so drop + append replaces an entry in
      one write).
    - Always: auto-prune any [[package]] block that contains no
      behavior-changing override field (see OVERRIDE_FIELDS).

    A missing file starts from :data:`PACKAGES_TOML_HEADER`. The result is
    written once, through ``atomic_write.replace_file`` (atomic; escalates for
    the root-owned default ``/etc/sysforge``, 3.5.0-B47).
    """
    from sysforge.primitives import atomic_write

    text = path.read_text(encoding="utf-8") if path.exists() else PACKAGES_TOML_HEADER
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    appended_from = len(lines)

    if append:
        # Split into lines so the block finder sees the appended header.
        block = append if append.startswith("\n") else "\n" + append
        lines.extend(block.splitlines(keepends=True))
        if not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"

    # Migrate the legacy per-package key in place on every rewrite, *before*
    # block-parsing and the prune decision below — so a pre-rename entry is
    # already carrying the current key name when `entry_is_inert` judges it,
    # and is migrated rather than pruned as inert. Anchored at the start of
    # the line (after indentation) so it never touches the key name embedded
    # in a reason string or comment. Substitution never changes line count,
    # so it's safe to apply ahead of the index-based blank-line peeling below.
    _legacy_key_re = re.compile(
        rf"^(\s*){re.escape(_LEGACY_PKG_KEY_BUILD_FROM_SOURCE)}(\s*=)"
    )
    lines = [
        _legacy_key_re.sub(rf"\1{PKG_KEY_BUILD_FROM_SOURCE}\2", line)
        for line in lines
    ]

    keep_lines: list[str] = []
    drop_ranges: list[tuple[int, int]] = []

    _, blocks = _split_blocks(lines)
    for start, end in blocks:
        entry = _block_entry(lines, start, end)
        name = entry.get("name", "")
        dropped_by_name = (drop_name is not None and name == drop_name
                           and start < appended_from)
        if dropped_by_name or entry_is_inert(entry):
            drop_ranges.append((start, end))

    # Build the kept-line list, also peeling a leading blank-line run before
    # each dropped block so we don't leave stacked blanks behind.
    cursor = 0
    drop_set: set[int] = set()
    for start, end in drop_ranges:
        peel_start = start
        while peel_start > cursor and lines[peel_start - 1].strip() == "":
            peel_start -= 1
        for i in range(peel_start, end):
            drop_set.add(i)
    keep_lines = [line for i, line in enumerate(lines) if i not in drop_set]

    # Drop trailing blank-line runs to avoid growth across rewrites.
    while keep_lines and keep_lines[-1].strip() == "":
        keep_lines.pop()
    if keep_lines:
        keep_lines.append("\n") if not keep_lines[-1].endswith("\n") else None

    atomic_write.replace_file(path, "".join(keep_lines), tag="PACKAGES")


# ---------------------------------------------------------------------------
# build_state demotion
# ---------------------------------------------------------------------------

def forget_packages(state_dir, pkgnames: list[str], *,
                    opt_out: str | None = None) -> tuple[list[str], list[str]]:
    """Stop maintaining the named package(s): delete their build_state records.

    build_state is the authority for what ``sysforge update`` rebuilds from
    source. Forgetting drops a package's record so update no longer tracks it —
    the "hand it back to pacman" escape hatch for the durable-by-default
    tracking model. The *installed* package is left in place; it still carries
    the ``sf-build`` pacman group, so ``pacman -Syu`` won't replace it. To fully
    revert to the stock repo binary, reinstall it explicitly with
    ``pacman -S <pkg>``. (The next update's ``sync_with_installed`` re-seeds a
    plain ``build_mode = "pacman"`` marker, which is inert.)

    A name matching a pkgbase forgets every split-package member sharing it.

    Returns ``(forgotten, missing)`` — the pkgnames actually deleted (split
    siblings expanded) and the requested names that had no record. Printing is
    the caller's business: ``state forget`` reports both lists, while ``revert``
    and ``uninstall`` fold demotion into their own output. Takes a state dir and
    a name list rather than an ``args`` namespace, because two of its three
    callers are not the ``forget`` verb and were synthesising a fake namespace
    to call it.

    Tracking ends with the record, so a recorded build failure of a pkgbase
    left with no tracked member is cleared too, as is one keyed on a
    requested name (3.5.0-B120): ``state failed`` would otherwise keep
    listing a package nothing builds any more.

    ``opt_out`` (``opt_out.VIA_REVERT`` / ``VIA_FORGET``, 3.4.0-F4) also records
    the decision in ``opted-out.toml``, one record per stock pkgbase covering
    every forgotten member (renamed and stock spellings), so ``update`` does
    not re-adopt the package. ``uninstall`` passes none: a removed package is
    outside the installed set, and a later reinstall is a fresh decision.
    """
    from sysforge.primitives.build_state import BuildState

    bs = BuildState(state_dir)
    all_pkgs = bs.all_packages()

    forgotten: list[str] = []
    missing: list[str] = []
    for name in list(pkgnames or []):
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

    cleared = _clear_untracked_failures(
        bs, set(pkgnames or []) | {
            k for pn in forgotten
            for k in (all_pkgs[pn].get("pkgbase"), all_pkgs[pn].get("origin_pkgbase")) if k})
    if forgotten or cleared:
        # Opt-out first: it is idempotent, so a failure or Ctrl-C between the
        # two writes leaves the package still tracked (retry the command)
        # rather than forgotten but adoptable by the next update (3.5.0-B57).
        if opt_out:
            _record_opt_out(state_dir, {pn: all_pkgs[pn] for pn in forgotten}, opt_out)
        bs.save()
    return forgotten, missing


def _clear_untracked_failures(bs, pkgbases: set[str]) -> list[str]:
    """Clear the failures of *pkgbases* no remaining build_state entry belongs to."""
    remaining = bs.all_packages()
    owned = set(remaining) | {
        k for e in remaining.values()
        for k in (e.get("pkgbase"), e.get("origin_pkgbase")) if k}
    return [b for b in sorted(pkgbases - owned) if bs.clear_failure(b)]


def _record_opt_out(state_dir, entries: dict[str, dict], via: str) -> None:
    """Write one opt-out record per stock pkgbase for the forgotten entries."""
    from sysforge.primitives.opt_out import OptOutList
    from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX

    suffix = f"-{RENAME_SUFFIX}"
    by_base: dict[str, set[str]] = {}
    for pn, e in entries.items():
        # Keyed by the stock pkgbase: after a revert the installed package is
        # the stock one, which is what update's adoption sources will see.
        base = e.get("origin_pkgbase") or e.get("pkgbase") or pn
        if base.endswith(suffix):
            base = base[: -len(suffix)]
        stock = pn[: -len(suffix)] if pn.endswith(suffix) else pn
        by_base.setdefault(base, set()).update({pn, stock})
    record = OptOutList(state_dir)
    for base, names in sorted(by_base.items()):
        record.add(base, names, via=via)


def profile_store_names(state_dir, pkgnames) -> list[str]:
    """Every name a removed package's profile store may be keyed on.

    Read *before* ``forget_packages`` erases the record: the instrumentation
    store is keyed on the pre-rename pkgbase (``origin_pkgbase``: ``pgo/mesa``,
    not ``pgo/mesa-sysforge``), the kernel methods on pkgname
    (3.2.0-F11).
    """
    from sysforge.primitives.build_state import BuildState

    bs = BuildState(state_dir)
    names: list[str] = []
    for name in pkgnames:
        entry = bs.get(name) or {}
        names += [n for n in (name, entry.get("pkgbase"), entry.get("origin_pkgbase")) if n]
    return list(dict.fromkeys(names))


def emit_orphaned_profiles(names, emit) -> None:
    """Name each non-empty profile store a removal left behind. Never raises."""
    from sysforge import state_cmd

    try:
        for line in state_cmd.orphaned_profile_lines(names):
            emit(line)
    except Exception:  # noqa: BLE001 — advisory notice after a completed removal
        return


# ---------------------------------------------------------------------------
# Revert to stock (revert-to-stock, self-uninstall)
# ---------------------------------------------------------------------------

_revert_log = log.get_logger("REVERT")


@dataclass
class RevertPlan:
    """One target's resolved revert action (produced by :func:`plan_revert`)."""

    target: str
    action: str            # "skip" | "reinstall" | "replace" | "derename"
    pkgname: str | None     # installed name to act on
    stock_pkg: str | None   # repo package to (re)install
    reason: str
    # ``replace`` only: every installed split member reverted in the same
    # transaction, as (renamed pkgname, stock pkgname), sorted by pkgname.
    members: list[tuple[str, str]] = field(default_factory=list)


def _stock_name(pkgname: str) -> str:
    """Stock pkgname of a conflict-mode member (``vulkan-radeon-sysforge`` →
    ``vulkan-radeon``): ``patch_package_suffix`` appends the suffix per member."""
    suffix = f"-{RENAME_SUFFIX}"
    return pkgname[: -len(suffix)] if pkgname.endswith(suffix) else pkgname


def _conflict_members(entries: dict, key: str, installed: set | None) -> list:
    """Installed members of ``key``'s split set, each paired with its stock name.

    Every member records the same ``origin_pkgbase``, so the reverse lookup that
    produced ``key`` picks one sibling arbitrarily (the lowest key). Reverting
    only that one left the other installed ``-sysforge`` members behind,
    untracked and stale (3.3.0-B6). Members built but not installed are skipped:
    there is nothing to swap. ``key`` itself is always kept. ``installed`` is
    queried only when the set actually has siblings.
    """
    pkgbase = entries[key].get("pkgbase")
    sibs = sorted(n for n, e in entries.items()
                  if pkgbase and e.get("pkgbase") == pkgbase)
    if len(sibs) > 1:
        if installed is None:
            installed = set(pacman.get_all_installed_packages())
        sibs = [n for n in sibs if n == key or n in installed]
    else:
        sibs = [key]
    return [(n, _stock_name(n)) for n in sibs]


def plan_revert(bs: BuildState, targets: list, installed: set | None = None) -> list:
    """Resolve each target to a :class:`RevertPlan`. Pure — no mutation.

    ``installed`` (pkgnames) scopes a conflict-mode split revert to the members
    actually installed; ``None`` queries pacman, only if a split set needs it.
    """
    plans: list[RevertPlan] = []
    entries = bs.all_packages()
    for target in targets:
        # Reverse lookup (shared home): user may have named the stock base of a
        # renamed build; resolve it to the actually-installed pkgname.
        target_name = install_reconcile.resolve_installed_name(bs, target)
        entry = entries.get(target_name)
        if entry is None:
            plans.append(RevertPlan(target, "skip", None, None,
                                    "not tracked by sysforge: already stock"))
            continue

        mode = entry.get("build_mode")
        if mode is None or mode == "pacman":
            plans.append(RevertPlan(target, "skip", None, None,
                                    "already a stock repo package"))
        elif profile.is_optimized_build_mode(mode) and not is_source_build_mode(mode):
            # bolt_llvm is "optimized" but keeps the stock name (3.5.0-B31), so
            # it reverts like the plain source build it is, below.
            stock = entry.get("origin_pkgbase") or target_name
            if profile.rename_mode_for_build_mode(mode) == "conflict":
                # Each renamed member declares provides/conflicts for its own
                # stock name; one `pacman -S <stock names>` atomically swaps the
                # whole installed set in (no pre-remove — that would break
                # reverse deps depending on the provided stock names).
                members = _conflict_members(entries, target_name, installed)
                if not _is_split(entries, target_name):
                    # Not split: origin_pkgbase is authoritative. A lone
                    # *installed* member of a split set keeps its own stock
                    # name, never the pkgbase (3.5.0-B62).
                    members = [(target_name, stock)]
                # Name the member whose stock name is the pkgbase (mesa-sysforge),
                # not whichever sibling the reverse lookup happened to land on.
                primary = next((n for n, st in members if st == stock), target_name)
                renamed = ", ".join(n for n, _ in members)
                plans.append(RevertPlan(
                    target, "replace", primary, stock,
                    f"reinstall stock {', '.join(st for _, st in members)} "
                    f"(atomically replaces conflict-mode {renamed})",
                    members))
            else:  # coexist — renamed build genuinely coexists with stock
                plans.append(RevertPlan(
                    target, "derename", target_name, stock,
                    f"remove renamed {target_name}, reinstall stock {stock}"))
        else:  # plain source_built — installed under the stock name
            # Reinstall the member itself, never its pkgbase: `pacman -S mesa`
            # leaves a source-built vulkan-radeon in place (3.5.0-B62). Naming
            # the pkgbase reverts every installed member, as forget does.
            if target_name == entry.get("pkgbase") and _is_split(entries, target_name):
                members = _conflict_members(entries, target_name, installed)
            else:
                members = [(target_name, target_name)]
            names = [n for n, _ in members]
            plans.append(RevertPlan(target, "reinstall", target_name, target_name,
                                    f"reinstall repo {', '.join(names)}", members))
    return plans


def _is_split(entries: dict, key: str) -> bool:
    """Whether ``key`` shares its recorded pkgbase with another entry."""
    pkgbase = entries[key].get("pkgbase")
    return bool(pkgbase) and sum(1 for e in entries.values()
                                 if e.get("pkgbase") == pkgbase) > 1


def apply_reverts(actionable: list, state_dir) -> int:
    """Carry out ``actionable`` plans (none ``skip``); return an exit code.

    The one home for a revert's pacman steps, forget and reconcile, shared by
    ``revert-to-stock`` and ``self-uninstall``. Stops at the first failure
    (exit 1, nothing changed for that target); raises ``RuntimeError`` when a
    derename removed a package but could not reinstall its stock one, so a
    caller's sentinel keeps its recovery.
    """
    store_names = profile_store_names(
        state_dir, [p.pkgname for p in actionable if p.pkgname])
    for p in actionable:
        if p.action == "derename":
            # Coexist rename: remove the renamed build first, then reinstall
            # stock. Name the step that actually failed so a remove-step
            # failure doesn't wrongly claim the system was left bare.
            try:
                pacman.remove_pkgs([p.pkgname])
            except subprocess.CalledProcessError as exc:
                _revert_log.error(
                    f"[revert] {p.target}: removal of {p.pkgname} failed "
                    f"({exc}); nothing changed")
                _revert_log.error("[revert] stopping: remaining targets not processed")
                return 1
            try:
                pacman.reinstall_repo_pkgs([p.stock_pkg])
            except subprocess.CalledProcessError as exc:
                # Raise, not return: the package is gone, so the sentinel
                # must keep its recovery for the next run (3.5.0-B93).
                raise RuntimeError(
                    f"[revert] {p.target}: removed {p.pkgname} but stock "
                    f"reinstall of {p.stock_pkg} FAILED ({exc}); system "
                    f"left without this package; run "
                    f"`sudo pacman -S {p.stock_pkg}` to recover. Remaining "
                    "targets not processed") from exc
        elif p.action == "replace":
            # Conflict-mode: one atomic `pacman -S` over every installed
            # member; on failure nothing changed.
            stocks = [st for _, st in p.members]
            try:
                pacman.reinstall_repo_pkgs(stocks, replace=True)
            except subprocess.CalledProcessError as exc:
                _revert_log.error(
                    f"[revert] {p.target}: reinstall of {', '.join(stocks)} "
                    f"failed ({exc}); nothing changed")
                _revert_log.error("[revert] stopping: remaining targets not processed")
                return 1
            forget_packages(state_dir, [n for n, _ in p.members],
                            opt_out=VIA_REVERT)
            continue
        else:  # "reinstall" (plain) — one atomic `pacman -S`; on failure
               # nothing changed.
            names = [n for n, _ in p.members] or [p.stock_pkg]
            try:
                pacman.reinstall_repo_pkgs(names)
            except subprocess.CalledProcessError as exc:
                _revert_log.error(
                    f"[revert] {p.target}: reinstall of {', '.join(names)} "
                    f"failed ({exc})")
                _revert_log.error("[revert] stopping: remaining targets not processed")
                return 1
            forget_packages(state_dir, names, opt_out=VIA_REVERT)
            continue
        # forget this entry so `update` stops rebuilding it
        forget_packages(state_dir, [p.pkgname], opt_out=VIA_REVERT)

    # Demote any that pacman now owns (belt-and-suspenders alongside forget).
    bs = BuildState(state_dir)
    if install_reconcile.consume_external_installs(bs).changed:
        bs.save()
    emit_orphaned_profiles(store_names, lambda line: _revert_log.ui(f"[revert] {line}"))
    return 0


# ---------------------------------------------------------------------------
# pacman.conf IgnoreGroup (setup adds it, self-uninstall removes it)
# ---------------------------------------------------------------------------

PACMAN_CONF = Path("/etc/pacman.conf")
IGNORE_GROUP = "sf-build"

# Any uncommented IgnoreGroup line (case-insensitive key).
IGNOREGROUP_RE = re.compile(r"^[ \t]*IgnoreGroup[ \t]*=[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)


def has_ignore_group(conf_text: str) -> bool:
    """Whether sf-build is in an uncommented IgnoreGroup line."""
    return any(IGNORE_GROUP in m.group(1).split() for m in IGNOREGROUP_RE.finditer(conf_text))


def drop_ignore_group(conf_text: str) -> str:
    """``conf_text`` with sf-build taken out of every IgnoreGroup line.

    The reverse of ``setup``'s edit: other groups on the line stay, and a line
    left with none is dropped, so the file is what it was before.
    """
    def _drop(m: re.Match) -> str:
        kept = [g for g in m.group(1).split() if g != IGNORE_GROUP]
        if not kept:
            return "\0"  # marker: the whole line goes
        return m.group(0).replace(m.group(1), " ".join(kept))

    text = IGNOREGROUP_RE.sub(_drop, conf_text)
    return re.sub(r"^\0\n?", "", text, flags=re.MULTILINE)


def remove_ignore_group(path: Path = PACMAN_CONF) -> bool:
    """Take sf-build out of ``path``'s IgnoreGroup; True when it was there.

    Atomic and escalating, like ``setup``'s write. Raises ``RuntimeError`` when
    the line is there but the write fails, so the caller can stop.
    """
    try:
        text = path.read_text()
    except OSError:
        return False
    if not has_ignore_group(text):
        return False
    try:
        atomic_write.replace_file(path, drop_ignore_group(text), tag="SETUP")
    except OSError as exc:
        raise RuntimeError(
            f"could not remove {IGNORE_GROUP} from IgnoreGroup in {path}: {exc}") from exc
    return True
