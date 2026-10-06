# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
verbs/base.py — protocol and result types for the CLI verb framework.

Every top-level CLI verb is a :class:`Verb` subclass with three phases:

  1. ``pre_check(args) -> PreCheckResult`` — validate, load config, run
     preflights. No state mutation. Returns one of three terminal shapes:
       • proceed: ``skip_reason=None, blocker=None`` → run execute next.
       • skip:    ``skip_reason="…"``               → exit 0, log reason.
       • block:   ``blocker="…", exit_code=N``      → exit N, log message.

  2. ``execute(args, pre) -> ExecResult`` — do the work. May mutate state.
     ``ExecResult.exit_code`` propagates to the process; ``artifacts`` is
     a free-form dict for ``post_validate`` to read.

  3. ``post_validate(args, pre, result) -> None`` — verify post-conditions,
     write final state, raise :class:`RuntimeError` on failure. Default is
     a no-op (read-only verbs return ``ExecResult()`` and leave this).

Verbs that mutate the live system set ``requires_sentinel = True``; the
runner wraps ``execute + post_validate`` in
:func:`~sysforge.primitives.stage_sentinel.sentinel_scope` so a crash or
interrupt mid-mutation leaves a recovery sentinel for the next run.

Each verb also owns its argparse surface: :meth:`Verb.add_parser` registers the
command word and its flags, next to the code that reads ``args.<flag>``. A
namespace verb (``doctor``, ``state``, …) is a :class:`VerbGroup` over its leaf
verbs. ``verbs/registry.py`` lists them in order and assembles the parser
(3.2.0-F7).
"""
from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreCheckResult:
    """Outcome of a verb's :meth:`Verb.pre_check` phase.

    Exactly one of three terminal states:
      • Normal: ``skip_reason=None, blocker=None`` → proceed to execute.
      • Skip:   ``skip_reason="…"``                → success short-circuit (exit 0).
      • Block:  ``blocker="…", exit_code=N``       → failure short-circuit (exit N).

    ``ctx`` is a free-form dict that ``pre_check`` may populate for
    ``execute`` / ``post_validate`` to consume. The runner does not
    inspect its contents.
    """

    ctx: dict[str, Any] = field(default_factory=dict)
    skip_reason: str | None = None
    blocker: str | None = None
    exit_code: int = 1  # used only when ``blocker`` is set


@dataclass
class ExecResult:
    """Outcome of a verb's :meth:`Verb.execute` phase.

    ``exit_code`` becomes the process exit code (when ``post_validate``
    passes). ``artifacts`` is a free-form dict for ``post_validate`` to
    read; the runner does not inspect it.
    """

    exit_code: int = 0
    artifacts: dict[str, Any] = field(default_factory=dict)


class Verb(ABC):
    """A standardized CLI verb dispatched by :func:`sysforge.verbs.runner.run_verb`.

    Subclasses set:
      • ``name``               — short identifier (used for logger tag and
        sentinel stage name; defaults to the lowercase class name minus
        ``Verb`` if unset).
      • ``requires_sentinel``  — ``True`` for verbs that mutate the live
        system. When set, the runner wraps execute+post_validate in
        :func:`~sysforge.primitives.stage_sentinel.sentinel_scope`.

    and implement the three phase methods, plus :meth:`add_parser` for the
    argparse surface.
    """

    name: str = ""
    requires_sentinel: bool = False
    #: Opt in to a consolidated run log at ``sysforge-<name>.log`` (opened and
    #: closed by ``verbs.runner._consolidated_log``: purged before ``execute``,
    #: kept ``persist=True`` after, skipped on dry-run). Substantial verbs set
    #: this True; pure printers / read-only reports leave it False. Verbs that
    #: need a non-derived basename override ``unified_log_basename`` instead.
    wants_run_log: bool = False

    @classmethod
    def add_parser(cls, sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
        """Register this verb's subparser on ``sub`` and return it.

        Adds the command word, its arguments and ``set_defaults(verb_cls=cls)``,
        which is how ``cli._dispatch`` finds the verb. Called once per parser
        build by ``verbs.registry.build_parser``.
        """
        raise NotImplementedError(f"{cls.__name__} does not define add_parser")

    @abstractmethod
    def pre_check(self, args) -> PreCheckResult:
        """Validate args, load config, run preflights. No state mutation."""

    @abstractmethod
    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        """Do the verb's work. May mutate state."""

    def post_validate(
        self,
        args,
        pre: PreCheckResult,
        result: ExecResult,
    ) -> None:
        """Verify post-conditions; raise :class:`RuntimeError` on failure.

        Default: no-op. Read-only verbs leave this alone; verbs that
        record state typically write here so a failed write surfaces as
        a verb failure (sentinel stays in place).
        """
        del args, pre, result
        return None

    def unified_log_basename(self, args) -> str | None:
        """Basename of the consolidated run log this verb wants, or ``None``.

        When a verb returns a basename (e.g. ``"sysforge-build.log"``), the
        runner opens it — under ``--log-dir`` else the resolved state dir —
        purged before ``execute`` and closes it *kept* (``persist=True``)
        afterwards, so a multi-package run leaves one consolidated log
        alongside the scattered per-package logs (parallel to
        ``sysforge-update.log``). Success is taken from
        ``ExecResult.artifacts['log_success']`` when present, else from a
        clean (exception-free, exit-0) return. Dry runs never write.

        Default ``None`` = no consolidated log. ``update`` and the full
        pipeline return ``None`` here because they own richer log lifecycles
        (their own purge/persist/success policy) and call
        :func:`~sysforge.log.open_unified_log` directly.
        Setting ``wants_run_log = True`` derives the basename from ``self.name``.
        """
        del args
        return f"sysforge-{self.name}.log" if self.wants_run_log else None

    def journal_target(self, args) -> str | None:
        """The ``SYSFORGE_TARGET`` value for this verb's journald record, or ``None``.

        Default: no target (the verb name alone is the record). Verbs with a
        meaningful subject (e.g. ``build`` → package names) override this.
        Only consulted for ``requires_sentinel`` verbs. See ``primitives/journal.py``.
        """
        del args
        return None

    def sentinel_metadata(self, args, pre: PreCheckResult) -> dict[str, Any]:
        """Extra fields persisted in the sentinel file.

        Override to record context that surfaces in the recovery prompt
        (e.g. ``compiler="llvm"``, ``pgo=True``). Default is empty.
        """
        del args, pre
        return {}

    def sentinel_recovery_cmd(self, args, pre: PreCheckResult) -> str | None:
        """Recovery shell command stored in the sentinel for the operator.

        Surfaces in the CLI-entry recovery prompt and the
        ``CleanExitRequested`` → ``RuntimeError`` message. Override when
        a single canonical command (e.g. ``sudo pacman -S llvm llvm-libs``)
        restores consistency after an interrupt.
        """
        del args, pre
        return None


@dataclass(frozen=True)
class VerbGroup:
    """A namespace verb (``sysforge state …``) over a fixed set of leaf verbs.

    ``build`` adds the parent parser, then ``add_arguments`` (flags accepted on
    the bare namespace), then one subparser per ``members`` entry in order.
    ``defaults`` is applied *after* the subparsers action exists. argparse gives
    that action its own ``default=None`` for ``dest``, which beats an earlier
    ``set_defaults`` for the same key, so this order is the only one in which a
    default subcommand name such as ``state_cmd="list"`` takes effect.
    ``default`` is the verb a bare ``sysforge <name>`` runs; ``None`` makes the
    subcommand required.
    """

    name: str
    help: str
    dest: str
    members: tuple[type[Verb], ...]
    default: type[Verb] | None = None
    defaults: Mapping[str, Any] = field(default_factory=dict)
    metavar: str | None = None
    add_arguments: Callable[[argparse.ArgumentParser], None] | None = None

    def build(self, sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
        p = sub.add_parser(self.name, help=self.help)
        if self.add_arguments is not None:
            self.add_arguments(p)
        group_sub = p.add_subparsers(dest=self.dest, metavar=self.metavar)
        group_sub.required = self.default is None
        if self.default is not None:
            p.set_defaults(verb_cls=self.default, **self.defaults)
        for verb_cls in self.members:
            verb_cls.add_parser(group_sub)
        return p
