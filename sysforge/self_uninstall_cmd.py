# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""self_uninstall_cmd.py — the ``sysforge self-uninstall`` verb (3.5.0-F19).

Remove SysForge from the system in one command, in the order that leaves
nothing half-done:

1. revert every source-built package a repo carries (``revert-to-stock``'s
   own steps, :func:`verbs.shared.apply_reverts`); a build no repo carries (an
   AUR or local package, a custom kernel) stays installed and keeps working,
   since there is nothing to revert it to;
2. take ``sf-build`` out of ``IgnoreGroup`` in ``/etc/pacman.conf`` and the
   local repo block out of the sandbox chroot's ``pacman.conf``;
3. remove the sysforge package, then its state, caches, profiles and config
   and the ``sysforge`` group, in one escalated command.

Step 3 runs last and as one ``sudo sh -c``: once pacman has removed the
package, this process must not import anything more from it. A failed revert
stops the run before step 2, so a partial removal never strands a package.
``--keep-data`` stops after the package (pacman keeps edited config as
``.pacsave``); ``--dry-run`` prints the plan.
"""
from __future__ import annotations

import grp
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import sysforge
from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import build_sandbox, local_repo, pacman, paths, prompt
from sysforge.primitives import sudo_session
from sysforge.primitives.build_state import BuildState
from sysforge.primitives.config import load_sysforge_toml
from sysforge.primitives.privilege import privileged_argv
from sysforge.primitives.resource_guard import lift_for_child
from sysforge.primitives.stage_sentinel import Recovery, sentinel_scope
from sysforge.verbs.shared import (
    IGNORE_GROUP,
    PACMAN_CONF,
    RevertPlan,
    apply_reverts,
    has_ignore_group,
    plan_revert,
    remove_ignore_group,
)
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("SELF-UNINSTALL")

_GROUP = "sysforge"


@dataclass
class SelfUninstallPlan:
    """Everything the run will do (produced by :func:`plan_self_uninstall`)."""

    reverts: list[RevertPlan] = field(default_factory=list)
    # Source-built packages left installed, with the reason.
    kept: list[tuple[str, str]] = field(default_factory=list)
    # The package that installed this sysforge; None when it runs from a checkout.
    package: str | None = None
    ignore_group: bool = False
    chroot_conf: Path | None = None
    system_dirs: list[Path] = field(default_factory=list)
    user_dirs: list[Path] = field(default_factory=list)
    group: bool = False


def _stocks(plan: RevertPlan) -> list[str]:
    return [st for _, st in plan.members] or [plan.stock_pkg or plan.target]


def plan_self_uninstall(bs: BuildState, *, in_repo, owner: str | None,
                        installed: set | None = None,
                        pacman_conf: Path = PACMAN_CONF,
                        chroot_conf: Path | None = None,
                        system_dirs: list[Path] | None = None,
                        user_dirs: list[Path] | None = None,
                        group: bool = False) -> SelfUninstallPlan:
    """Decide what to revert, keep and remove. Reads, never writes.

    ``in_repo(name)`` says whether a sync repo carries ``name``. A split set
    is planned once, from whichever member comes first; ``installed``
    (pkgnames) as for :func:`plan_revert`.
    """
    plan = SelfUninstallPlan(package=owner, group=group)
    built = sorted(n for n, e in bs.all_packages().items()
                   if (e or {}).get("build_mode") not in (None, "pacman"))
    covered: set[str] = set()
    for rp in plan_revert(bs, built, installed):
        if rp.action == "skip" or rp.target in covered:
            continue
        names = [n for n, _ in rp.members] or [rp.pkgname or rp.target]
        covered.update(names)
        missing = [st for st in _stocks(rp) if not in_repo(st)]
        if missing:
            for n in names:
                plan.kept.append((n, f"no repo package {', '.join(missing)} to go back to"))
        else:
            plan.reverts.append(rp)
    try:
        plan.ignore_group = has_ignore_group(pacman_conf.read_text())
    except OSError:
        plan.ignore_group = False
    if chroot_conf is not None and local_repo.has_chroot_section(chroot_conf):
        plan.chroot_conf = chroot_conf
    plan.system_dirs = [d for d in (system_dirs or []) if d.exists()]
    plan.user_dirs = [d for d in (user_dirs or []) if d.exists()]
    return plan


def _owning_package() -> str | None:
    try:
        return pacman.owners_of_paths([sysforge.__file__]).get(sysforge.__file__)
    except Exception:  # noqa: BLE001 — no pacman, or a checkout: nothing owns it
        return None


def _group_exists() -> bool:
    try:
        grp.getgrnam(_GROUP)
        return True
    except KeyError:
        return False


def _chroot_conf() -> Path | None:
    policy = build_sandbox.resolve_sandbox(load_sysforge_toml().get("security", {}))
    if policy.chroot_dir is None:
        return None
    return policy.chroot_dir / "root" / "etc" / "pacman.conf"


def removal_script(plan: SelfUninstallPlan, *, keep_data: bool) -> str | None:
    """The one ``sh -c`` script for step 3, or None when there is nothing to run.

    Pure. ``-Rns`` removes the package's config too; ``--keep-data`` uses
    ``-Rs`` so pacman keeps edited config as ``.pacsave``.
    """
    steps = []
    if plan.package:
        flags = "-Rs" if keep_data else "-Rns"
        steps.append(f"pacman {flags} --noconfirm -- {shlex.quote(plan.package)}")
    if not keep_data:
        if plan.system_dirs:
            steps.append("rm -rf -- " + " ".join(shlex.quote(str(d)) for d in plan.system_dirs))
        if plan.group:
            steps.append(f"groupdel {_GROUP}")
    return " && ".join(steps) or None


def _describe(plan: SelfUninstallPlan, *, keep_data: bool) -> list[str]:
    lines = []
    if plan.reverts:
        lines.append("Revert to the repo version:")
        lines += [f"  {rp.target}: {rp.reason}" for rp in plan.reverts]
    if plan.kept:
        lines.append("Keep installed (built by sysforge, kept working):")
        lines += [f"  {n}: {why}" for n, why in plan.kept]
    if plan.ignore_group:
        lines.append(f"Remove {IGNORE_GROUP} from IgnoreGroup in {PACMAN_CONF}")
    if plan.chroot_conf:
        lines.append(f"Remove [{local_repo.REPO_NAME}] from {plan.chroot_conf}")
    if plan.package:
        how = "keeping edited config as .pacsave" if keep_data else "with its config"
        lines.append(f"Remove the {plan.package} package, {how}")
    else:
        lines.append("sysforge is not installed as a package (a checkout?): not removing it")
    if not keep_data:
        for d in plan.system_dirs + plan.user_dirs:
            lines.append(f"Delete {d}")
        if plan.group:
            lines.append(f"Delete the {_GROUP} group")
    return lines


class SelfUninstallVerb(Verb):
    """Revert sysforge's builds to stock, then remove sysforge."""

    name = "self-uninstall"
    # Its own sentinel covers only the reverts: after them the state dir, where
    # a sentinel and a run log live, is deleted with the rest.
    requires_sentinel = False
    wants_run_log = False

    @classmethod
    def add_parser(cls, sub):
        p = sub.add_parser("self-uninstall",
            help="Revert the packages sysforge built to the repo versions, then remove "
                 "sysforge, its state and its config.")
        p.add_argument("--keep-data", action="store_true", dest="keep_data",
            help="Remove only the package: keep edited config (as .pacsave), state, "
                 "PGO profiles and the sysforge group.")
        p.add_argument("--dry-run", action="store_true", dest="dry_run",
            help="Show the plan without changing anything.")
        p.add_argument("--yes", action="store_true", dest="yes",
            help="Do it without asking (needed without a terminal).")
        p.add_argument("--state-dir", metavar="DIR", dest="state_dir",
            help="Override state directory.")
        p.set_defaults(verb_cls=cls)
        return p

    def journal_target(self, args) -> str | None:
        del args
        return "mode:self-uninstall"

    def pre_check(self, args) -> PreCheckResult:
        state_dir, _ = resolve_state_dir(args.state_dir, provision=False)
        plan = plan_self_uninstall(
            BuildState(state_dir),
            in_repo=pacman.get_sync_pkgnames().__contains__,
            owner=_owning_package(),
            installed=set(pacman.get_all_installed_packages()),
            chroot_conf=_chroot_conf(),
            system_dirs=list(paths.PACKAGE_DIRS),
            user_dirs=[paths.USER_CACHE_DIR, paths.USER_DATA_DIR,
                       paths.USER_CONFIG_DIR, paths.USER_STATE_DIR],
            group=_group_exists(),
        )
        return PreCheckResult(ctx={"plan": plan, "state_dir": state_dir})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        plan: SelfUninstallPlan = pre.ctx["plan"]
        for line in _describe(plan, keep_data=args.keep_data):
            _log.ui(line)
        if args.dry_run:
            _log.ui("dry run: nothing changed")
            return ExecResult(exit_code=0)

        if not args.yes:
            if not prompt.is_interactive():
                _log.error("refusing without a terminal; pass --yes")
                return ExecResult(exit_code=2)
            ans = prompt.prompt_choice(
                "Remove sysforge as above? [y/N] ", ["y", "n"],
                default="n", eof_default="n", retry_on_invalid=False, tag="self-uninstall")
            if ans != "y":
                _log.ui("aborted: nothing changed")
                return ExecResult(exit_code=2)
        if not sudo_session.authenticate():
            _log.error("sudo was not authorized, so nothing was changed")
            return ExecResult(exit_code=1)

        # 1. Reverts, under a sentinel that reinstalls the stock packages if
        # one is interrupted between its remove and its reinstall.
        if plan.reverts:
            stocks = sorted({st for rp in plan.reverts for st in _stocks(rp)})
            with sentinel_scope(pre.ctx["state_dir"], self.name,
                                recovery=Recovery.pacman_sync(stocks),
                                retry_cmd="sysforge self-uninstall"):
                rc = apply_reverts(plan.reverts, pre.ctx["state_dir"])
            if rc:
                _log.error("stopping: a revert failed, so sysforge stays installed; "
                           "fix it and run `sysforge self-uninstall` again")
                return ExecResult(exit_code=rc)

        # 2. The two pacman.conf changes sysforge made.
        try:
            if plan.ignore_group:
                remove_ignore_group(PACMAN_CONF)
            if plan.chroot_conf is not None:
                local_repo.remove_chroot_section(
                    build_sandbox.SandboxPolicy(enabled=False,
                                                chroot_dir=plan.chroot_conf.parents[2]))
        except Exception as exc:  # noqa: BLE001 — any failure here stops before removal
            _log.error(f"stopping before removing sysforge: {exc}")
            return ExecResult(exit_code=1)

        # 3. The package, then its data, in one escalation. Nothing below may
        # import from sysforge: pacman has just deleted it.
        script = removal_script(plan, keep_data=args.keep_data)
        if script is not None:
            argv = privileged_argv(["sh", "-c", script])
            result = subprocess.run(argv, check=False, preexec_fn=lift_for_child)  # noqa: TID251 — the last command; no sysforge code may run after it
            if result.returncode != 0:
                print(f"sysforge self-uninstall: `{script}` failed (exit {result.returncode})")
                return ExecResult(exit_code=1)
        if not args.keep_data:
            for d in plan.user_dirs:
                shutil.rmtree(d, ignore_errors=True)
        print("sysforge is removed." if plan.package else
              "sysforge's changes are undone; remove the checkout yourself.")
        return ExecResult(exit_code=0)

