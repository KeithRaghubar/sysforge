"""
verbs/runner.py — uniform dispatcher for CLI verbs.

:func:`run_verb` walks a :class:`~sysforge.verbs.base.Verb` through its
three phases (``pre_check`` → ``execute`` → ``post_validate``) with one
shared error model and one shared sentinel-handoff path. Every
``args.func`` in ``cli.py`` resolves to a ``Verb`` factory; ``main()``
invokes ``sys.exit(run_verb(args.func(), args))``.

Error model:
  • :class:`RuntimeError` raised from any phase → ``_log.error(msg)``,
    return 1. Sentinel is preserved if active (the next sysforge run
    blocks at the CLI-entry recovery prompt).
  • :class:`SystemExit` propagates verbatim so tests and CLI hooks can
    see the raw exit code.
  • Other exceptions propagate verbatim (the user gets a traceback —
    a verb that raises ``KeyError`` is a bug, not an expected outcome).

Sentinel handling (only when ``verb.requires_sentinel`` and pre_check
proceeds): the runner enters
:func:`~sysforge.primitives.stage_sentinel.sentinel_scope` for the
duration of ``execute + post_validate``. The sentinel is written on
entry, cleared on a clean exit, and left in place on any exception.
"""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from sysforge import log
from sysforge.primitives.stage_sentinel import sentinel_scope
from sysforge.verbs.base import Verb


def run_verb(verb: Verb, args) -> int:
    """Run ``verb`` through pre_check → execute → post_validate.

    Returns the process exit code (0 on success, 1 on RuntimeError,
    custom exit code on pre-check block, or the verb's own
    ``ExecResult.exit_code`` on success).
    """
    tag = (verb.name or "VERB").upper()
    _log = log.get_logger(tag)

    try:
        pre = verb.pre_check(args)
    except RuntimeError as e:
        _log.error(str(e))
        return 1

    if pre.blocker is not None:
        _log.error(pre.blocker)
        return pre.exit_code or 1
    if pre.skip_reason is not None:
        _log.info(pre.skip_reason)
        return 0

    if verb.requires_sentinel:
        scope = sentinel_scope(
            _resolve_state_dir(args),
            verb.name,
            recovery_cmd=verb.sentinel_recovery_cmd(args, pre),
            retry_cmd=f"sysforge {verb.name}",
            **verb.sentinel_metadata(args, pre),
        )
    else:
        scope = nullcontext()

    try:
        with scope:
            result = verb.execute(args, pre)
            verb.post_validate(args, pre, result)
    except RuntimeError as e:
        _log.error(str(e))
        return 1

    return result.exit_code


def _resolve_state_dir(args) -> Path | None:
    """Pluck ``--state-dir`` from parsed args, if present.

    Matches the same resolution path the rest of sysforge uses — explicit
    arg wins; otherwise return ``None`` and let the downstream
    :class:`~sysforge.primitives.stage_sentinel.StageSentinel` fall back
    to the env var or the default state dir.
    """
    sd = getattr(args, "state_dir", None)
    return Path(sd) if sd else None
