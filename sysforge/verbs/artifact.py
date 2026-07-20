# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
artifact.py — CLI verbs over the user-owned artifact inventory.

Thin shells: all logic lives in ``primitives/artifacts.py``. Read-only verbs
carry no sentinel; the mutating ones (deploy/remove) set
``requires_sentinel = True`` and supply a ``journal_target``.
"""
from __future__ import annotations

from sysforge import log
from sysforge.primitives import artifacts, editor
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("ARTIFACT")


class ArtifactListVerb(Verb):
    """List managed artifacts, plus (with --unmanaged) discovery candidates."""

    name = "artifact-list"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        del args
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        del pre
        registry = artifacts.ArtifactRegistry(
            state_dir=getattr(args, "state_dir", None)
        )
        try:
            rows = artifacts.unified_rows(registry)
        except artifacts.ArtifactError as exc:
            # Includes ArtifactRegistryError (corrupt/unreadable registry):
            # surface the repair guidance, not a traceback.
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        if not rows:
            _log.ui("No managed artifacts.")
        else:
            _log.ui(f"{'STATUS':<9} {'OWNER':<9} {'CLASS':<13} NAME")
            for row in rows:
                _log.ui(
                    f"{row['status']:<9} {row['owner']:<9} "
                    f"{row['cls']:<13} {row['name']}"
                )

        on_path = artifacts.script_root_on_path()
        if on_path is False:
            root = artifacts.default_script_root()
            _log.warn(
                f"{root} is not on PATH — scripts deployed there will not be "
                f"runnable by name. Add it to PATH in your shell profile."
            )

        if getattr(args, "unmanaged", False):
            managed_dests = {r["dest"] for r in rows}
            cands = [
                c for c in artifacts.scan()
                if c.path not in managed_dests
                and c.owner != artifacts.OWNER_SYSFORGE
            ]
            _log.ui("")
            if not cands:
                _log.ui("No unmanaged candidates found.")
            else:
                _log.ui(f"Unmanaged candidates ({len(cands)}):")
                for c in cands:
                    suffix = "  [ownership unknown]" if (
                        c.owner == artifacts.OWNER_UNKNOWN
                    ) else ""
                    _log.ui(f"  {c.cls:<13} {c.path}{suffix}")
        return ExecResult(exit_code=0)


class ArtifactAdoptVerb(Verb):
    """Copy a live artifact into the managed set. Touches no live system file."""

    name = "artifact-adopt"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        del args
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        del pre
        registry = artifacts.ArtifactRegistry(
            state_dir=getattr(args, "state_dir", None)
        )
        try:
            art = artifacts.adopt(registry, args.path, cls=args.cls)
        except artifacts.ArtifactError as exc:
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        _log.ui(f"Adopted {art.name} ({art.cls}) from {art.dest}")
        return ExecResult(exit_code=0)


class ArtifactEditVerb(Verb):
    """Open the managed copy in an editor, then re-hash. Live file untouched."""

    name = "artifact-edit"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        del args
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        del pre
        registry = artifacts.ArtifactRegistry(
            state_dir=getattr(args, "state_dir", None)
        )
        try:
            entries = registry.load()
        except artifacts.ArtifactError as exc:
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        if args.name not in entries:
            _log.error(f"{args.name} is not managed")
            return ExecResult(exit_code=1)

        editor_bin, source = editor.resolve_editor()
        if not editor_bin:
            _log.error("no editor available (set $EDITOR or [ui].editor)")
            return ExecResult(exit_code=1)
        del source

        content_path = registry.content_path(args.name)
        rc = editor.run_tty_argv([editor_bin, str(content_path)])
        if rc != 0:
            _log.error(f"editor exited with status {rc}; not re-hashing")
            return ExecResult(exit_code=1)

        art = artifacts.rehash(registry, args.name)
        status = artifacts.status_of(registry, art)
        _log.ui(f"{art.name}: {status}")
        if status == artifacts.STATUS_PENDING:
            _log.ui(f"Run `sysforge artifact deploy {art.name}` to push the change.")
        return ExecResult(exit_code=0)


class ArtifactDeployVerb(Verb):
    """Push managed content to the live system. Mutating: sentinel-gated."""

    name = "artifact-deploy"
    requires_sentinel = True

    def journal_target(self, args) -> str | None:
        if getattr(args, "all", False):
            return "all"
        return getattr(args, "name", None)

    def pre_check(self, args) -> PreCheckResult:
        del args
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        del pre
        registry = artifacts.ArtifactRegistry(
            state_dir=getattr(args, "state_dir", None)
        )
        try:
            entries = registry.load()
        except artifacts.ArtifactError as exc:
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        names = sorted(entries) if getattr(args, "all", False) else [args.name]

        force = getattr(args, "force", False)
        adopt_live = getattr(args, "adopt_live", False)
        failed = 0
        deployed_classes: set[str] = set()
        for name in names:
            art = entries.get(name)
            # Read status before the mutation so an already-`ok` artifact is
            # reported as unchanged rather than as if we wrote it.
            was_ok = (
                art is not None
                and artifacts.status_of(registry, art) == artifacts.STATUS_OK
            )
            try:
                artifacts.deploy(
                    registry, name, force=force, adopt_live=adopt_live,
                )
                if was_ok and not force and not adopt_live:
                    _log.ui(f"{name}: ok (unchanged)")
                else:
                    _log.ui(f"Deployed {name}")
                # The PATH warning is about where the script lives, not whether
                # this run rewrote it — a current-but-off-PATH script is still
                # unrunnable by name — so a successfully-processed script counts
                # even when it was already ok.
                if art is not None:
                    deployed_classes.add(art.cls)
            except artifacts.ArtifactError as exc:
                _log.error(str(exc))
                failed += 1

        # Warn once per run, not per artifact, and only when a script actually
        # landed — the problem is concrete at deploy time (you just installed
        # something you expect to run by name, and it won't).
        if artifacts.CLASS_SCRIPT in deployed_classes and artifacts.script_root_on_path() is False:
            root = artifacts.default_script_root()
            _log.warn(
                f"{root} is not on PATH — the script you just deployed will "
                f"not be runnable by name. Add it to PATH in your shell profile."
            )
        return ExecResult(exit_code=1 if failed else 0)


class ArtifactRemoveVerb(Verb):
    """Remove an artifact from the live system. Mutating: sentinel-gated."""

    name = "artifact-remove"
    requires_sentinel = True

    def journal_target(self, args) -> str | None:
        return getattr(args, "name", None)

    def pre_check(self, args) -> PreCheckResult:
        del args
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        del pre
        registry = artifacts.ArtifactRegistry(
            state_dir=getattr(args, "state_dir", None)
        )
        try:
            entries = registry.load()
        except artifacts.ArtifactError as exc:
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        art = entries.get(args.name)
        if art is None:
            _log.error(f"{args.name} is not managed")
            return ExecResult(exit_code=1)
        if art.cls == artifacts.CLASS_HOOK:
            _log.warn(
                f"{art.name} is a pacman hook — removing it changes what "
                "happens on your next pacman transaction."
            )
        try:
            artifacts.remove(
                registry, args.name,
                purge=args.purge, force=getattr(args, "force", False),
            )
        except artifacts.ArtifactError as exc:
            _log.error(str(exc))
            return ExecResult(exit_code=1)
        _log.ui(f"Removed {args.name}" + (" (purged)" if args.purge else ""))
        return ExecResult(exit_code=0)
