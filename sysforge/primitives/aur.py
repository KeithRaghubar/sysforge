"""
aur.py — AUR RPC queries, git clone, pkgctl checkout, and GPG key import

Public API:
    is_repo_package(name)          -> bool              True if name is in pacman sync DBs
    aur_info(names)                -> dict[str, dict]   batch AUR RPC v5 query (name → result)
    aur_clone(name, dest)          -> None              git clone from AUR into dest
    pkgctl_checkout(name, dest)    -> None              pkgctl repo clone into dest
    import_pgp_keys(pkgmeta)       -> None              recv any missing validpgpkeys
    git_pull_rebase(pkgbuild_dir)  -> None              git pull --rebase; abort+raise on conflict
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


def is_repo_package(name: str) -> bool:
    """Return True if name exists in any pacman sync DB."""
    result = subprocess.run(["pacman", "-Si", name], capture_output=True)
    return result.returncode == 0


def pkgctl_checkout(name: str, dest: Path) -> None:
    """
    Clone the official Arch Linux packaging repo for name into dest via pkgctl.

    pkgctl repo clone <name> run in dest.parent creates dest.parent/<name>/PKGBUILD.
    Raises RuntimeError on failure.
    """
    _log.info("[BUILD]", f"Checking out {name!r} from official repos → {dest}")
    result = subprocess.run(
        ["pkgctl", "repo", "clone", "--protocol=https", name],
        cwd=str(dest.parent),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pkgctl checkout failed for {name!r}:\n{result.stderr.strip()}"
        )


def import_pgp_keys(pkgmeta: dict, pkgbuild_path: Path) -> None:
    """
    Ensure all validpgpkeys in pkgmeta are present in the GPG keyring.

    Strategy (in order):
    1. Import any bundled .asc files from keys/pgp/ next to the PKGBUILD.
    2. Check which validpgpkeys are still missing from the keyring.
    3. Fetch any remaining missing keys via gpg --recv-keys.

    Import/fetch failures are logged as warnings — makepkg will surface a
    clearer error if a key is still absent when signature verification runs.
    """
    keys = pkgmeta.get("globals", {}).get("validpgpkeys", [])
    if not keys:
        return

    _log.info("[BUILD]", f"GPG: {len(keys)} validpgpkey(s) required")

    # Step 1: import bundled keys from keys/pgp/ if present
    keys_dir = pkgbuild_path.parent / "keys" / "pgp"
    if keys_dir.is_dir():
        asc_files = sorted(keys_dir.glob("*.asc"))
        if asc_files:
            _log.info("[BUILD]", f"GPG: importing {len(asc_files)} bundled key(s) from {keys_dir}")
            r = subprocess.run(
                ["gpg", "--import", *[str(f) for f in asc_files]],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _log.warn("[BUILD]", f"GPG: bundled import failed:\n{r.stderr.strip()}")
            else:
                _log.info("[BUILD]", "GPG: bundled import succeeded")

    # Step 2: check which keys are still missing
    missing = [
        key for key in keys
        if subprocess.run(["gpg", "--list-keys", key], capture_output=True).returncode != 0
    ]

    if not missing:
        _log.info("[BUILD]", f"GPG: all {len(keys)} key(s) present in keyring")
        return

    # Step 3: fetch remaining keys from keyserver
    _log.info("[BUILD]", f"GPG: {len(missing)}/{len(keys)} key(s) missing, fetching via keyserver")
    r = subprocess.run(["gpg", "--recv-keys", *missing], capture_output=True, text=True)
    if r.returncode != 0:
        _log.warn("[BUILD]", f"GPG: keyserver fetch failed:\n{r.stderr.strip()}")
    else:
        _log.info("[BUILD]", "GPG: keyserver fetch succeeded")


def git_pull_rebase(pkgbuild_dir: Path) -> None:
    """
    Attempt `git pull --rebase` in pkgbuild_dir before a build.

    - Not a git repo: skips silently (plain directories are fine).
    - No tracking branch: skips with an info log (detached HEAD, local-only repos).
    - Success: logs output at INFO level.
    - Conflict / failure: runs `git rebase --abort` to restore clean state,
      then raises RuntimeError so the caller can handle it cleanly.
    """
    # Check if the directory is inside a git repo at all
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if r.returncode != 0:
        return  # not a git repo — skip silently

    # Check for a configured tracking branch
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse",
         "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _log.info("[GIT]", f"{pkgbuild_dir.name}: no tracking branch — skipping update")
        return

    tracking = r.stdout.strip()
    _log.info("[GIT]", f"Updating {pkgbuild_dir.name} from {tracking}")

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "pull", "--rebase"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            if line.strip():
                _log.info("[GIT]", f"  {line}")
        return

    # Pull failed — abort the rebase to restore a clean state before raising
    _log.error("[GIT]", f"git pull --rebase failed for {pkgbuild_dir.name}:")
    for line in (r.stdout + r.stderr).strip().splitlines():
        _log.error("[GIT]", f"  {line}")
    subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rebase", "--abort"],
        capture_output=True,
    )
    raise RuntimeError(
        f"[GIT] git pull --rebase failed for {pkgbuild_dir.name}. "
        "Resolve conflicts manually and re-run, or use --no-update to skip."
    )


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
