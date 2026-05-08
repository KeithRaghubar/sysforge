"""
aur.py — AUR RPC queries, git clone, pkgctl checkout, and GPG key import

Public API:
    repo_packages(names)              -> set[str]          subset of names present in pacman sync DBs
    is_repo_package(name)             -> bool              True if name is in pacman sync DBs
    aur_info(names)                   -> dict[str, dict]   batch AUR RPC v5 query (name → result)
    aur_clone(name, dest, *, ref=...) -> None              git clone from AUR into dest
    pkgctl_checkout(name, dest)       -> None              pkgctl repo clone into dest
    import_pgp_keys(pkgmeta)          -> None              recv any missing validpgpkeys
    git_fetch_and_compare(dir, ...)   -> GitFetchOutcome   shallow fetch + HEAD compare (no destructive ops)
    git_is_dirty(pkgbuild_dir)        -> bool              True if git repo has uncommitted changes
    purge_src(pkgbuild_dir)           -> None              rm -rf pkgbuild_dir; fatal if git_is_dirty
    fetch_aur_name_cache()            -> Path | None       refresh ~/.config/sysforge/cache/aur-packages.txt

Also exports TRANSIENT_GIT_ERRORS / RATE_LIMIT_GIT_ERRORS string tuples and
``is_transient_git_error()`` / ``is_rate_limit_error()`` classifiers for
callers that need to distinguish network flakes from AUR throttling.
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
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.paths import USER_CACHE_DIR
_aur_log      = log.get_logger("AUR")
_build_log    = log.get_logger("BUILD")
_git_log      = log.get_logger("GIT")
_manifest_log = log.get_logger("MANIFEST")


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


@dataclass
class GitFetchOutcome:
    """Result of a non-destructive shallow fetch + HEAD compare.

    ``status`` values:
      - ``not_a_repo``  — dir isn't a git repo; no action taken.
      - ``no_tracking`` — no upstream configured; no action taken.
      - ``up_to_date``  — HEAD already matches FETCH_HEAD.
      - ``fetched``     — FF-merged FETCH_HEAD onto HEAD.
      - ``diverged``    — can't fast-forward (divergent history or dirty
                          working tree). Local PKGBUILD is kept untouched.
                          Caller can surface and skip; user must rerun with
                          ``--cleansrc`` if they want the destructive reset.
      - ``rate_limited`` — git returned 429/503/502. No working-tree change.
      - ``failed``      — transient or unknown error. No working-tree change.
    """
    status: str
    head_before: str | None
    head_after: str | None
    error: str | None = None


def git_fetch_and_compare(
    pkgbuild_dir: Path,
    *,
    timeout: int | None = 30,
    limiter=None,
) -> GitFetchOutcome:
    """Shallow-fetch upstream and report the outcome without destructive ops.

    Replaces the old `git pull --rebase` flow. Never runs `rebase --abort`,
    `reset --hard`, or `purge_src`; divergence is surfaced via the
    ``diverged`` status so the caller (typically the sync scheduler) can
    decide whether to skip the package or trigger an opt-in recovery.

    When ``limiter`` is provided, the git invocation goes through
    ``rate_limit.run_throttled_git`` so the Retry-After window is enforced
    across RPC and git paths.
    """
    timeout = timeout or None  # 0 → disable

    def _run(cmd: list[str]):
        if limiter is not None:
            from sysforge.primitives.rate_limit import run_throttled_git
            return run_throttled_git(cmd, limiter, timeout=timeout)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # 1. Is this a git repo at all?
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if r.returncode != 0:
        return GitFetchOutcome(status="not_a_repo", head_before=None, head_after=None)

    # 2. Is there a tracking branch?
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse",
         "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        _git_log.info(f"{pkgbuild_dir.name}: no tracking branch — skipping fetch")
        return GitFetchOutcome(status="no_tracking", head_before=None, head_after=None)

    tracking = r.stdout.strip()
    # tracking looks like "origin/master" — split once, right-hand side is the branch.
    if "/" in tracking:
        remote, _, branch = tracking.partition("/")
    else:
        remote, branch = "origin", tracking

    head_before = _rev_parse(pkgbuild_dir, "HEAD")

    _git_log.info(f"Fetching {pkgbuild_dir.name} from {tracking}")

    # 3. Shallow fetch.
    try:
        r = _run(["git", "-C", str(pkgbuild_dir), "fetch",
                  "--depth=1", remote, branch])
    except subprocess.TimeoutExpired:
        err = f"git fetch timed out after {timeout}s"
        _git_log.warn(f"{pkgbuild_dir.name}: {err}")
        return GitFetchOutcome(
            status="failed", head_before=head_before, head_after=None, error=err,
        )

    if r.returncode != 0:
        combined = ((r.stdout or "") + (r.stderr or "")).strip()
        status = "rate_limited" if is_rate_limit_error(combined) else "failed"
        _git_log.warn(f"{pkgbuild_dir.name}: git fetch failed: {combined.splitlines()[0] if combined else 'no output'}")
        return GitFetchOutcome(
            status=status, head_before=head_before, head_after=None, error=combined,
        )

    fetch_head = _rev_parse(pkgbuild_dir, "FETCH_HEAD")
    if fetch_head is None or head_before is None:
        return GitFetchOutcome(
            status="failed", head_before=head_before, head_after=fetch_head,
            error="could not resolve HEAD / FETCH_HEAD after fetch",
        )

    # 4. Up-to-date?
    if head_before == fetch_head:
        return GitFetchOutcome(
            status="up_to_date", head_before=head_before, head_after=head_before,
        )

    # 5. Can we fast-forward? (HEAD is ancestor of FETCH_HEAD AND working tree is clean)
    ancestor = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "merge-base",
         "--is-ancestor", "HEAD", "FETCH_HEAD"],
        capture_output=True,
    )
    if ancestor.returncode != 0 or git_is_dirty(pkgbuild_dir):
        err = (
            f"divergent: HEAD {head_before[:10]} vs FETCH_HEAD {fetch_head[:10]}"
            if ancestor.returncode != 0
            else "working tree has local modifications"
        )
        _git_log.warn(f"{pkgbuild_dir.name}: {err} — keeping local PKGBUILD")
        return GitFetchOutcome(
            status="diverged", head_before=head_before, head_after=fetch_head,
            error=err,
        )

    # 6. Fast-forward merge.
    merge = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "merge", "--ff-only", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge.returncode != 0:
        combined = ((merge.stdout or "") + (merge.stderr or "")).strip()
        _git_log.warn(f"{pkgbuild_dir.name}: ff-merge failed: {combined}")
        return GitFetchOutcome(
            status="failed", head_before=head_before, head_after=fetch_head,
            error=combined,
        )

    new_head = _rev_parse(pkgbuild_dir, "HEAD") or fetch_head
    _git_log.info(f"  {pkgbuild_dir.name}: {head_before[:10]} → {new_head[:10]}")
    return GitFetchOutcome(
        status="fetched", head_before=head_before, head_after=new_head,
    )


def _rev_parse(pkgbuild_dir: Path, ref: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", ref],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def git_is_dirty(pkgbuild_dir: Path) -> bool:
    """
    Return True if pkgbuild_dir is a git repo with local modifications, defined as:

    1. Uncommitted changes — staged or unstaged modifications to tracked files.
       (Untracked files such as build artifacts are intentionally ignored.)
    2. Unpushed commits — commits that exist locally but not on the tracking branch.
    3. No tracking branch — the repo is entirely local with no upstream to compare
       against, so it is treated as dirty by definition.

    An empty repo with no HEAD (no commits) is treated as clean — it has no
    work to protect, and reporting it as dirty would block recovery from
    aborted-clone leftovers (`.git/` only, no PKGBUILD).

    Returns False if the directory is not a git repo or is clean and fully in sync
    with its tracking branch.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if r.returncode != 0:
        return False

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--verify", "--quiet", "HEAD"],
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


def is_transient_git_error(err: str) -> bool:
    """True for network flakes worth retrying (not rate limits)."""
    return "timed out" in err or any(m in err for m in TRANSIENT_GIT_ERRORS)


def is_rate_limit_error(err: str) -> bool:
    """True when the server is explicitly throttling (429/503/502)."""
    return any(m in err for m in RATE_LIMIT_GIT_ERRORS)


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
