# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stage_sentinel.py — durable marker for in-progress install-bearing stages.

The toolchain/kernel/packages stages mutate the live system via
`sudo pacman -U` / `pacman -S`. If the process is interrupted (SIGINT,
crash, power loss) partway through, the live system can end up with a
mismatched set of packages — e.g. a Pass-1 ``llvm-libs`` installed
against the prior ``llvm``/``clang``/``lld`` versions.

This primitive writes a small TOML sentinel into ``<state_dir>/
stage_in_progress.toml`` just before the stage performs the first
mutation, and clears it only after the stage's post-install verification
passes. Any subsequent sysforge invocation that loads state checks for a
stale sentinel via :func:`get_active` and surfaces a recovery prompt
before proceeding — converting "silently broken system" into "loud
blocker at next run".

State dir resolution matches ``BuildState`` / ``PipelineState``:
explicit ``Path`` > ``SYSFORGE_STATE_DIR`` env var > ``/var/lib/sysforge``.

Schema (TOML):

    [stage_in_progress]
    stage         = "toolchain"
    started_at    = "2026-05-14T15:00:00Z"
    compiler      = "llvm"       # optional per-stage metadata
    pgo           = true
    # ... arbitrary additional keys

The file is removed in its entirety on ``clear()``. Atomic write-then-rename matches the rest
of the state dir.

Liveness
--------

Presence of the sentinel alone cannot distinguish "a previous run died
mid-mutation" from "a run is alive right now" — and treating a live run as
stale is destructive (clearing its sentinel, then overwriting the record,
leaves two concurrent install-bearing runs with no interruption record for
either). So ``sentinel_scope`` also holds an exclusive ``flock`` on
``<state_dir>/stage_in_progress.lock`` for the stage's lifetime, via the
shared :mod:`sysforge.primitives.build_lock` primitive. Liveness is then
"is the lock takeable?", which the kernel answers correctly even after
``SIGKILL`` or power loss — a PID recorded in the sentinel could not, since
recycling would report a dead owner as alive forever.

Two layers use it: :func:`check_and_recover_stale_sentinel` probes the lock
at CLI entry and refuses *without prompting* when an owner is alive, and
``sentinel_scope`` acquires it for real, catching the probe/acquire race
plus any mutating verb outside ``cli._gate_sentinel_check``'s allowlist.
There is deliberately no override: a live owner is unambiguous, so a prompt
would only invite the mistake the guard exists to prevent.

Contention is strict (hard refusal), but ``OSError``/``PermissionError``
stay lenient — matching the sentinel write itself — so a read-only state
dir cannot lock the user out of every mutating verb.
"""
import os
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.interrupt import CleanExitRequested, InterruptScope
from sysforge.primitives.prompt import is_interactive, prompt_choice

_log = log.get_logger("SENTINEL")

LOCK_FILENAME = "stage_in_progress.lock"
# Woven into build_lock's contention message: "Another sysforge install
# stage is running (pid N)".
_LOCK_LABEL = "install"
_LOCK_NOUN = "stage"


def _resolve_state_dir(state_dir: Path | str | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ.get("SYSFORGE_STATE_DIR")
    if env:
        return Path(env)
    return Path("/var/lib/sysforge")


def lock_path(state_dir: Path | str | None = None) -> Path:
    """Path of the stage liveness lock for ``state_dir``.

    Living in the state dir makes per-state-dir scoping automatic: a run
    under an isolated ``SYSFORGE_STATE_DIR`` (test fixtures, VM) never
    contends with a live run against the real one.
    """
    return _resolve_state_dir(state_dir) / LOCK_FILENAME


def _stage_lock(state_dir: Path | str | None):
    return build_lock(lock_path(state_dir), label=_LOCK_LABEL, noun=_LOCK_NOUN)


def check_and_recover_stale_sentinel(state_dir: Path | str | None = None) -> bool:
    """Detect and offer recovery for a stale stage-in-progress sentinel.

    Called from CLI entry for every install-bearing command (build,
    update, run pipeline, run toolchain, run packages, run kernel). If a sentinel from a
    prior interrupted run is present, surfaces what was in flight and prompts the operator
    with the stage's recorded ``recovery_cmd`` (e.g. ``sudo pacman -S llvm llvm-libs ...``).

    Returns True if it is safe to proceed (no sentinel, or recovery
    succeeded). Returns False if the operator declined recovery or
    recovery failed — the caller should refuse to proceed.

    Non-interactive: ``eof_default="n"`` fires so unattended runs (CI,
    cron) block on a stale sentinel rather than silently auto-recovering.
    """
    sentinel = StageSentinel(state_dir)

    # Liveness first: presence of the sentinel says nothing about whether its
    # owner is still running, and the questions below ("clear and proceed?",
    # "restore now?") are unanswerable — and destructive — against a live run.
    # Probing acquires and releases immediately; the stage that follows takes
    # the lock for real.
    try:
        with _stage_lock(state_dir):
            pass
    except RuntimeError as e:
        live = sentinel.get_active() or {}
        detail = ""
        if live:
            detail = (
                f" It is running stage '{live.get('stage', '?')}', begun at "
                f"{live.get('started_at', '?')}."
            )
        _log.error(
            f"{e}{detail} Wait for it to finish, or kill it — this run cannot "
            "proceed and must not clear a live run's sentinel."
        )
        return False
    except (OSError, PermissionError) as e:
        # Lenient by design: a read-only or missing state dir must not lock the
        # user out of every mutating verb (see module docstring).
        _log.warn(
            f"Cannot probe the stage lock ({e}); concurrency detection is "
            "unavailable for this run"
        )

    record = sentinel.get_active()
    if record is None:
        return True

    stage = record.get("stage", "?")
    started = record.get("started_at", "?")
    recovery_cmd = record.get("recovery_cmd")

    _log.warn(
        f"Detected stale stage sentinel: '{stage}' began at {started} "
        "and did not complete cleanly.",
    )
    for k, v in sorted(record.items()):
        if k in ("stage", "started_at", "recovery_cmd"):
            continue
        _log.warn(f"  {k} = {v}")
    if recovery_cmd:
        _log.warn(f"Suggested recovery: {recovery_cmd}")
    else:
        _log.warn(
            "No recovery command recorded; manually verify the system or "
            "remove the sentinel file by hand once verified."
        )

    # Non-TTY: the prompt would auto-decline silently, leaving the user
    # thinking the recovery prompt never fired or that they answered it.
    # Emit explicit instructions instead so the manual path is obvious.
    if not is_interactive():
        _log.error(
            "Cannot prompt for recovery — stdin is not a TTY. "
            "Run sysforge in an interactive terminal to use the prompt, "
            f"or clear the sentinel manually: rm {sentinel.path}"
        )
        if recovery_cmd:
            _log.error(
                f"Then run the recovery command yourself: {recovery_cmd}"
            )
        return False

    # No auto-recovery registered: the prompt would dead-end on "y" if we
    # asked the recovery question, so ask the only question we can actually
    # honor — clear the sentinel after manual verification.
    if not recovery_cmd:
        choice = prompt_choice(
            "Clear the sentinel and proceed? [y/N]: ",
            choices=("y", "yes", "n"),
            default="n",
            eof_default="n",
            tag="SENTINEL",
            level="WARN",
        )
        if choice not in ("y", "yes"):
            return False
        sentinel.clear()
        if sentinel.path.exists():
            _log.error(
                f"Tried to clear sentinel but file is still present at "
                f"{sentinel.path}. Possible state-dir mismatch — verify "
                "$SYSFORGE_STATE_DIR and any --state-dir flag match the path "
                "above, then remove the file manually."
            )
            return False
        _log.ui("Sentinel cleared; proceeding.")
        return True

    choice = prompt_choice(
        "Restore a consistent state now? [y/N]: ",
        choices=("y", "yes", "n"),
        default="n",
        eof_default="n",
        tag="SENTINEL",
        level="WARN",
    )
    if choice not in ("y", "yes"):
        return False

    try:
        proc = subprocess.run(recovery_cmd.split(), check=False)
    except FileNotFoundError:
        _log.error("Recovery command binary missing — cannot run recovery")
        return False
    if proc.returncode != 0:
        _log.error(
            f"Recovery command exited {proc.returncode}; the live system "
            "may still be inconsistent — sentinel left in place",
        )
        return False
    sentinel.clear()
    # Verify the file is actually gone. If it isn't, the recovery succeeded
    # but cleared the wrong path (state-dir mismatch, namespace/chroot
    # surprise, etc.) — log loudly so the user doesn't trust a false-positive
    # "sentinel cleared" message while the same sentinel keeps firing.
    if sentinel.path.exists():
        _log.error(
            f"Recovery ran but sentinel file is still present at "
            f"{sentinel.path}. Possible state-dir mismatch — verify "
            "$SYSFORGE_STATE_DIR and any --state-dir flag match the path "
            "above, then remove the file manually."
        )
        return False
    _log.ui("Recovery completed; sentinel cleared.")
    return True


class StageSentinel:
    """Read/write wrapper around ``<state_dir>/stage_in_progress.toml``."""

    FILENAME = "stage_in_progress.toml"

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self._dir = _resolve_state_dir(state_dir)
        self.path = self._dir / self.FILENAME

    def get_active(self) -> dict | None:
        """Return the active sentinel record, or ``None`` if absent.

        A non-empty record means a previous invocation began an
        install-bearing stage and never cleared the sentinel — either it
        is still running (rare; sysforge does not parallelise stages) or
        it was interrupted. Callers should treat this as a blocker.
        """
        if not self.path.exists():
            return None
        try:
            with self.path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        record = data.get("stage_in_progress")
        if not record:
            return None
        return dict(record)

    def mark_started(self, stage: str, **metadata) -> None:
        """Record that ``stage`` has begun a system-mutating operation.

        ``started_at`` is set to the current UTC time in ISO-8601. Extra
        ``metadata`` keys (e.g. ``compiler="llvm"``, ``pgo=True``) are
        serialised verbatim and surfaced in the recovery prompt so the
        user knows what was in flight.
        """
        record = {
            "stage": stage,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for k, v in metadata.items():
            record[k] = v
        self._write({"stage_in_progress": record})

    def clear(self) -> None:
        """Remove the sentinel file. No-op if absent."""
        with suppress(FileNotFoundError):
            self.path.unlink()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _write(self, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(self._serialize(data))
        tmp.rename(self.path)

    @staticmethod
    def _serialize(data: dict) -> str:
        lines = [
            "# SysForge stage-in-progress sentinel",
            "# Present means a previous run began an install-bearing stage",
            "# (toolchain/kernel/packages) and did not complete cleanly.",
            "# Sysforge refuses to proceed until the system is verified or",
            "# the sentinel is cleared. Do not edit by hand.",
            "",
        ]
        for section, body in data.items():
            lines.append(f"[{section}]")
            for key in sorted(body.keys()):
                val = body[key]
                # Skip None — TOML has no null, and str(None) writes the
                # literal "None" which downstream readers then interpret as
                # a meaningful (truthy) value. Caller can omit the key.
                if val is None:
                    continue
                if isinstance(val, bool):
                    lines.append(f"{key} = {'true' if val else 'false'}")
                elif isinstance(val, int):
                    lines.append(f"{key} = {val}")
                else:
                    escaped = (
                        str(val)
                        .replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                        .replace("\r", "\\r")
                    )
                    lines.append(f'{key} = "{escaped}"')
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# sentinel_scope — shared context manager
# ---------------------------------------------------------------------------

@contextmanager
def sentinel_scope(
    state_dir: Path | str | None,
    stage_name: str,
    *,
    recovery_cmd: str | None = None,
    retry_cmd: str | None = None,
    **metadata,
) -> Iterator["StageSentinel"]:
    """Install a stage sentinel for the duration of the block.

    Wraps the body in an :class:`InterruptScope` so the first Ctrl-C inside
    install-bearing work defers to the next safe boundary instead of
    interrupting mid-mutation. On normal completion the sentinel is cleared;
    on any exception (including ``CleanExitRequested``) the sentinel is left
    in place so the next sysforge invocation blocks at the CLI-entry recovery
    prompt in :func:`check_and_recover_stale_sentinel`.

    Used by both the toolchain pipeline stage and the CLI verb runner so
    there is one implementation of "install sentinel → interrupt-safe body
    → clear sentinel on success" rather than two divergent copies.

    **Scopes must not nest** against the same state dir. They already don't:
    stages run sequentially and the ``run`` verbs set
    ``requires_sentinel = False`` precisely so the verb-level scope cannot
    wrap a stage's own (see ``run_cmd`` module docstring). Since the liveness
    lock is per open file description, a nested scope would contend with its
    own parent and raise — which is the correct report of a real bug, but
    reach for a sequential scope, not a re-entrant lock.

    Args:
        state_dir: same resolution as the rest of the state dir
            (explicit > env var > ``/var/lib/sysforge``).
        stage_name: short identifier recorded in the sentinel; surfaces in
            the recovery prompt and the ``CleanExitRequested`` log message.
        recovery_cmd: shell command (e.g. ``sudo pacman -S llvm llvm-libs``)
            stored in the sentinel for the operator-facing recovery prompt.
        retry_cmd: command the operator can re-run to resume from a clean
            state, surfaced in the ``CleanExitRequested`` → ``RuntimeError``
            message (e.g. ``sysforge run toolchain``).
        **metadata: extra fields persisted in the sentinel (e.g.
            ``compiler="llvm"``, ``pgo=True``).

    Raises:
        RuntimeError: when another sysforge run already holds the stage lock
            — raised on entry, before ``mark_started``, so a live owner's
            sentinel is never overwritten. ``verbs.runner.run_verb`` catches
            it and exits 1.
        RuntimeError: when a ``CleanExitRequested`` propagates out of the
            body. The message includes ``retry_cmd`` and ``recovery_cmd``
            when supplied so the operator has both copy-pasteable hints.
        Any other exception raised by the body propagates verbatim.
    """
    sentinel = StageSentinel(state_dir)
    # Only forward recovery_cmd when set — otherwise it would land in the
    # sentinel record as a key with no useful value (see _serialize).
    extra = {"recovery_cmd": recovery_cmd} if recovery_cmd is not None else {}
    with ExitStack() as stack:
        # Held for the whole stage so a concurrent run's entry probe (and the
        # acquisition below, on the probe/acquire race) sees a live owner.
        # Contention raises RuntimeError out of the CM's __enter__; OSError
        # stays lenient.
        try:
            stack.enter_context(_stage_lock(state_dir))
        except (OSError, PermissionError) as e:
            _log.warn(
                f"Cannot acquire the stage lock ({e}); concurrent-run "
                "detection will be unavailable for this run"
            )

        try:
            sentinel.mark_started(stage_name, **extra, **metadata)
        except (OSError, PermissionError) as e:
            _log.warn(
                f"Cannot write stage sentinel ({e}); interrupted-run "
                "detection will be unavailable for this run"
            )

        try:
            with InterruptScope():
                yield sentinel
        except CleanExitRequested:
            _log.warn(
                f"{stage_name} interrupted at a safe boundary. Stage sentinel "
                "left in place; the next sysforge invocation will detect the "
                "interruption and offer recovery."
            )
            parts = [f"[{stage_name.upper()}] interrupted by user (Ctrl-C)."]
            if retry_cmd:
                parts.append(f"Re-run with `{retry_cmd}` to resume.")
            if recovery_cmd:
                parts.append(f"To restore consistency manually: {recovery_cmd}")
            raise RuntimeError(" ".join(parts)) from None

        try:
            sentinel.clear()
        except OSError as e:
            _log.warn(f"Cannot clear stage sentinel ({e})")
