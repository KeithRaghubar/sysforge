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


from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import journal, pacman, prompt  # noqa: F401 — pacman: tests patch it here
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.stage_sentinel import Recovery
from sysforge.verbs.shared import (  # noqa: F401 — re-exported: callers and tests reach them here
    RevertPlan,
    _conflict_members,
    _is_split,
    _stock_name,
    apply_reverts,
    plan_revert,
)
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("REVERT")


class RevertToStockVerb(Verb):
    """Undo a source-built/optimized package back to the repo version."""

    name = "revert-to-stock"
    wants_run_log = True
    requires_sentinel = True

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser(
            "revert-to-stock",
            help="Undo a source-built/optimized package back to the repo version; "
                 "update will not re-adopt it until you `build` or `packages add` it.",
        )
        p.add_argument("packages", nargs="+", metavar="PKG",
            help="Package name(s) to revert (stock or -sysforge name).")
        p.add_argument("--yes", action="store_true", dest="yes",
            help="Skip the confirmation prompt.")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show the planned revert without making changes.")
        p.set_defaults(verb_cls=cls)
        return p

    def journal_target(self, args) -> str | None:
        return journal.pkg_target(args.packages)

    def sentinel_recovery(self, args, pre: PreCheckResult) -> "Recovery | None":
        """Reinstall every stock package the revert was putting back: an
        interrupted or failed derename leaves the package removed (3.5.0-B93)."""
        del args
        stocks = sorted({st for p in pre.ctx.get("plans", []) if p.action != "skip"
                         for st in ([st for _, st in p.members] or [p.stock_pkg])
                         if st})
        return Recovery.pacman_sync(stocks) if stocks else None

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
                _log.ui(f"[revert] {p.target}: {p.reason}; nothing to do")
            else:
                _log.ui(f"[revert] {p.target}: {p.reason}")
        if not actionable:
            return ExecResult(exit_code=0)

        if getattr(args, "dry_run", False):
            _log.ui("[revert] dry-run: no changes made")
            return ExecResult(exit_code=0)

        if not getattr(args, "yes", False):
            if not prompt.is_interactive():
                _log.error(f"[revert] refusing ({prompt.no_prompt_reason()}); pass --yes")
                return ExecResult(exit_code=2)
            ans = prompt.prompt_choice(
                "Proceed with revert? [y/N] ", ["y", "n"],
                default="n", eof_default="n", retry_on_invalid=False, tag="revert")
            if ans != "y":
                _log.ui("[revert] aborted")
                return ExecResult(exit_code=2)

        return ExecResult(exit_code=apply_reverts(actionable, pre.ctx["state_dir"]))


