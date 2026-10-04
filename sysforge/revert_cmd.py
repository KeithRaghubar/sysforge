# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""revert_cmd.py — the ``sysforge revert-to-stock`` verb.

Undo a source-built/optimized package back to the official repo version.
The revert action branches on whether the build earned the ``-sysforge``
rename (``profile.is_optimized_build_mode``) and, for renamed builds, on the
rename mode (``profile.rename_mode_for_build_mode``):

  * plain ``source_built`` (stock name)       → reinstall the repo package in
                                                 place (``reinstall``).
  * optimized, ``conflict`` rename (mesa/pgo) → reinstall the stock name of
                                                 every installed member of the
                                                 split set in ONE ``pacman -S``
                                                 (``replace``): each renamed
                                                 member declares
                                                 ``provides``/``conflicts`` for
                                                 its own stock name, so pacman
                                                 removes the ``-sysforge`` builds
                                                 and installs stock atomically —
                                                 a pre-remove would break reverse
                                                 deps. ``--ask=4`` confirms the
                                                 conflict removal ``--noconfirm``
                                                 would refuse (3.3.0-B6).
  * optimized, ``coexist`` rename (kernel FDO) → remove the renamed package,
                                                 then reinstall the stock
                                                 ``origin_pkgbase`` (``derename``);
                                                 the two genuinely coexist so a
                                                 removal is needed.

All paths then ``state forget`` the entry and run
``BuildState.reconcile_external_installs`` so ``update`` stops rebuilding it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import install_reconcile, journal, pacman, profile, prompt
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.pkgbuild_patcher import RENAME_SUFFIX
from sysforge.verbs.shared import (
    emit_orphaned_profiles,
    forget_packages,
    profile_store_names,
)
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("REVERT")


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
                                    "not tracked by sysforge — already stock"))
            continue

        mode = entry.get("build_mode")
        if mode is None or mode == "pacman":
            plans.append(RevertPlan(target, "skip", None, None,
                                    "already a stock repo package"))
        elif profile.is_optimized_build_mode(mode):
            stock = entry.get("origin_pkgbase") or target_name
            if profile.rename_mode_for_build_mode(mode) == "conflict":
                # Each renamed member declares provides/conflicts for its own
                # stock name; one `pacman -S <stock names>` atomically swaps the
                # whole installed set in (no pre-remove — that would break
                # reverse deps depending on the provided stock names).
                members = _conflict_members(entries, target_name, installed)
                if len(members) == 1:  # not split: origin_pkgbase is authoritative
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
            plans.append(RevertPlan(target, "reinstall", target_name,
                                    entry.get("pkgbase") or target_name,
                                    f"reinstall repo {target_name}"))
    return plans


class RevertToStockVerb(Verb):
    """Undo a source-built/optimized package back to the repo version."""

    name = "revert-to-stock"
    wants_run_log = True
    requires_sentinel = True

    def journal_target(self, args) -> str | None:
        return journal.pkg_target(args.packages)

    def pre_check(self, args) -> PreCheckResult:
        state_dir, _source = resolve_state_dir(getattr(args, "state_dir", None))
        bs = BuildState(state_dir)
        plans = plan_revert(bs, list(args.packages))
        return PreCheckResult(ctx={"plans": plans, "state_dir": state_dir})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        plans = pre.ctx["plans"]
        actionable = [p for p in plans if p.action != "skip"]
        for p in plans:
            if p.action == "skip":
                _log.ui(f"[revert] {p.target}: {p.reason} — nothing to do")
            else:
                _log.ui(f"[revert] {p.target}: {p.reason}")
        if not actionable:
            return ExecResult(exit_code=0)

        if getattr(args, "dry_run", False):
            _log.ui("[revert] dry-run — no changes made")
            return ExecResult(exit_code=0)

        if not getattr(args, "force", False):
            if not prompt.is_interactive():
                _log.error("[revert] refusing without a TTY; pass --force")
                return ExecResult(exit_code=2)
            ans = prompt.prompt_choice(
                "Proceed with revert? [y/N] ", ["y", "n"],
                default="n", eof_default="n", retry_on_invalid=False, tag="revert")
            if ans != "y":
                _log.ui("[revert] aborted")
                return ExecResult(exit_code=2)

        store_names = profile_store_names(
            pre.ctx["state_dir"], [p.pkgname for p in actionable if p.pkgname])
        for p in actionable:
            if p.action == "derename":
                # Coexist rename: remove the renamed build first, then reinstall
                # stock. Name the step that actually failed so a remove-step
                # failure doesn't wrongly claim the system was left bare.
                try:
                    pacman.remove_pkgs([p.pkgname])
                except subprocess.CalledProcessError as exc:
                    _log.error(
                        f"[revert] {p.target}: removal of {p.pkgname} failed "
                        f"({exc}); nothing changed")
                    _log.error("[revert] stopping — remaining targets not processed")
                    return ExecResult(exit_code=1)
                try:
                    pacman.reinstall_repo_pkgs([p.stock_pkg])
                except subprocess.CalledProcessError as exc:
                    _log.error(
                        f"[revert] {p.target}: removed {p.pkgname} but stock "
                        f"reinstall of {p.stock_pkg} FAILED ({exc}) — system "
                        f"left without this package; run "
                        f"`sudo pacman -S {p.stock_pkg}` to recover")
                    _log.error("[revert] stopping — remaining targets not processed")
                    return ExecResult(exit_code=1)
            elif p.action == "replace":
                # Conflict-mode: one atomic `pacman -S` over every installed
                # member; on failure nothing changed.
                stocks = [st for _, st in p.members]
                try:
                    pacman.reinstall_repo_pkgs(stocks, replace=True)
                except subprocess.CalledProcessError as exc:
                    _log.error(
                        f"[revert] {p.target}: reinstall of {', '.join(stocks)} "
                        f"failed ({exc}); nothing changed")
                    _log.error("[revert] stopping — remaining targets not processed")
                    return ExecResult(exit_code=1)
                forget_packages(pre.ctx["state_dir"], [n for n, _ in p.members])
                continue
            else:  # "reinstall" (plain) — one atomic `pacman -S`; on failure
                   # nothing changed.
                try:
                    pacman.reinstall_repo_pkgs([p.stock_pkg])
                except subprocess.CalledProcessError as exc:
                    _log.error(
                        f"[revert] {p.target}: reinstall of {p.stock_pkg} "
                        f"failed ({exc})")
                    _log.error("[revert] stopping — remaining targets not processed")
                    return ExecResult(exit_code=1)
            # forget this entry so `update` stops rebuilding it
            forget_packages(pre.ctx["state_dir"], [p.pkgname])

        # Demote any that pacman now owns (belt-and-suspenders alongside forget).
        bs = BuildState(pre.ctx["state_dir"])
        demoted = bs.reconcile_external_installs(
            install_reconcile.external_install_targets())
        if demoted:
            bs.save()
        emit_orphaned_profiles(store_names, lambda line: _log.ui(f"[revert] {line}"))
        return ExecResult(exit_code=0)


