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
  * optimized, ``conflict`` rename (mesa/pgo) → reinstall the stock
                                                 ``origin_pkgbase`` ALONE
                                                 (``replace``): the renamed
                                                 package declares
                                                 ``provides``/``conflicts`` for
                                                 the stock name, so ``pacman -S``
                                                 detects the conflict, removes
                                                 the ``-sysforge`` build and
                                                 installs stock atomically in one
                                                 transaction — a pre-remove would
                                                 break reverse deps.
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
from dataclasses import dataclass

from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import install_reconcile, pacman, profile, prompt
from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_forget
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


def plan_revert(bs: BuildState, targets: list) -> list:
    """Resolve each target to a :class:`RevertPlan`. Pure — no mutation."""
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
                # Renamed build declares provides/conflicts for the stock name;
                # `pacman -S stock` atomically swaps it in (no pre-remove — that
                # would break reverse deps depending on the provided stock name).
                plans.append(RevertPlan(
                    target, "replace", target_name, stock,
                    f"reinstall stock {stock} (atomically replaces "
                    f"conflict-mode {target_name})"))
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
    requires_sentinel = True

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
            else:  # "reinstall" (plain) or "replace" (conflict-mode) — one
                   # atomic `pacman -S`; on failure nothing changed.
                try:
                    pacman.reinstall_repo_pkgs([p.stock_pkg])
                except subprocess.CalledProcessError as exc:
                    _log.error(
                        f"[revert] {p.target}: reinstall of {p.stock_pkg} "
                        f"failed ({exc})")
                    _log.error("[revert] stopping — remaining targets not processed")
                    return ExecResult(exit_code=1)
            # forget this entry so `update` stops rebuilding it
            fa = _forget_args(args, p.pkgname)
            cmd_state_forget(fa)

        # Demote any that pacman now owns (belt-and-suspenders alongside forget).
        bs = BuildState(pre.ctx["state_dir"])
        demoted = bs.reconcile_external_installs(
            install_reconcile.external_install_targets())
        if demoted:
            bs.save()
        return ExecResult(exit_code=0)


def _forget_args(args, pkgname: str):
    import argparse
    ns = argparse.Namespace(pkgnames=[pkgname])
    for k in ("dry_run", "state_dir"):
        if hasattr(args, k):
            setattr(ns, k, getattr(args, k))
    return ns
