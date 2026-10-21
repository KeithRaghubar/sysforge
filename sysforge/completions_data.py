# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
completions_data.py — candidate lists for the shell completion scripts.

``sysforge completions RESOURCE`` prints one candidate per line for
``completions/_sysforge`` and ``completions/sysforge.bash``. It runs on every
Tab press, so ``cli.main`` hands it straight to :func:`main` before the verb
registry, the runner or any startup logging load (3.5.0-B124). Keep this
module's top-level imports to the stdlib plus ``primitives.config``/``paths``:
``tests/test_completions_speed.py`` fails if the fast path pulls in the verb
framework or a command module.

The repo+AUR universe (``packages``, ~140k names) is cached in
:data:`PACKAGES_CACHE`, keyed on the pacman sync DBs, the AUR name list and
``pkgbuild_src_dir``, so a Tab press with a fresh cache never runs pacman.

Public API:
    RESOURCES, PACKAGES_CACHE, add_arguments(parser), emit(args), main(argv)
"""
import argparse
import os
import sys
from pathlib import Path

from sysforge.primitives.config import expand_package_groups, load_config
from sysforge.primitives.paths import USER_CACHE_DIR, resolve_packages_path

RESOURCES = ("packages", "manifest", "local", "state", "makepkg-flags")

#: The merged local+repo+AUR list behind ``packages``. Line 1 is the input key.
PACKAGES_CACHE = USER_CACHE_DIR / "completion-packages.txt"
_SYNC_DB_DIR = Path("/var/lib/pacman/sync")
_KEY_PREFIX = "# key "


def add_arguments(p: argparse.ArgumentParser) -> None:
    """The ``completions`` arguments, shared by the fast path and the verb."""
    p.add_argument("resource", choices=RESOURCES)
    p.add_argument("--prefix", default="",
        help="Print only candidates starting with PREFIX.")
    p.add_argument("--limit", type=int, default=0, metavar="N",
        help="Print at most N candidates (0 = no limit).")


def main(argv: list[str]) -> int:
    """Fast-path entry from ``cli.main``: parse *argv* and print candidates."""
    p = argparse.ArgumentParser(prog="sysforge completions")
    add_arguments(p)
    return emit(p.parse_args(argv))


def emit(args) -> int:
    """Print the candidates for ``args.resource``, filtered by ``--prefix`` and
    capped at ``--limit``. Returns the exit code (always 0)."""
    prefix = getattr(args, "prefix", "") or ""
    limit = getattr(args, "limit", 0) or 0
    shown = 0
    out = []
    for line in _candidates(args.resource):
        if prefix and not line.startswith(prefix):
            continue
        out.append(line)
        shown += 1
        if limit and shown >= limit:
            break
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


def _candidates(resource: str):
    config = load_config() or {}
    if resource == "makepkg-flags":
        return _makepkg_flags()
    if resource == "state":
        return _state_names()
    if resource == "manifest":
        return _manifest_names(config)
    if resource == "local":
        return _local_names(config)
    return _universe(config)


def _makepkg_flags() -> list[str]:
    import re
    from sysforge.primitives import run
    r = run.probe(["makepkg", "--help"])
    text = r.stdout or r.stderr or ""
    exclude = {"-h", "--help", "-V", "--version", "-p", "-m", "--nocolor"}
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s+(-\w),\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
        if m:
            short, long, desc = m.group(1), m.group(2), m.group(3).strip()
            if short not in exclude:
                out.append(f"{short}:{desc}")
            if long not in exclude:
                out.append(f"{long}:{desc}")
            continue
        m = re.match(r"^\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
        if m:
            long, desc = m.group(1), m.group(2).strip()
            if long not in exclude:
                out.append(f"{long}:{desc}")
    return out


def _state_names() -> list[str]:
    from sysforge.primitives import paths
    from sysforge.primitives.build_state import BuildState
    # Read-only: a Tab press must never provision the state dir (that can sudo).
    state_dir, _ = paths.resolve_state_dir(None, provision=False)
    return sorted(BuildState(state_dir).all_packages())


def _manifest_names(config: dict) -> list[str]:
    import tomllib
    pkg_path = resolve_packages_path(config)
    if not pkg_path.exists():
        return []
    with pkg_path.open("rb") as f:
        data = tomllib.load(f)
    return [e["name"] for e in expand_package_groups(data) if e.get("name")]


def _src_dir(config: dict) -> Path | None:
    raw = config.get("paths", {}).get("pkgbuild_src_dir")
    if not raw:
        return None
    d = Path(raw).expanduser()
    return d if d.is_dir() else None


def _local_names(config: dict) -> list[str]:
    d = _src_dir(config)
    if d is None:
        return []
    return [sub.name for sub in sorted(d.iterdir())
            if sub.is_dir() and (sub / "PKGBUILD").exists()]


def _aur_names_path() -> Path:
    # Read through the aur module so its patch point stays the one AUR path,
    # but only when needed: importing it costs ~20 ms of urllib.
    from sysforge.primitives.aur import AUR_CACHE_PATH
    return AUR_CACHE_PATH.expanduser()


def _cache_key(config: dict) -> str:
    """One line naming every input of the universe and its mtime. Any sync DB
    refresh, a repo added or dropped, a new AUR name list, or a dir added to
    ``pkgbuild_src_dir`` changes it; nothing is read beyond a stat."""
    parts = []
    try:
        with os.scandir(_SYNC_DB_DIR) as it:
            dbs = [e for e in it if e.name.endswith(".db")]
    except OSError:
        dbs = []
    for e in sorted(dbs, key=lambda e: e.name):
        try:
            parts.append(f"{e.name}={e.stat().st_mtime_ns}")
        except OSError:
            continue
    for label, path in (("aur", _aur_names_path()), ("src", _src_dir(config))):
        try:
            parts.append(f"{label}={path}:{path.stat().st_mtime_ns}" if path else f"{label}=-")
        except OSError:
            parts.append(f"{label}={path}:-")
    return " ".join(parts)


def _universe(config: dict) -> list[str]:
    key = _cache_key(config)
    try:
        with PACKAGES_CACHE.open(encoding="utf-8") as f:
            if f.readline().rstrip("\n") == _KEY_PREFIX + key:
                return f.read().splitlines()
    except OSError:
        pass
    names = _build_universe(config)
    try:
        from sysforge.primitives.atomic_write import write_state
        write_state(PACKAGES_CACHE, "\n".join([_KEY_PREFIX + key, *names]) + "\n")
    except OSError:
        pass  # an unwritable cache only costs the next Tab a rebuild
    return names


def _build_universe(config: dict) -> list[str]:
    """Local PKGBUILD dirs, then repo packages, then AUR names, each once."""
    from sysforge.primitives import run
    seen: set[str] = set()
    names: list[str] = []

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for name in _local_names(config):
        _add(name)
    r = run.probe(["pacman", "-Ssq"])
    if r.returncode == 0:
        for name in r.stdout.splitlines():
            _add(name)
    aur = _aur_names_path()
    if aur.exists():
        for name in aur.read_text().splitlines():
            _add(name)
    return names
