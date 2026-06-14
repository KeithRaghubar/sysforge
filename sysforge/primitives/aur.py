# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
aur.py — AUR RPC queries, git clone, and AUR name-cache refresh

Public API:
    repo_packages(names)              -> set[str]          subset of names present in pacman sync DBs
    is_repo_package(name)             -> bool              True if name is in pacman sync DBs
    aur_info(names)                   -> dict[str, dict]   batch AUR RPC v5 query (name → result)
    aur_clone(name, dest, *, ref=...) -> None              git clone from AUR into dest
    fetch_aur_name_cache()            -> Path | None       refresh ~/.cache/sysforge/aur-packages.txt

Two neighbouring concerns were split into their own modules and are re-exported
from here so existing ``from sysforge.primitives.aur import …`` call sites and
``patch("sysforge.primitives.aur.<name>")`` targets are unchanged:

  * local git plumbing — ``git_fetch_and_compare`` / ``git_is_dirty`` /
    ``purge_src`` / ``classify_head_vs_upstream`` and the
    ``TRANSIENT_GIT_ERRORS`` / ``RATE_LIMIT_GIT_ERRORS`` classifiers —
    now lives in ``primitives.git_ops``.
  * pre-build source acquisition — ``pkgctl_checkout`` (pkgctl repo clone)
    and ``import_pgp_keys`` (validpgpkey setup) — now lives in
    ``primitives.build_prep``.
"""
import gzip
import json
import re as _re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sysforge import log
from sysforge.primitives.paths import USER_CACHE_DIR

# git_ops (local git plumbing) and build_prep (pkgctl checkout + GPG key import)
# were split out of this module; re-export their public surface so existing
# ``from sysforge.primitives.aur import …`` imports and
# ``patch("sysforge.primitives.aur.<name>")`` targets keep working. The names
# are listed in __all__ below, which marks them as an intentional re-export for
# the linter.
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
from sysforge.primitives.build_prep import import_pgp_keys, pkgctl_checkout

# One module, one tag. The RPC-query/clone narration previously logged under a
# separate [MANIFEST] tag, but it is the same concern as the name-cache refresh
# (talking to the AUR) — collapsed to a single [AUR] in P3.5.
_log          = log.get_logger("AUR")

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
        _log.warn(f"AUR RPC query failed: {e}")
        return {}

    results = data.get("results", [])
    found = {r["Name"]: r for r in results}
    _log.info(f"AUR RPC: {len(found)}/{len(names)} found")
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
            _log.info(f"name cache is fresh ({int(age)}s old) — skipping refresh")
            return cache

    _log.info(f"refreshing AUR name cache → {cache}")
    try:
        with urllib.request.urlopen(AUR_PACKAGES_URL, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        names = gzip.decompress(raw).decode().splitlines()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("\n".join(n for n in names if n) + "\n")
        _log.info(f"AUR name cache updated: {len(names)} packages")
        return cache
    except (urllib.error.URLError, OSError, EOFError) as e:
        _log.warn(f"failed to refresh AUR name cache: {e}")
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

    _log.info(f"Cloning {name!r} from AUR → {dest}")
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
                _log.warn(
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
            _log.warn(f"{name!r}: AUR clone hit transient error, retrying...")
            time.sleep(2)
            continue

        raise RuntimeError(f"AUR clone failed for {name!r}:\n{stderr}")
