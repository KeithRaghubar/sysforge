"""
aur.py — AUR RPC queries, git clone, pkgctl checkout, and GPG key import

Public API:
    repo_packages(names)              -> set[str]          subset of names present in pacman sync DBs
    is_repo_package(name)             -> bool              True if name is in pacman sync DBs
    aur_info(names)                   -> dict[str, dict]   batch AUR RPC v5 query (name → result)
    aur_clone(name, dest, *, ref=...) -> None              git clone from AUR into dest
    pkgctl_checkout(name, dest)       -> None              pkgctl repo clone into dest
    import_pgp_keys(pkgmeta)          -> None              recv any missing validpgpkeys
    fetch_aur_name_cache()            -> Path | None       refresh ~/.config/sysforge/cache/aur-packages.txt

Local git plumbing (``git_fetch_and_compare`` / ``git_is_dirty`` /
``purge_src`` / ``classify_head_vs_upstream`` and the
``TRANSIENT_GIT_ERRORS`` / ``RATE_LIMIT_GIT_ERRORS`` classifiers) now lives in
``primitives.git_ops``; it is re-exported from this module so existing
``from sysforge.primitives.aur import …`` call sites are unchanged.
"""
import gzip
import json
import os
import re as _re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sysforge import log
from sysforge.primitives.paths import USER_CACHE_DIR

# Local git plumbing was split out into primitives.git_ops; re-export its
# public surface so existing ``from sysforge.primitives.aur import …`` imports
# and ``patch("sysforge.primitives.aur.<name>")`` targets keep working. The
# names are listed in __all__ below, which marks them as an intentional
# re-export for the linter.
from sysforge.primitives.git_ops import (
    GitFetchOutcome,
    RATE_LIMIT_GIT_ERRORS,
    TRANSIENT_GIT_ERRORS,
    classify_head_vs_upstream,
    git_fetch_and_compare,
    git_is_dirty,
    is_rate_limit_error,
    is_transient_git_error,
    purge_src,
)

_aur_log      = log.get_logger("AUR")
_build_log    = log.get_logger("BUILD")
_manifest_log = log.get_logger("MANIFEST")

__all__ = [
    # AUR RPC / cache / clone / checkout — this module's own surface.
    "aur_info",
    "fetch_aur_name_cache",
    "repo_packages",
    "is_repo_package",
    "pkgctl_checkout",
    "import_pgp_keys",
    "aur_clone",
    "AUR_RPC_URL",
    "AUR_GIT_BASE",
    "AUR_PACKAGES_URL",
    "AUR_CACHE_PATH",
    "AUR_CACHE_MAX_AGE",
    # Re-exported from primitives.git_ops (kept importable from here).
    "GitFetchOutcome",
    "git_fetch_and_compare",
    "classify_head_vs_upstream",
    "git_is_dirty",
    "purge_src",
    "is_transient_git_error",
    "is_rate_limit_error",
    "TRANSIENT_GIT_ERRORS",
    "RATE_LIMIT_GIT_ERRORS",
]


AUR_RPC_URL       = "https://aur.archlinux.org/rpc/v5/info"
AUR_GIT_BASE      = "https://aur.archlinux.org"
AUR_PACKAGES_URL  = "https://aur.archlinux.org/packages.gz"
AUR_CACHE_PATH    = USER_CACHE_DIR / "aur-packages.txt"
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
    Download the AUR package name list (packages.gz) to ~/.config/sysforge/cache/aur-packages.txt.

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

    Output is streamed line-by-line to the build log so progress is visible at
    -vvv on slow networks (cloning from gitlab.archlinux.org can take minutes).

    If ``dest`` exists but has no PKGBUILD, it's a leftover from an aborted
    prior clone — pkgctl exits 0 with "Skip cloning: Directory exists" in
    that case, silently masking the missing checkout. Purge first so the
    re-clone runs (purge_src refuses if the leftover has uncommitted work).
    """
    timeout = timeout or None  # 0 → disable
    if (dest.exists()
            and (dest / ".git").exists()
            and not (dest / "PKGBUILD").exists()):
        purge_src(dest)
    _build_log.info(f"Checking out {name!r} from official repos → {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            ["pkgctl", "repo", "clone", "--protocol=https", name],
            cwd=str(dest.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pkgctl not found on PATH. Install it with: sudo pacman -S --needed devtools"
        )

    output_lines: list[str] = []
    proc_stdout = proc.stdout
    assert proc_stdout is not None  # set above by stdout=subprocess.PIPE

    def _drain():
        for line in proc_stdout:
            stripped = line.rstrip()
            output_lines.append(stripped)
            _build_log.debug(stripped)

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        drainer.join(timeout=1)
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(
            f"pkgctl checkout timed out after {timeout}s for {name!r}. "
            "Increase [git] clone_timeout in sysforge.toml on slow networks."
        )
    drainer.join(timeout=1)

    if proc.returncode != 0:
        raise RuntimeError(
            f"pkgctl checkout failed for {name!r}:\n" + "\n".join(output_lines).strip()
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


def aur_clone(
    name: str,
    dest: Path,
    *,
    timeout: int | None = 60,
    ref: str | None = None,
    depth: int | None = None,
) -> None:
    """
    Clone an AUR package repository into dest via git.

    Retries once with a short pause on timeouts and transient network errors
    (connection resets, recv failures, TLS hiccups) — these are common when
    AUR is under heavy concurrent load. Raises RuntimeError if the retry also
    fails, or immediately on non-transient errors (e.g. repository not found).

    ``ref`` restricts the clone to a specific branch or tag (``--branch``).
    ``depth`` passes ``--depth`` for a shallow clone; useful when the caller
    only needs the current PKGBUILD, not full history.
    """
    timeout = timeout or None  # 0 → disable
    url = f"{AUR_GIT_BASE}/{name}.git"
    extra: list[str] = []
    if depth is not None and depth > 0:
        extra += ["--depth", str(depth)]
    if ref:
        extra += ["--branch", ref]

    _manifest_log.info(f"Cloning {name!r} from AUR → {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(2):
        try:
            result = subprocess.run(
                ["git", "clone", *extra, url, str(dest)],
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
        rate_limited = is_rate_limit_error(stderr)
        transient = is_transient_git_error(stderr)
        # Rate limits are a hard "stop retrying" — any retry extends the window.
        if attempt == 0 and transient and not rate_limited:
            shutil.rmtree(dest, ignore_errors=True)
            _manifest_log.warn(f"{name!r}: AUR clone hit transient error, retrying...")
            time.sleep(2)
            continue

        raise RuntimeError(f"AUR clone failed for {name!r}:\n{stderr}")
