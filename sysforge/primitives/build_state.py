"""
build_state.py — build history tracking

Manages /var/lib/sysforge/build_state.toml (or configured override).
Records per-package build metadata after each successful sysforge build.
Used by `sysforge update` to determine which packages need rebuilding.

State dir resolution follows pipeline/state.py (highest priority first):
  1. Explicit Path passed at construction (from --state-dir CLI flag)
  2. SYSFORGE_STATE_DIR environment variable
  3. /var/lib/sysforge (default)

Public API:
    BuildState(state_dir)
    group_by_pkgbase(packages)
"""
import tomllib
from datetime import datetime, timezone
from pathlib import Path

def group_by_pkgbase(packages: dict) -> tuple[dict, dict]:
    """
    Group {pkgname: record} from BuildState.all_packages() by pkgbase.

    Returns:
        pkgbase_map   — {pkgbase: [pkgname, ...]}
        pkgbase_entry — {pkgbase: representative_record}  (first seen wins)
    """
    pkgbase_map: dict[str, list] = {}
    pkgbase_entry: dict[str, dict] = {}
    for pkgname, entry in packages.items():
        base = entry.get("pkgbase", pkgname)
        pkgbase_map.setdefault(base, []).append(pkgname)
        if base not in pkgbase_entry:
            pkgbase_entry[base] = entry
    return pkgbase_map, pkgbase_entry


class BuildState:
    """
    Read/write wrapper around build_state.toml.

    Records per-package build metadata (versions, pkgbuild directory) after
    each successful build. Used by `sysforge update` to determine what needs
    rebuilding.

    The state file is TOML for human readability and manual recovery.
    """

    def __init__(self, state_dir):
        self._dir = Path(state_dir)
        self.path = self._dir / "build_state.toml"
        self._data = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        with open(self.path, "rb") as f:
            return tomllib.load(f)

    def record(self, pkgname: str, pkgver: str, pkgrel: str,
               epoch: str, pkgbase: str, pkgbuild_dir: Path,
               build_mode: str | None = None,
               flags_string: str | None = None,
               built_at: str | None = None) -> None:
        """Record build metadata for a single package name.

        ``built_at`` defaults to now; callers performing a repair pass may
        pass the original timestamp to preserve true build history.
        """
        entry = {
            "pkgver": pkgver,
            "pkgrel": pkgrel,
            "epoch": epoch,
            "pkgbase": pkgbase,
            "pkgbuild_dir": str(pkgbuild_dir),
            "built_at": built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if build_mode is not None:
            entry["build_mode"] = build_mode
        if flags_string is not None:
            entry["flags_string"] = flags_string
        self._data[pkgname] = entry

    def delete(self, pkgname: str) -> bool:
        """Remove an entry by pkgname.  Returns True if it existed."""
        return self._data.pop(pkgname, None) is not None

    def get(self, pkgname: str) -> dict | None:
        """Return build record for pkgname, or None if not recorded."""
        return self._data.get(pkgname)

    def all_packages(self) -> dict[str, dict]:
        """Return all recorded packages as {pkgname: record}."""
        return dict(self._data)

    def save(self) -> None:
        """Write current state to disk atomically (write + rename)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(self._serialize())
        tmp.rename(self.path)

    def _serialize(self) -> str:
        lines = [
            "# SysForge build state",
            "# Records per-package build metadata for use by sysforge update.",
            "",
        ]
        for pkgname, entry in sorted(self._data.items()):
            escaped = pkgname.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'["{escaped}"]')
            for key in ("pkgver", "pkgrel", "epoch", "pkgbase", "pkgbuild_dir", "build_mode", "flags_string", "built_at"):
                if key in entry:
                    val = (
                        str(entry[key])
                        .replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                        .replace("\r", "\\r")
                    )
                    lines.append(f'{key} = "{val}"')
            lines.append("")
        return "\n".join(lines)
