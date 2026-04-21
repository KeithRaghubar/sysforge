"""
aur.py — AUR RPC queries, git clone, pkgctl checkout, and GPG key import

Public API:
    repo_packages(names)           -> set[str]          subset of names present in pacman sync DBs
    repo_packages(names)           -> set[str]          subset of names present in pacman sync DBs (batch)
    is_repo_package(name)          -> bool              True if name is in pacman sync DBs
    aur_info(names)                -> dict[str, dict]   batch AUR RPC v5 query (name → result)
    aur_clone(name, dest)          -> None              git clone from AUR into dest
    pkgctl_checkout(name, dest)    -> None              pkgctl repo clone into dest
    import_pgp_keys(pkgmeta)       -> None              recv any missing validpgpkeys
    git_pull_rebase(pkgbuild_dir)  -> None              git pull --rebase; abort+raise on conflict
    git_is_dirty(pkgbuild_dir)    -> bool              True if git repo has uncommitted changes
    purge_src(pkgbuild_dir)        -> None              rm -rf pkgbuild_dir; fatal if git_is_dirty
    fetch_aur_name_cache()         -> Path | None       refresh ~/.cache/sysforge/aur-packages.txt
"""
import gzip
import json
import os
import re as _re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sysforge import log
_aur_log      = log.get_logger("AUR")
_build_log    = log.get_logger("BUILD")
_git_log      = log.get_logger("GIT")
_manifest_log = log.get_logger("MANIFEST")


AUR_RPC_URL       = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT_BASE      = "https://aur.archlinux.org"
AUR_PACKAGES_URL  = "https://aur.archlinux.org/packages.gz"
AUR_CACHE_PATH    = Path("~/.cache/sysforge/aur-packages.txt")
AUR_CACHE_MAX_AGE = 86400   # 1 day in seconds

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
        _manifest_log.warn(f"AUR RPC query failed: {e}")
        return {}

    results = data.get("results", [])
    found = {r["Name"]: r for r in results}
    _manifest_log.info(f"AUR RPC: {len(found)}/{len(names)} found")
    return found


def fetch_aur_name_cache(force: bool = False) -> Path | None:
    """
    Download the AUR package name list (packages.gz) to ~/.cache/sysforge/aur-packages.txt.

    Skips the download if the cache file is less than AUR_CACHE_MAX_AGE seconds old,
    unless force=True.  Returns the cache path on success, None on failure.
    Network errors are logged as warnings and do not propagate.
    """
    cache = AUR_CACHE_PATH.expanduser()

    if not force and cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < AUR_CACHE_MAX_AGE:
            _aur_log.info(f"name cache is fresh ({int(age)}s old) — skipping refresh")
            return cache

    _aur_log.info(f"refreshing AUR name cache → {cache}")
    try:
        with urllib.request.urlopen(AUR_PACKAGES_URL, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        names = gzip.decompress(raw).decode().splitlines()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(n for n in names if n) + "\n")
        _aur_log.info(f"AUR name cache updated: {len(names)} packages")
        return cache
    except (urllib.error.URLError, OSError, EOFError) as e:
        _aur_log.warn(f"failed to refresh AUR name cache: {e}")
        return None


def repo_packages(names: list[str]) -> set[str]:
    """Return the subset of names present in any pacman sync DB.

    Runs a single pacman -Si invocation for all names — O(1) subprocesses.
    Packages not found produce errors to stderr only; found packages appear in stdout.
    """
    if not names:
        return set()
    result = subprocess.run(["pacman", "-Si", *names], capture_output=True, text=True)
    found: set[str] = set()
    for line in result.stdout.splitlines():
        m = _re.match(r"^Name\s*:\s*(\S+)", line)
        if m:
            found.add(m.group(1))
    return found


def is_repo_package(name: str) -> bool:
    """Return True if name exists in any pacman sync DB."""
    result = subprocess.run(["pacman", "-Si", name], capture_output=True)
    return result.returncode == 0


def pkgctl_checkout(name: str, dest: Path, *, timeout: int | None = 60) -> None:
    """
    Clone the official Arch Linux packaging repo for name into dest via pkgctl.

    pkgctl repo clone <name> run in dest.parent creates dest.parent/<name>/PKGBUILD.
    Raises RuntimeError on failure or timeout.
    """
    timeout = timeout or None  # 0 → disable
    _build_log.info(f"Checking out {name!r} from official repos → {dest}")
    try:
        result = subprocess.run(
            ["pkgctl", "repo", "clone", "--protocol=https", name],
            cwd=str(dest.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(
            f"pkgctl checkout timed out after {timeout}s for {name!r}"
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

    _build_log.info(f"GPG: {len(keys)} validpgpkey(s) required")

    # Step 1: import bundled keys from keys/pgp/ if present
    keys_dir = pkgbuild_path.parent / "keys" / "pgp"
    if keys_dir.is_dir():
        asc_files = sorted(keys_dir.glob("*.asc"))
        if asc_files:
            _build_log.info(f"GPG: importing {len(asc_files)} bundled key(s) from {keys_dir}")
            r = subprocess.run(
                ["gpg", "--import", *[str(f) for f in asc_files]],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _build_log.warn(f"GPG: bundled import failed:\n{r.stderr.strip()}")
            else:
                _build_log.info("GPG: bundled import succeeded")

    # Step 2: check which keys are still missing
    missing = [
        key for key in keys
        if subprocess.run(["gpg", "--list-keys", key], capture_output=True).returncode != 0
    ]

    if not missing:
        _build_log.info(f"GPG: all {len(keys)} key(s) present in keyring")
        return

    # Step 3: fetch remaining keys from keyserver
    _build_log.info(f"GPG: {len(missing)}/{len(keys)} key(s) missing, fetching via keyserver")
    r = subprocess.run(["gpg", "--recv-keys", *missing], capture_output=True, text=True)
    if r.returncode != 0:
        _build_log.warn(f"GPG: keyserver fetch failed:\n{r.stderr.strip()}")
    else:
        _build_log.info("GPG: keyserver fetch succeeded")


def git_pull_rebase(pkgbuild_dir: Path, *, timeout: int | None = 30) -> None:
    """
    Attempt `git pull --rebase` in pkgbuild_dir before a build.

    - Not a git repo: skips silently (plain directories are fine).
    - No tracking branch: skips with an info log (detached HEAD, local-only repos).
    - Success: logs output at INFO level.
    - Timeout: runs `git rebase --abort` to restore clean state, raises
      RuntimeError. The caller (`_sync_sources` sub-phase 4) falls back to
      purge + re-clone, which reliably succeeds even when AUR is throttling
      concurrent pulls.
    - Conflict / failure: runs `git rebase --abort` to restore clean state,
      then raises RuntimeError so the caller can handle it cleanly.
    """
    timeout = timeout or None  # 0 → disable
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
        _git_log.info(f"{pkgbuild_dir.name}: no tracking branch — skipping update")
        return

    tracking = r.stdout.strip()
    _git_log.info(f"Updating {pkgbuild_dir.name} from {tracking}")

    try:
        r = subprocess.run(
            ["git", "-C", str(pkgbuild_dir), "pull", "--rebase"],
            capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _git_log.error(f"git pull --rebase timed out after {timeout}s for {pkgbuild_dir.name}")
        subprocess.run(
            ["git", "-C", str(pkgbuild_dir), "rebase", "--abort"],
            capture_output=True,
        )
        raise RuntimeError(
            f"git pull --rebase timed out after {timeout}s for {pkgbuild_dir.name}. "
            "Check network connectivity, or increase [git] pull_timeout in sysforge.toml."
        )

    if r.returncode == 0:
        for line in r.stdout.strip().splitlines():
            if line.strip():
                _git_log.info(f"  {pkgbuild_dir.name}: {line}")
        return

    # Pull failed — abort the rebase to restore a clean state before raising
    combined = (r.stdout + r.stderr).strip()
    _git_log.error(f"git pull --rebase failed for {pkgbuild_dir.name}:")
    for line in combined.splitlines():
        _git_log.error(f"  {line}")
    subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rebase", "--abort"],
        capture_output=True,
    )
    # Keep the stderr in the RuntimeError so callers can spot transient
    # network failures (Connection reset, Recv failure, ...) and react.
    raise RuntimeError(
        f"git pull --rebase failed for {pkgbuild_dir.name}: "
        f"{combined or 'no output'}"
    )


def git_is_dirty(pkgbuild_dir: Path) -> bool:
    """
    Return True if pkgbuild_dir is a git repo with local modifications, defined as:

    1. Uncommitted changes — staged or unstaged modifications to tracked files.
       (Untracked files such as build artifacts are intentionally ignored.)
    2. Unpushed commits — commits that exist locally but not on the tracking branch.
    3. No tracking branch — the repo is entirely local with no upstream to compare
       against, so it is treated as dirty by definition.

    Returns False if the directory is not a git repo or is clean and fully in sync
    with its tracking branch.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if r.returncode != 0:
        return False

    # Check 1: uncommitted changes (tracked files only)
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "status",
         "--short", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return True

    # Check 2: verify there is a tracking branch
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse",
         "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # No upstream configured — treat as dirty (entirely local repo)
        return True

    # Check 3: count commits on HEAD that are not on the upstream
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-list", "--count", "@{u}..HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        count = r.stdout.strip()
        return count.isdigit() and int(count) > 0

    return False


def purge_src(pkgbuild_dir: Path) -> None:
    """
    Remove pkgbuild_dir to allow a fresh clone on the next build.

    Refuses (raises RuntimeError) if the directory is a git repo with local
    work that would be destroyed: uncommitted tracked changes, unpushed
    commits, or no upstream tracking branch (entirely-local repo).

    A non-existent directory is a no-op. A non-git directory is purged
    unconditionally — it has no commit history to protect.
    """
    if not pkgbuild_dir.exists():
        return

    is_git = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    ).returncode == 0

    if is_git and git_is_dirty(pkgbuild_dir):
        raise RuntimeError(
            f"refusing to purge {pkgbuild_dir}: uncommitted changes, "
            "unpushed commits, or no upstream tracking branch — "
            "resolve manually or commit/push before retrying"
        )

    _git_log.warn(f"purging {pkgbuild_dir} for clean re-clone")
    shutil.rmtree(pkgbuild_dir)


TRANSIENT_GIT_ERRORS = (
    "Connection reset",
    "Recv failure",
    "Could not resolve host",
    "TLS connection",
    "early EOF",
    "RPC failed",
)

# Hard-stop errors: AUR is explicitly refusing us. Retrying extends the
# penalty window, so callers must break immediately instead of looping.
RATE_LIMIT_GIT_ERRORS = (
    "error: 429",
    "Too Many Requests",
    "error: 503",
    "error: 502",
)


def aur_clone(name: str, dest: Path, *, timeout: int | None = 60) -> None:
    """
    Clone an AUR package repository into dest via git.

    Retries once with a short pause on timeouts and transient network errors
    (connection resets, recv failures, TLS hiccups) — these are common when
    AUR is under heavy concurrent load. Raises RuntimeError if the retry also
    fails, or immediately on non-transient errors (e.g. repository not found).
    """
    timeout = timeout or None  # 0 → disable
    url = f"{AUR_GIT_BASE}/{name}.git"
    _manifest_log.info(f"Cloning {name!r} from AUR → {dest}")

    for attempt in range(2):
        try:
            result = subprocess.run(
                ["git", "clone", url, str(dest)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(dest, ignore_errors=True)
            if attempt == 0:
                _manifest_log.warn(
                    f"{name!r}: AUR clone timed out after {timeout}s, retrying..."
                )
                time.sleep(2)
                continue
            raise RuntimeError(
                f"AUR clone timed out after {timeout}s for {name!r} (after retry). "
                "Check network connectivity, or increase [git] clone_timeout in sysforge.toml."
            )

        if result.returncode == 0:
            return

        stderr = result.stderr.strip()
        rate_limited = any(marker in stderr for marker in RATE_LIMIT_GIT_ERRORS)
        transient = any(marker in stderr for marker in TRANSIENT_GIT_ERRORS)
        # Rate limits are a hard "stop retrying" — any retry extends the window.
        if attempt == 0 and transient and not rate_limited:
            shutil.rmtree(dest, ignore_errors=True)
            _manifest_log.warn(f"{name!r}: AUR clone hit transient error, retrying...")
            time.sleep(2)
            continue

        raise RuntimeError(f"AUR clone failed for {name!r}:\n{stderr}")
