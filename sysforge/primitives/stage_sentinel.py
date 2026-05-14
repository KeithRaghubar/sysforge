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

The file is removed in its entirety on ``clear()``. Atomic write-then-rename matches the rest of the state dir.
"""
import os
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from sysforge import log
from sysforge.primitives.prompt import prompt_choice

_log = log.get_logger("SENTINEL")


def _resolve_state_dir(state_dir: Path | str | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    env = os.environ.get("SYSFORGE_STATE_DIR")
    if env:
        return Path(env)
    return Path("/var/lib/sysforge")


def check_and_recover_stale_sentinel(state_dir: Path | str | None = None) -> bool:
    """Detect and offer recovery for a stale stage-in-progress sentinel.

    Called from CLI entry for every install-bearing command (build,
    update, converge, run pipeline, run toolchain, run packages,
    run kernel). If a sentinel from a prior interrupted run is present,
    surfaces what was in flight and prompts the operator with the stage's
    recorded ``recovery_cmd`` (e.g. ``sudo pacman -S llvm llvm-libs ...``).

    Returns True if it is safe to proceed (no sentinel, or recovery
    succeeded). Returns False if the operator declined recovery or
    recovery failed — the caller should refuse to proceed.

    Non-interactive: ``eof_default="n"`` fires so unattended runs (CI,
    cron) block on a stale sentinel rather than silently auto-recovering.
    """
    sentinel = StageSentinel(state_dir)
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

    if not recovery_cmd:
        _log.error(
            "No recovery command recorded — cannot auto-recover. "
            "Verify the system manually, then remove the sentinel.",
        )
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
            with open(self.path, "rb") as f:
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
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

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
