# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
init_notice.py — first-install bootstrap reminder (F1).

A fresh ``pacman -S sysforge`` install drops an empty marker file at
``<state_dir>/.sysforge-init-notice`` from the PKGBUILD ``post_install``
scriptlet. On *every* CLI invocation :func:`maybe_emit_init_notice` checks
for it and:

  * if absent → does nothing (the steady state);
  * if present and the ``reconfigure``/``hardware`` bootstrap stages are not
    both ``done`` → prints a one-time advisory naming the stages still to run
    (the file persists, so the advice repeats until setup is complete or the
    operator deletes the file to dismiss it);
  * if present and both stages are ``done`` → silently removes the marker.

State dir resolution matches :class:`StageSentinel` / :class:`PipelineState`:
explicit ``Path`` > ``SYSFORGE_STATE_DIR`` env var > ``/var/lib/sysforge``.

This is a pure UX nicety — it never blocks a command and never raises. The
marker is only ever *created* by the package scriptlet, so deleting it is an
unambiguous dismissal (sysforge never recreates it).
"""
import os
from pathlib import Path

from sysforge import log

_log = log.get_logger("INIT")

FILENAME = ".sysforge-init-notice"

# Bootstrap stages a fresh install must run before anything else is useful.
_REQUIRED_STAGES = ("reconfigure", "hardware")


def _resolve_state_dir(state_dir: Path | str | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ.get("SYSFORGE_STATE_DIR")
    if env:
        return Path(env)
    return Path("/var/lib/sysforge")


def notice_path(state_dir: Path | str | None = None) -> Path:
    """Return the notice marker path under the resolved state dir."""
    return _resolve_state_dir(state_dir) / FILENAME


def maybe_emit_init_notice(state_dir: Path | str | None = None) -> str | None:
    """Surface or retire the first-install notice. Best-effort; never raises.

    Returns one of:
      ``None``     — no notice present (nothing to do);
      ``"advised"``— notice present, bootstrap incomplete, advisory printed;
      ``"cleared"``— notice present, bootstrap complete, marker removed.
    """
    try:
        path = notice_path(state_dir)
        if not path.exists():
            return None

        # Read the resolved dir's pipeline state directly (the marker lives in
        # the same dir as pipeline_state.toml).
        from sysforge.pipeline.state import PipelineState

        ps = PipelineState(path.parent)
        pending = [s for s in _REQUIRED_STAGES if ps.stage_status(s) != "done"]

        if not pending:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return "cleared"

        cmds = " && ".join(f"sysforge run {s}" for s in pending)
        _log.warn(
            "Initial setup is incomplete. Run the bootstrap "
            f"stage{'s' if len(pending) > 1 else ''} before other commands: {cmds}"
        )
        _log.warn(f"(or delete {path} to dismiss this notice)")
        return "advised"
    except Exception as e:  # pragma: no cover - defensive; never break a command
        log.debug("[INIT]", f"init-notice check failed: {e}")
        return None
