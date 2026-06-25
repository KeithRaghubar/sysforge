# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

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

from contextlib import contextmanager, nullcontext
from pathlib import Path

from sysforge import log
from sysforge.primitives.stage_sentinel import sentinel_scope
from sysforge.ui import progress
from sysforge.verbs.base import Verb


def run_verb(verb: Verb, args) -> int:
    """Run ``verb`` through pre_check → execute → post_validate.

    Returns the process exit code (0 on success, 1 on RuntimeError,
    custom exit code on pre-check block, or the verb's own
    ``ExecResult.exit_code`` on success).

    A generic startup phase is painted on entry so the bottom-anchored
    progress indicator is live for *every* verb from dispatch onward
    (filling the otherwise-blank startup window), and cleared on every
    exit path. Verbs that paint their own richer phases (e.g. ``update``)
    override the generic label seamlessly via the single shared phase
    state.
    """
    tag = (verb.name or "VERB").upper()
    _log = log.get_logger(tag)

    label = (verb.name or "verb").removeprefix("run-")
    progress.phase(f"{label}: starting…")
    try:
        return _run_verb_inner(verb, args, _log)
    finally:
        progress.phase(None)


def _run_verb_inner(verb: Verb, args, _log) -> int:
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
        with _consolidated_log(verb, args, _log) as log_state, scope:
            result = verb.execute(args, pre)
            verb.post_validate(args, pre, result)
            log_state["success"] = bool(
                result.artifacts.get("log_success", result.exit_code == 0)
            )
    except RuntimeError as e:
        _log.error(str(e))
        return 1

    return result.exit_code


@contextmanager
def _consolidated_log(verb: Verb, args, _log):
    """Open a per-verb consolidated run log around ``execute``, if requested.

    Yields a mutable ``{"success": bool}`` dict the caller sets from the verb
    outcome; the log is closed *kept* (``persist=True``) so the file survives
    for inspection, parallel to ``sysforge-update.log``. The lifecycle
    primitive (``log.open_unified_log``/``close_unified_log``) is the single
    home — this is just the generic-verb caller. Verbs return ``None`` from
    ``unified_log_basename`` (default) to opt out; ``update`` and the pipeline
    do so because they manage their own richer log lifecycle.

    No-op (and ``success`` is irrelevant) when the verb opts out or on a dry
    run, where nothing should be written to disk.
    """
    state = {"success": True}
    basename = verb.unified_log_basename(args)
    if not basename or getattr(args, "dry_run", False):
        yield state
        return

    from sysforge.pipeline.state import resolve_state_dir

    log_dir = (
        Path(args.log_dir)
        if getattr(args, "log_dir", None)
        else resolve_state_dir(getattr(args, "state_dir", None))[0]
    )
    log_path = log_dir / basename
    try:
        log.open_unified_log(log_path, purge=True)
        _log.info(f"Consolidated log: {log_path}")
    except OSError as e:
        _log.warn(f"Cannot write consolidated log to {log_path}: {e} — terminal only")
        yield state
        return

    try:
        yield state
    finally:
        log.close_unified_log(success=state["success"], persist=True)
        _log.ui(f"[SYSFORGE] Consolidated log: {log_path}")


def _resolve_state_dir(args) -> Path | None:
    """Pluck ``--state-dir`` from parsed args, if present.

    Matches the same resolution path the rest of sysforge uses — explicit
    arg wins; otherwise return ``None`` and let the downstream
    :class:`~sysforge.primitives.stage_sentinel.StageSentinel` fall back
    to the env var or the default state dir.
    """
    sd = getattr(args, "state_dir", None)
    return Path(sd) if sd else None
