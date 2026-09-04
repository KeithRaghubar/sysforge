# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
init_notice.py — first-run advisories (F1, 3.1.0-F5).

Two advisories share this module because they answer the same question at
different moments: *what does this fresh install still need from you?*

  * :func:`maybe_emit_init_notice` — the post-install bootstrap reminder (F1).
  * :func:`maybe_emit_stage_config_notice` — the first ``run toolchain`` /
    ``run kernel`` on an install (3.1.0-F5).

Both are strictly advisory: they print, never prompt, never block, never raise.

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
import contextlib
import os
from pathlib import Path

from sysforge import log

_log = log.get_logger("INIT")

FILENAME = ".sysforge-init-notice"

# Bootstrap stages a fresh install must run before anything else is useful.
_REQUIRED_STAGES = ("reconfigure", "hardware")

# 3.1.0-F5: the two stages whose behaviour is decided almost entirely by their
# TOML rather than by the flags the user typed. A user who ran `setup` and then
# types `sysforge run kernel` otherwise gets a kernel built from defaults they
# have never opened. Each entry names the config file and the two or three
# settings most likely to surprise.
#
# The F1 notice above cannot cover this: it keys on a marker the PKGBUILD
# post_install scriptlet drops and retires itself the moment reconfigure and
# hardware are both done — i.e. it clears *before* anyone reaches toolchain or
# kernel.
_STAGE_CONFIG_ADVICE: dict[str, tuple[str, tuple[str, ...]]] = {
    "toolchain": (
        "toolchain.toml",
        (
            "compiler — gcc or llvm (default: gcc)",
            "[pgo] enabled — a PGO build always builds LLVM from source",
            "repo_mode — whether stock packages come from the repos or a "
            "source build",
        ),
    ),
    "kernel": (
        "kernel.toml",
        (
            "[[kconfig]] — the fragments merged into the kernel config",
            "subpackages — which of headers/docs/api-headers are built",
            "initramfs handling and the interactive kconfig step",
        ),
    ),
}


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
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
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


def stage_config_path(stage_name: str):
    """Config file that drives ``stage_name``, or ``None`` if it has no advice."""
    from sysforge.primitives import paths

    entry = _STAGE_CONFIG_ADVICE.get(stage_name)
    if entry is None:
        return None
    return paths.CONFIG_DIR / entry[0]


def maybe_emit_stage_config_notice(stage_name: str, state) -> str | None:
    """Advise on the config driving a stage the install has never completed.

    3.1.0-F5. Returns ``"advised"`` when the advisory printed, else ``None``.

    "First run" means never *completed*, not never *attempted*: a failed first
    attempt re-advises, because the config is exactly as unread as it was
    before — and a stage that failed is the one a user is most likely to retry
    without having looked. ``PipelineState.stage_status`` already returns
    ``"pending"`` for a stage it has never seen, so this needs no new state.

    Emitted at ``ui()`` rather than ``warn()``: this is a pre-flight block
    before a stage that changes the system, the same class as the plan tables
    ``render_preflight`` emits, and ``warn()`` is gated behind ``-v`` — a fresh
    install would never see it, which is the entire point of the item.

    Strictly advisory: prints, never prompts, never blocks, never raises. A
    prompt here would inherit every unattended-consent problem 3.0.0-F3 and
    3.1.0-F4 are about.

    Superseded by 3.1.0-F4 if that lands — a rendered plan *is* the config made
    visible.
    """
    try:
        entry = _STAGE_CONFIG_ADVICE.get(stage_name)
        if entry is None:
            return None
        if state.stage_status(stage_name) == "done":
            return None

        _filename, settings = entry
        path = stage_config_path(stage_name)
        _log.ui("")
        _log.ui(
            f"This is the first {stage_name} run on this install. What it "
            f"builds is decided by {path}, not by the flags you typed:"
        )
        for setting in settings:
            _log.ui(f"  - {setting}")
        _log.ui(f"Review {path} before continuing.")
        _log.ui("")
        return "advised"
    except Exception as e:  # pragma: no cover - defensive; never break a stage
        log.debug("[INIT]", f"stage-config notice failed for {stage_name}: {e}")
        return None
