"""
state.py — pipeline checkpoint state

Manages reading and writing /var/lib/sysforge/pipeline_state.toml (or
the configured override). Tracks per-stage status and per-package progress
within the packages stage.

State dir resolution (highest priority first):
  1. Explicit Path passed at construction (from --state-dir CLI flag)
  2. SYSFORGE_STATE_DIR environment variable
  3. /var/lib/sysforge (default)

Public API:
    PipelineState(state_dir)
"""
import os
import tomllib
from sysforge import log
from sysforge.primitives.paths import USER_STATE_DIR
_log = log.get_logger("STATE")
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_DEFAULT_STATE_DIR = Path("/var/lib/sysforge")
_FALLBACK_STATE_DIR = USER_STATE_DIR


def _state_dir_is_writable(path: Path) -> bool:
    """Return True if path exists and is writable, or its parent is writable (so mkdir can succeed)."""
    if path.exists():
        return os.access(path, os.W_OK)
    return path.parent.exists() and os.access(path.parent, os.W_OK)


def resolve_state_dir(cli_override=None):
    """
    Resolve the state directory from CLI flag, env var, or default.
    Returns (Path, source_str) where source_str describes which was used.
    Logs both CLI and env sources whenever present.

    If the system default (/var/lib/sysforge) is not writable (e.g. running
    from source without root), falls back to ~/.config/sysforge/state and logs
    a one-time info message so the location is transparent.
    """
    env_val = os.environ.get("SYSFORGE_STATE_DIR")
    sources = []

    if cli_override:
        sources.append(f"--state-dir={cli_override}")
    if env_val:
        sources.append(f"SYSFORGE_STATE_DIR={env_val}")

    if sources:
        _log.info(f"State dir source(s) found: {', '.join(sources)}")

    if cli_override:
        chosen = Path(cli_override)
        _log.info(f"Using state dir (--state-dir takes priority): {chosen}")
        return chosen, "--state-dir"

    if env_val:
        chosen = Path(env_val)
        _log.info(f"Using state dir (SYSFORGE_STATE_DIR): {chosen}")
        return chosen, "SYSFORGE_STATE_DIR"

    if not _state_dir_is_writable(_DEFAULT_STATE_DIR):
        _log.info(
            f"State dir {_DEFAULT_STATE_DIR} is not writable — "
            f"falling back to {_FALLBACK_STATE_DIR} "
            "(set SYSFORGE_STATE_DIR or install sysforge via PKGBUILD to use /var/lib/sysforge)",
        )
        return _FALLBACK_STATE_DIR, "xdg-fallback"

    return _DEFAULT_STATE_DIR, "default"


# ---------------------------------------------------------------------------
# Valid values
# ---------------------------------------------------------------------------

STAGE_STATUSES = {"pending", "running", "done", "failed", "skipped_to"}
PACKAGE_STATUSES = {"pending", "building", "built", "failed", "skipped"}


# ---------------------------------------------------------------------------
# PipelineState
# ---------------------------------------------------------------------------

class PipelineState:
    """
    Read/write wrapper around pipeline_state.toml.

    The state file is the authoritative record of pipeline progress. It is
    written after every status transition so a crash leaves a valid checkpoint.

    State file is TOML for human readability — useful for manual recovery
    when a stage fails and needs intervention before resuming.
    """

    def __init__(self, state_dir):
        self._dir = Path(state_dir)
        self.path = self._dir / "pipeline_state.toml"
        self._data = self._load()

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load(self):
        if not self.path.exists():
            return {"meta": {}, "stages": {}}
        with open(self.path, "rb") as f:
            return tomllib.load(f)

    def save(self):
        """Write current state to disk atomically (write + rename)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(self._serialize())
        tmp.rename(self.path)

    def _now(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _escape_toml_str(s):
        """Escape a string for use in a TOML basic (double-quoted) string."""
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        # Escape remaining control characters as \uXXXX
        return "".join(
            c if ord(c) >= 0x20 and ord(c) != 0x7F else f"\\u{ord(c):04x}"
            for c in s
        )

    def _serialize(self):
        """
        Serialize state dict to TOML string.
        Handles the fixed known structure — not a general-purpose TOML writer.
        """
        lines = [
            "# SysForge pipeline state",
            "# Do not edit while the pipeline is running.",
            "",
        ]

        meta = self._data.get("meta", {})
        if meta:
            lines.append("[meta]")
            for key in ("started_at", "last_updated"):
                if key in meta:
                    lines.append(f'{key} = "{meta[key]}"')
            lines.append("")

        for stage_name, stage_data in self._data.get("stages", {}).items():
            if "status" not in stage_data:
                continue  # progress-only entry not yet formally assigned a status
            lines.append(f"[stages.{stage_name}]")
            lines.append(f'status = "{stage_data["status"]}"')
            for key in ("started_at", "completed_at"):
                if key in stage_data:
                    lines.append(f'{key} = "{stage_data[key]}"')
            if "error" in stage_data:
                lines.append(f'error = "{self._escape_toml_str(stage_data["error"])}"')
            lines.append("")

            progress = stage_data.get("progress")
            if progress:
                lines.append(f"[stages.{stage_name}.progress]")
                for key in ("built", "failed", "skipped", "remaining"):
                    if key in progress:
                        items = ", ".join(f'"{v}"' for v in progress[key])
                        lines.append(f"{key} = [{items}]")
                lines.append("")

            errors = stage_data.get("errors")
            if errors:
                lines.append(f"[stages.{stage_name}.errors]")
                for pkgname, errmsg in errors.items():
                    lines.append(f'{pkgname} = "{self._escape_toml_str(errmsg)}"')
                lines.append("")

            result = stage_data.get("result")
            if result:
                lines.append(f"[stages.{stage_name}.result]")
                for key, val in result.items():
                    if isinstance(val, str):
                        lines.append(f'{key} = "{self._escape_toml_str(val)}"')
                    else:
                        lines.append(f"{key} = {val!r}")
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def init_meta(self):
        """Record pipeline start time. No-op if already set."""
        if "started_at" not in self._data.get("meta", {}):
            self._data.setdefault("meta", {})["started_at"] = self._now()
        self._data["meta"]["last_updated"] = self._now()

    def touch(self):
        """Update last_updated timestamp."""
        self._data.setdefault("meta", {})["last_updated"] = self._now()

    # ------------------------------------------------------------------
    # Stage status
    # ------------------------------------------------------------------

    def stage_status(self, name):
        """Return stage status string, or 'pending' if not yet seen."""
        return self._data.get("stages", {}).get(name, {}).get("status", "pending")

    def mark_running(self, name):
        self._set_stage(name, status="running", started_at=self._now())

    def mark_done(self, name):
        self._set_stage(name, status="done", completed_at=self._now())

    def mark_failed(self, name, error=""):
        self._set_stage(name, status="failed", completed_at=self._now(), error=error)

    def mark_skipped_to(self, name):
        """Mark a stage as skipped-to (bypassed by --start-from)."""
        self._set_stage(name, status="skipped_to")

    def _set_stage(self, name, **kwargs):
        stages = self._data.setdefault("stages", {})
        stage = stages.setdefault(name, {})
        stage.update(kwargs)
        self.touch()

    # ------------------------------------------------------------------
    # Package progress (packages stage only)
    # ------------------------------------------------------------------

    def _progress(self):
        stages = self._data.setdefault("stages", {})
        pkg_stage = stages.setdefault("packages", {})
        # Ensure the stage entry has a status so _serialize doesn't skip it.
        # "running" is correct — progress only exists while the stage is active.
        pkg_stage.setdefault("status", "running")
        return pkg_stage.setdefault("progress", {
            "built": [],
            "failed": [],
            "skipped": [],
            "remaining": [],
        })

    def init_package_list(self, names):
        """
        Initialise progress for the packages stage from a full ordered list.
        Only sets remaining if it hasn't been set yet (idempotent on resume).
        """
        p = self._progress()
        if not p.get("remaining") and not p.get("built"):
            p["remaining"] = list(names)
        self.touch()

    def mark_package_building(self, name):
        p = self._progress()
        # Remove from remaining; will be re-added to built/failed on completion
        p["remaining"] = [n for n in p.get("remaining", []) if n != name]
        self.touch()

    def mark_package_built(self, name):
        p = self._progress()
        p.setdefault("built", [])
        if name not in p["built"]:
            p["built"].append(name)
        p["remaining"] = [n for n in p.get("remaining", []) if n != name]
        p["failed"] = [n for n in p.get("failed", []) if n != name]
        self.touch()

    def mark_package_failed(self, name, error=""):
        p = self._progress()
        if name not in p.get("failed", []):
            p.setdefault("failed", []).append(name)
        p["remaining"] = [n for n in p.get("remaining", []) if n != name]
        # Store error per-package under a sub-key
        stage = self._data["stages"]["packages"]
        stage.setdefault("errors", {})[name] = error
        self.touch()

    def mark_package_skipped(self, name):
        p = self._progress()
        if name not in p.get("skipped", []):
            p.setdefault("skipped", []).append(name)
        p["remaining"] = [n for n in p.get("remaining", []) if n != name]
        p["failed"] = [n for n in p.get("failed", []) if n != name]
        self.touch()

    def get_package_progress(self):
        """
        Return a copy of the current package progress dict.
        Keys: built, failed, skipped, remaining.
        """
        return dict(self._progress())

    def get_package_errors(self):
        """Return dict of {pkgname: error_str} for failed packages."""
        return dict(
            self._data.get("stages", {})
            .get("packages", {})
            .get("errors", {})
        )

    # ------------------------------------------------------------------
    # Stage result (toolchain compiler propagation)
    # ------------------------------------------------------------------

    def set_stage_result(self, stage_name: str, result: dict):
        """Write result key/value data for a stage (e.g. toolchain compiler paths)."""
        stages = self._data.setdefault("stages", {})
        stage = stages.setdefault(stage_name, {})
        stage["result"] = dict(result)
        self.touch()

    def get_stage_result(self, stage_name: str) -> dict:
        """Read result data for a stage. Returns empty dict if not set."""
        return dict(
            self._data.get("stages", {})
            .get(stage_name, {})
            .get("result", {})
        )
