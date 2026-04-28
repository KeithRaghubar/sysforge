"""
build_state.py — build history tracking

Manages /var/lib/sysforge/build_state.toml (or configured override).
Records per-package build metadata after each successful sysforge build.
Used by `sysforge update` to determine which packages need rebuilding.

build_state.toml is a superset of `pacman -Q`: every installed package has
an entry, regardless of whether sysforge built it. Entries are distinguished
by the `build_mode` field:

  - "profiled"  — built by sysforge; carries pkgbuild_dir and flags_string.
  - "pacman"    — installed via pacman; pkgver/pkgrel/epoch parsed from
                  `pacman -Q` output; pkgbuild_dir and flags_string absent.
  - "pgo_llvm_toolchain" — experimental, deferred post-1.0.

`BuildState.sync_with_installed()` keeps the file in lockstep with pacman:
it adds pacman-mode entries for newly installed packages and prunes entries
for packages that are no longer installed.

State dir resolution follows pipeline/state.py (highest priority first):
  1. Explicit Path passed at construction (from --state-dir CLI flag)
  2. SYSFORGE_STATE_DIR environment variable
  3. /var/lib/sysforge (default)

Public API:
    BuildState(state_dir)
    group_by_pkgbase(packages)
    parse_pacman_version(ver_str) -> (epoch, pkgver, pkgrel)
"""
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def parse_pacman_version(ver_str: str) -> tuple[str, str, str]:
    """
    Parse a `pacman -Q` version string into (epoch, pkgver, pkgrel).

    Accepts ``[epoch:]pkgver-pkgrel``. Missing epoch defaults to "0".
    Missing pkgrel defaults to "1". An empty input yields ("0", "", "1").
    """
    if not ver_str:
        return ("0", "", "1")
    rest = ver_str
    epoch = "0"
    if ":" in rest:
        epoch, _, rest = rest.partition(":")
    if "-" in rest:
        pkgver, _, pkgrel = rest.rpartition("-")
    else:
        pkgver, pkgrel = rest, "1"
    return (epoch, pkgver, pkgrel)

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
               built_at: str | None = None,
               built_upstream_commit: str | None = None) -> None:
        """Record build metadata for a single package name.

        ``built_at`` defaults to now; callers performing a repair pass may
        pass the original timestamp to preserve true build history.

        ``built_upstream_commit`` is the resolved upstream git SHA of a
        single-source VCS package at the time of build — read by
        ``sysforge update --devel`` to short-circuit ``pkgver()`` resolution
        via ``git ls-remote``. None for non-VCS or multi-git-source packages.
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
        if built_upstream_commit is not None:
            entry["built_upstream_commit"] = built_upstream_commit
        self._data[pkgname] = entry

    def delete(self, pkgname: str) -> bool:
        """Remove an entry by pkgname.  Returns True if it existed."""
        return self._data.pop(pkgname, None) is not None

    def sync_with_installed(self, installed: dict[str, str]) -> tuple[int, int]:
        """
        Reconcile state with `pacman -Q` output so the file is a superset of
        installed packages.

        Adds a ``build_mode = "pacman"`` entry for every installed package
        that does not already have one, with pkgver/pkgrel/epoch parsed from
        the pacman version string. Prunes entries whose pkgname is no longer
        installed — this also removes zombies left by pre-superset parser
        runs (e.g. keys containing unexpanded ``$_pkgname``).

        Returns (added, removed).
        """
        installed_names = set(installed.keys())
        existing_names = set(self._data.keys())

        removed_names = existing_names - installed_names
        for name in removed_names:
            del self._data[name]

        added_names = installed_names - existing_names
        for name in added_names:
            epoch, pkgver, pkgrel = parse_pacman_version(installed[name])
            self._data[name] = {
                "pkgver": pkgver,
                "pkgrel": pkgrel,
                "epoch": epoch,
                "build_mode": "pacman",
            }

        return (len(added_names), len(removed_names))

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
            for key in ("pkgver", "pkgrel", "epoch", "pkgbase", "pkgbuild_dir", "build_mode", "flags_string", "built_at", "built_upstream_commit"):
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
