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
passes. (A stage writes it when its scope opens, around the install; the
verb runner's scope writes it at the first escalation that changes the
system, via :func:`~sysforge.primitives.privilege.on_mutation`, 3.5.0-B138.)
Any subsequent sysforge invocation that loads state checks for a
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
    recovery_kind = "pacman_sync"            # optional, see Recovery
    recovery_args = ["llvm", "llvm-libs"]
    # ... arbitrary additional keys

Recovery is stored as a kind plus arguments, never as a command line
(3.5.0-B43). The state dir is ``root:sysforge`` 2775, so any group member can
write this file; a stored command would let one member choose what another
runs, possibly under sudo. :class:`Recovery` rebuilds the argv from a fixed
template per kind and validates every argument first, and
:func:`check_and_recover_stale_sentinel` refuses a sentinel owned by anyone
but the current user or root.

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
import re
import shlex
import subprocess
import tomllib
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log
from sysforge.primitives import atomic_write
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.interrupt import CleanExitRequested, InterruptScope
from sysforge.primitives.privilege import on_mutation, privileged_argv
from sysforge.primitives.prompt import is_interactive, no_prompt_reason, prompt_choice
from sysforge.primitives.resource_guard import lift_for_child

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


# ---------------------------------------------------------------------------
# Recovery — a kind plus validated arguments, never a stored command line
# ---------------------------------------------------------------------------

RECOVERY_PACMAN_SYNC = "pacman_sync"         # pacman -S -- <pkgname>...
RECOVERY_PACMAN_UPGRADE = "pacman_upgrade"   # pacman -U --noconfirm -- <cached archive>...
RECOVERY_MKINITCPIO = "mkinitcpio"           # mkinitcpio -P
RECOVERY_KINDS = (RECOVERY_PACMAN_SYNC, RECOVERY_PACMAN_UPGRADE, RECOVERY_MKINITCPIO)

# pacman's pkgname rule: lowercase alnum and @._+-, not starting with - or .
_PKGNAME_RE = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]*$")
_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9@._+-]+\.pkg\.tar(?:\.[a-z0-9]+)?$")


def _check_cached_archive(arg: str) -> None:
    """Refuse anything but a root-owned package archive in pacman's cache.

    ``pacman -U`` accepts an unsigned local file under the default
    ``LocalFileSigLevel``, so a path from a group-writable record would let a
    member hand another user's sudo their own package.
    """
    from sysforge.primitives import pacman
    path = Path(arg)
    if not path.is_absolute() or not _ARCHIVE_RE.match(path.name):
        raise ValueError(f"not a package archive path: {arg!r}")
    real = path.resolve()
    caches = [d.resolve() for d in pacman.get_pacman_cache_dirs()]
    if real.parent not in caches:
        raise ValueError(f"{arg!r} is not in a pacman cache dir")
    try:
        st = real.stat()
    except OSError as e:
        raise ValueError(f"{arg!r}: {e}") from None
    if st.st_uid != 0:
        raise ValueError(f"{arg!r} is not owned by root")


@dataclass(frozen=True)
class Recovery:
    """What to run to restore consistency after an interrupted stage.

    Persisted as ``recovery_kind`` + ``recovery_args`` and turned back into a
    command only through :meth:`argv`, which applies a fixed template per kind
    to arguments it has validated (3.5.0-B43).
    """
    kind: str
    args: tuple[str, ...] = ()

    @classmethod
    def pacman_sync(cls, names: Iterable[str]) -> "Recovery":
        return cls(RECOVERY_PACMAN_SYNC, tuple(names))

    @classmethod
    def pacman_upgrade(cls, files: Iterable["Path | str"]) -> "Recovery":
        return cls(RECOVERY_PACMAN_UPGRADE, tuple(str(f) for f in files))

    @classmethod
    def mkinitcpio(cls) -> "Recovery":
        return cls(RECOVERY_MKINITCPIO)

    def _command(self) -> list[str]:
        """The unescalated argv. Raises ``ValueError`` on anything invalid."""
        if self.kind == RECOVERY_PACMAN_SYNC:
            if not self.args or not all(_PKGNAME_RE.match(a) for a in self.args):
                raise ValueError(f"invalid package names: {list(self.args)}")
            return ["pacman", "-S", "--", *self.args]
        if self.kind == RECOVERY_PACMAN_UPGRADE:
            if not self.args:
                raise ValueError("no package archives")
            for a in self.args:
                _check_cached_archive(a)
            return ["pacman", "-U", "--noconfirm", "--", *self.args]
        if self.kind == RECOVERY_MKINITCPIO:
            if self.args:
                raise ValueError("mkinitcpio recovery takes no arguments")
            return ["mkinitcpio", "-P"]
        raise ValueError(f"unknown recovery kind {self.kind!r}")

    def argv(self, *, to_run: bool = True) -> list[str]:
        """The escalated argv to run. Raises ``ValueError`` when invalid.

        ``to_run=False`` for an argv only validated or shown, which is no
        system change (``privileged_argv(mutates=False)``).
        """
        return privileged_argv(self._command(), mutates=to_run)

    def display(self) -> str:
        """The command as a copy-pasteable shell line, for messages.

        Built from the template too, so a message never repeats a stored
        string. An invalid record renders as its kind alone.
        """
        try:
            return shlex.join(self.argv(to_run=False))
        except ValueError:
            return f"<invalid {self.kind} recovery>"

    def record_fields(self) -> dict:
        return {"recovery_kind": self.kind, "recovery_args": list(self.args)}


def recovery_from_record(record: dict) -> "Recovery | None":
    """The validated :class:`Recovery` a sentinel record names, or ``None``.

    ``None`` when the record has no recovery, names an unknown kind, or its
    arguments fail validation. A pre-3.5.0-B43 free-form ``recovery_cmd`` is
    never turned into a command.
    """
    kind = record.get("recovery_kind")
    args = record.get("recovery_args", [])
    if kind not in RECOVERY_KINDS or not isinstance(args, list) \
            or not all(isinstance(a, str) for a in args):
        return None
    rec = Recovery(kind, tuple(args))
    try:
        rec.argv(to_run=False)
    except ValueError as e:
        _log.warn(f"Recorded recovery refused: {e}")
        return None
    return rec


def _foreign_owner(path: Path) -> int | None:
    """The owner uid when ``path`` is owned by neither the current user nor
    root (or is a symlink, reported as its own uid); else ``None``."""
    try:
        st = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or st.st_uid not in (os.getuid(), 0):
        return st.st_uid
    return None


def check_and_recover_stale_sentinel(state_dir: Path | str | None = None) -> bool:
    """Detect and offer recovery for a stale stage-in-progress sentinel.

    Called from CLI entry for every install-bearing command (build,
    update, run pipeline, run toolchain, run packages, run kernel). If a sentinel from a
    prior interrupted run is present, surfaces what was in flight and prompts the operator
    with the stage's recorded :class:`Recovery` (e.g. ``sudo pacman -S -- llvm llvm-libs ...``),
    rebuilt from its template; a sentinel owned by anyone but the current user
    or root is refused without prompting (3.5.0-B43).

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
            f"{e}{detail} Wait for it to finish, or kill it: this run cannot "
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

    owner = _foreign_owner(sentinel.path)
    if owner is not None:
        # Anyone in the sysforge group can write the state dir, so a record
        # owned by someone else may have been planted (3.5.0-B43). Act on
        # nothing in it; the operator inspects it and removes it.
        _log.error(
            f"Stale stage sentinel {sentinel.path} is owned by uid {owner}, not "
            "by you or root; refusing to act on it. Inspect it, then remove it "
            "by hand if it is safe."
        )
        return False

    stage = record.get("stage", "?")
    started = record.get("started_at", "?")
    recovery = recovery_from_record(record)
    recovery_cmd = recovery.display() if recovery else None

    _log.warn(
        f"Detected stale stage sentinel: '{stage}' began at {started} "
        "and did not complete cleanly.",
    )
    for k, v in sorted(record.items()):
        if k in _RECORD_DISPLAY_SKIP:
            continue
        _log.warn(f"  {k} = {v}")
    legacy = record.get("recovery_cmd")
    if recovery_cmd:
        _log.warn(f"Suggested recovery: {recovery_cmd}")
    elif isinstance(legacy, str) and legacy:
        _log.warn(
            f"Recorded recovery (from an older sysforge, never run "
            f"automatically): {legacy!r}. Verify it before running it yourself."
        )
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
            f"Cannot prompt for recovery ({no_prompt_reason()}). "
            "Run sysforge at a terminal, without --unattended, to use the prompt, "
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
                f"{sentinel.path}. Possible state-dir mismatch: verify "
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

    if recovery is None:   # unreachable: recovery_cmd is set only from it
        return False
    try:
        proc = subprocess.run(recovery.argv(), check=False, preexec_fn=lift_for_child)  # noqa: TID251 — operator's recovery command runs on the TTY
    except FileNotFoundError:
        _log.error("Recovery command binary missing: cannot run recovery")
        return False
    if proc.returncode != 0:
        _log.error(
            f"Recovery command exited {proc.returncode}; the live system "
            "may still be inconsistent: sentinel left in place",
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
            f"{sentinel.path}. Possible state-dir mismatch: verify "
            "$SYSFORGE_STATE_DIR and any --state-dir flag match the path "
            "above, then remove the file manually."
        )
        return False
    _log.ui("Recovery completed; sentinel cleared.")
    return True


# Record keys the recovery prompt does not list as metadata.
_RECORD_DISPLAY_SKIP = frozenset(
    {"stage", "started_at", "recovery_cmd", "recovery_kind", "recovery_args"})


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
        atomic_write.write_state(self.path, self._serialize(data))

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
                elif isinstance(val, (list, tuple)):
                    items = ", ".join(_toml_str(v) for v in val)
                    lines.append(f"{key} = [{items}]")
                else:
                    lines.append(f"{key} = {_toml_str(val)}")
            lines.append("")
        return "\n".join(lines)


def _toml_str(val) -> str:
    """``val`` as a TOML basic string."""
    escaped = (
        str(val)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# sentinel_scope — shared context manager
# ---------------------------------------------------------------------------

@contextmanager
def sentinel_scope(
    state_dir: Path | str | None,
    stage_name: str,
    *,
    recovery: Recovery | None = None,
    retry_cmd: str | None = None,
    arm_on_mutation: bool = False,
    **metadata,
) -> Generator["StageSentinel", None, None]:
    """Install a stage sentinel for the duration of the block.

    Wraps the body in an :class:`InterruptScope` so the first Ctrl-C inside
    install-bearing work defers to the next safe boundary instead of
    interrupting mid-mutation: the next ``interrupt.checkpoint()`` the body
    reaches, or, when it reaches none, the end of the body, after which the
    SIGINT is redelivered (3.5.0-B44). On normal completion the sentinel is cleared;
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

    A stale record from an earlier interrupted run is never overwritten: the
    scope first offers the recovery :func:`check_and_recover_stale_sentinel`
    offers at CLI entry, and raises ``RuntimeError`` with the record kept
    when it is declined or fails (3.5.0-B46).

    Args:
        state_dir: same resolution as the rest of the state dir
            (explicit > env var > ``/var/lib/sysforge``).
        stage_name: short identifier recorded in the sentinel; surfaces in
            the recovery prompt and the ``CleanExitRequested`` log message.
        recovery: what restores consistency (e.g.
            ``Recovery.pacman_sync(["llvm", "llvm-libs"])``), stored as a kind
            plus arguments for the operator-facing recovery prompt, which
            rebuilds the command from a fixed template (3.5.0-B43).
        retry_cmd: command the operator can re-run to resume from a clean
            state, surfaced in the ``CleanExitRequested`` → ``RuntimeError``
            message (e.g. ``sysforge run toolchain``).
        arm_on_mutation: write the record at the body's first system change
            (the first :func:`~sysforge.primitives.privilege.privileged_argv`
            that mutates) instead of on entry (3.5.0-B138). The verb runner
            sets it: a verb's ``execute`` does its checks first, and a failure
            there, before anything changed, must leave no record for the next
            run to refuse on. The lock is still taken on entry. The stages
            keep the default: they open their scope around the install itself.
        **metadata: extra fields persisted in the sentinel (e.g.
            ``compiler="llvm"``, ``pgo=True``).

    Raises:
        RuntimeError: when another sysforge run already holds the stage lock
            — raised on entry, before ``mark_started``, so a live owner's
            sentinel is never overwritten. ``verbs.runner.run_verb`` catches
            it and exits 1.
        RuntimeError: when a ``CleanExitRequested`` propagates out of the
            body. The message includes ``retry_cmd`` and the recovery command
            when supplied so the operator has both copy-pasteable hints.
        Any other exception raised by the body propagates verbatim.
    """
    sentinel = StageSentinel(state_dir)
    # Another run's interrupted-install record must survive this scope
    # (3.5.0-B46): mark_started would overwrite it and the success path clear
    # it. The CLI gate offers recovery only to build/update/run/setup, so a
    # verb outside that list (uninstall, revert-to-stock, state, artifact)
    # gets the same offer here, before the lock (the check probes it), and
    # refuses when the record stays.
    if sentinel.get_active() is not None and not check_and_recover_stale_sentinel(state_dir):
        stale = (sentinel.get_active() or {}).get("stage", "?")
        raise RuntimeError(
            f"[{stage_name.upper()}] refusing to start: an interrupted '{stale}' "
            f"run left its recovery record at {sentinel.path}. Recover it first "
            "(rerun in a terminal for the prompt), or remove the file once the "
            "system is verified."
        )
    # Only forward the recovery when set — otherwise it would land in the
    # sentinel record as keys with no useful value (see _serialize).
    extra = recovery.record_fields() if recovery is not None else {}
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

        armed = False

        def arm(_argv: list[str] | None = None) -> None:
            nonlocal armed
            if armed:
                return
            armed = True
            try:
                sentinel.mark_started(stage_name, **extra, **metadata)
            except (OSError, PermissionError) as e:
                _log.warn(
                    f"Cannot write stage sentinel ({e}); interrupted-run "
                    "detection will be unavailable for this run"
                )

        if not arm_on_mutation:
            arm()
        scope = InterruptScope()
        try:
            with scope, on_mutation(arm) if arm_on_mutation else nullcontext():
                yield sentinel
        except CleanExitRequested:
            if not armed:
                raise RuntimeError(
                    f"[{stage_name.upper()}] interrupted by user (Ctrl-C) before "
                    "anything on the system changed."
                ) from None
            _log.warn(
                f"{stage_name} interrupted at a safe boundary. Stage sentinel "
                "left in place; the next sysforge invocation will detect the "
                "interruption and offer recovery."
            )
            parts = [f"[{stage_name.upper()}] interrupted by user (Ctrl-C)."]
            if retry_cmd:
                parts.append(f"Re-run with `{retry_cmd}` to resume.")
            if recovery is not None:
                parts.append(f"To restore consistency manually: {recovery.display()}")
            raise RuntimeError(" ".join(parts)) from None

        try:
            sentinel.clear()
        except OSError as e:
            _log.warn(f"Cannot clear stage sentinel ({e})")
    # A Ctrl-C the body never reached a checkpoint for: the mutation window is
    # complete and the sentinel cleared, so stop now, as a Ctrl-C outside the
    # scope would (3.5.0-B44). Outside the ExitStack, so the lock is released.
    scope.redeliver()
