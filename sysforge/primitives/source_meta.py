# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
source_meta.py — persistent PKGBUILD metadata cache for `sysforge update`.

Stores per-pkgbase snapshots of the last-known AUR RPC version, RPC
LastModified timestamp, and local HEAD commit so `source_sync.py` can
short-circuit shallow git fetches when nothing has changed.

Cache path: ``$SYSFORGE_STATE_DIR/source_meta.toml`` (alongside
``build_state.toml``; state dir resolved via
``sysforge.pipeline.state.resolve_state_dir``).

Schema::

    schema_version = 1
    last_rpc_at = "2026-04-21T12:34:56Z"

    [mesa-git]
    rpc_version       = "24.0-1"
    rpc_last_modified = 1718900000
    rpc_package_base  = "mesa-git"
    pkgbuild_sha256   = "ab12..."   # reserved for future use
    head_commit       = "deadbeef..."
    last_fetch_at     = "2026-04-21T12:34:56Z"
    is_vcs            = true

The file is keyed by pkgbase — matches ``group_by_pkgbase()`` in
``build_state.py``. Atomic write-then-rename matches ``BuildState.save()``.
"""
import tomllib
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SourceMetaCache:
    """Read/write wrapper around source_meta.toml.

    Loaded once at construction, mutated in memory, persisted via ``save()``.
    Callers call ``update()`` after every successful fetch, then ``save()``
    once at the end of the run (scheduler does this via ``close()``).
    """

    def __init__(self, state_dir: Path):
        self._dir = Path(state_dir)
        self.path = self._dir / "source_meta.toml"
        self._data, self._meta = self._load()

    def _load(self) -> tuple[dict[str, dict], dict]:
        if not self.path.exists():
            return {}, {"schema_version": SCHEMA_VERSION}
        try:
            with open(self.path, "rb") as f:
                raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return {}, {"schema_version": SCHEMA_VERSION}

        meta = {
            "schema_version": raw.pop("schema_version", SCHEMA_VERSION),
        }
        if "last_rpc_at" in raw:
            meta["last_rpc_at"] = raw.pop("last_rpc_at")

        if meta["schema_version"] != SCHEMA_VERSION:
            # Forward-incompat schema: discard entries but keep the file so
            # the next save rewrites with the current version.
            return {}, {"schema_version": SCHEMA_VERSION}

        # Entries are top-level TOML tables keyed by pkgbase.
        entries = {
            k: dict(v) for k, v in raw.items()
            if isinstance(v, dict)
        }
        return entries, meta

    def get(self, pkgbase: str) -> dict | None:
        entry = self._data.get(pkgbase)
        return dict(entry) if entry is not None else None

    def all(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._data.items()}

    def delete(self, pkgbase: str) -> bool:
        return self._data.pop(pkgbase, None) is not None

    def update(
        self,
        pkgbase: str,
        *,
        rpc_version: str | None = None,
        rpc_last_modified: int | None = None,
        rpc_package_base: str | None = None,
        head_commit: str | None = None,
        is_vcs: bool | None = None,
        pkgbuild_sha256: str | None = None,
        last_fetch_at: str | None = None,
    ) -> None:
        """Merge new fields into the entry for pkgbase. Unset fields preserved."""
        entry = self._data.setdefault(pkgbase, {})
        if rpc_version is not None:
            entry["rpc_version"] = rpc_version
        if rpc_last_modified is not None:
            entry["rpc_last_modified"] = int(rpc_last_modified)
        if rpc_package_base is not None:
            entry["rpc_package_base"] = rpc_package_base
        if head_commit is not None:
            entry["head_commit"] = head_commit
        if is_vcs is not None:
            entry["is_vcs"] = bool(is_vcs)
        if pkgbuild_sha256 is not None:
            entry["pkgbuild_sha256"] = pkgbuild_sha256
        if last_fetch_at is not None:
            entry["last_fetch_at"] = last_fetch_at

    def mark_rpc_sync(self, timestamp: str | None = None) -> None:
        """Record the timestamp of the last successful RPC batch query."""
        self._meta["last_rpc_at"] = timestamp or _now_iso()

    def last_rpc_at(self) -> str | None:
        return self._meta.get("last_rpc_at")

    def save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(self._serialize())
        tmp.rename(self.path)

    def _serialize(self) -> str:
        lines = [
            "# SysForge source metadata cache",
            "# Populated by sysforge.primitives.source_sync; safe to delete.",
            "",
            f"schema_version = {SCHEMA_VERSION}",
        ]
        if self._meta.get("last_rpc_at"):
            lines.append(f'last_rpc_at = "{self._meta["last_rpc_at"]}"')
        lines.append("")

        for pkgbase in sorted(self._data):
            entry = self._data[pkgbase]
            lines.append(f'["{_escape(pkgbase)}"]')
            for key in (
                "rpc_version",
                "rpc_last_modified",
                "rpc_package_base",
                "pkgbuild_sha256",
                "head_commit",
                "last_fetch_at",
                "is_vcs",
            ):
                if key not in entry:
                    continue
                val = entry[key]
                if isinstance(val, bool):
                    lines.append(f"{key} = {'true' if val else 'false'}")
                elif isinstance(val, int):
                    lines.append(f"{key} = {val}")
                else:
                    lines.append(f'{key} = "{_escape(str(val))}"')
            lines.append("")
        return "\n".join(lines)


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
