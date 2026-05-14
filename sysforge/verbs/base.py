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
"""
from __future__ import annotations

from abc import ABC, abstractmethod
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

    and implement the three phase methods.
    """

    name: str = ""
    requires_sentinel: bool = False

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
