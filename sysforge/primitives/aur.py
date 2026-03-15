"""
aur.py — AUR RPC queries and git clone helpers

Public API:
    aur_info(names)        -> dict[str, dict]   batch info query (name → result)
    aur_clone(name, dest)  -> None              git clone from AUR into dest
"""
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import sysforge.log as _log


AUR_RPC_URL  = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT_BASE = "https://aur.archlinux.org"

_REQUEST_TIMEOUT = 10   # seconds


def aur_info(names: list[str]) -> dict[str, dict]:
    """
    Batch query AUR RPC v5 for the given package names.

    Returns a dict mapping found package names to their AUR result dicts.
    Returns an empty dict on network error, timeout, or if names is empty.
    Silent on failure — callers treat an empty result as "not found".
    """
    if not names:
        return {}

    # AUR RPC expects literal arg[] keys; urllib.parse.urlencode percent-encodes
    # brackets by default, so build the query string manually.
    query = "&".join(f"arg[]={urllib.parse.quote(n)}" for n in names)
    url = f"{AUR_RPC_URL}?{query}"

    try:
        with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        _log.warn("[MANIFEST]", f"AUR RPC query failed: {e}")
        return {}

    results = data.get("results", [])
    found = {r["Name"]: r for r in results}
    _log.info("[MANIFEST]", f"AUR RPC: {len(found)}/{len(names)} found")
    return found


def aur_clone(name: str, dest: Path) -> None:
    """
    Clone an AUR package repository into dest via git.

    Raises RuntimeError on clone failure.
    """
    url = f"{AUR_GIT_BASE}/{name}.git"
    _log.info("[MANIFEST]", f"Cloning {name!r} from AUR → {dest}")
    result = subprocess.run(
        ["git", "clone", url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"AUR clone failed for {name!r}:\n{result.stderr.strip()}"
        )
