# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""uninstall_cmd.py — the ``sysforge uninstall`` verb.

Remove packages and, for a sysforge-tracked package, demote it out of the
build-state authority so ``sysforge update`` stops rebuilding it. A naive
``pacman -R`` is wrong here on two counts: it leaves the ``build_state.toml``
record in place, and it doesn't know an optimized build may be installed under
a ``-sysforge`` renamed name. Resolution and demotion reuse the single homes
(``install_reconcile.resolve_installed_name`` + ``cmd_state_forget`` +
``reconcile_external_installs``) — no parallel path.
"""
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass

from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import install_reconcile, journal, pacman
from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_forget
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("UNINSTALL")


@dataclass
class UninstallItem:
    """One target resolved to its installed name + tracking status."""

    target: str
    installed_name: str
    tracked: bool


def plan_uninstall(bs: BuildState, targets: list) -> list:
    """Resolve each target to its installed name + tracked flag. Pure."""
    entries = bs.all_packages()
    items: list[UninstallItem] = []
    for target in targets:
        installed = install_reconcile.resolve_installed_name(bs, target)
        items.append(UninstallItem(target, installed, installed in entries))
    return items


class UninstallVerb(Verb):
    """Remove package(s) and demote any sysforge-tracked ones."""

    name = "uninstall"
    wants_run_log = True
    requires_sentinel = True

    def journal_target(self, args) -> str | None:
        return journal.pkg_target(args.packages)

    def pre_check(self, args) -> PreCheckResult:
        state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
        bs = BuildState(state_dir)
        items = plan_uninstall(bs, list(args.packages))
        return PreCheckResult(ctx={"items": items, "state_dir": state_dir})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        items = pre.ctx["items"]
        for it in items:
            renamed = (
                "" if it.installed_name == it.target else f" (installed as {it.installed_name})"
            )
            tag = "sysforge-tracked" if it.tracked else "repo/untracked"
            _log.ui(f"[uninstall] {it.target}{renamed} — {tag}")

        names = [it.installed_name for it in items]
        try:
            # Interactive: pacman prints its own transaction + confirmation.
            pacman.uninstall_pkgs(names, extra_flags=list(getattr(args, "pacman_flags", []) or []))
        except subprocess.CalledProcessError as exc:
            _log.error(f"[uninstall] pacman removal failed ({exc}); nothing demoted")
            return ExecResult(exit_code=1)

        # Demote tracked packages: forget their build_state records (handles
        # split-package siblings by pkgbase), then reconcile as belt-and-braces.
        tracked = [it.installed_name for it in items if it.tracked]
        if tracked:
            cmd_state_forget(argparse.Namespace(pkgnames=tracked, state_dir=pre.ctx["state_dir"]))
            bs = BuildState(pre.ctx["state_dir"])
            demoted = bs.reconcile_external_installs(install_reconcile.external_install_targets())
            if demoted:
                bs.save()
        return ExecResult(exit_code=0)
