# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
git_ops.py — local git inspection, non-destructive fetch, and safe source purge

This is the leaf of the former ``aur.py``: pure git plumbing with no AUR-RPC or
network-clone concerns. Everything here operates on an on-disk packaging repo
(``pkgbuild_dir``) and never mutates remote state.

Public API:
    git_fetch_and_compare(dir, ...)   -> GitFetchOutcome   shallow fetch + HEAD
                                         compare (no destructive ops)
    classify_head_vs_upstream(dir)    -> (state, n_local, n_upstream)
    git_is_dirty(pkgbuild_dir)        -> bool              True if git repo has
                                         uncommitted/unpushed work
    purge_src(pkgbuild_dir)           -> None              rm -rf pkgbuild_dir;
                                         refuses if local work would be lost

Also exports the ``GitFetchOutcome`` result dataclass plus the
``TRANSIENT_GIT_ERRORS`` / ``RATE_LIMIT_GIT_ERRORS`` string tuples and their
``is_transient_git_error()`` / ``is_rate_limit_error()`` classifiers, used by
callers that need to distinguish network flakes from AUR throttling.

``aur.py`` re-exports this module's public surface so existing
``from sysforge.primitives.aur import git_is_dirty`` call sites and tests are
unchanged.
"""
import re as _re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.net_policy import KIND_SOURCE_FETCH, get_policy

_log = log.get_logger("GIT")


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
    is_vcs: bool = False,
    pkgbase: str | None = None,
) -> GitFetchOutcome:
    """Shallow-fetch upstream and report the outcome without destructive ops.

    Replaces the old `git pull --rebase` flow. Never runs `rebase --abort`,
    `reset --hard`, or `purge_src`; divergence is surfaced via the
    ``diverged`` status so the caller (typically the sync scheduler) can
    decide whether to skip the package or trigger an opt-in recovery.

    When ``limiter`` is provided, the git invocation goes through
    ``rate_limit.run_throttled_git`` so the Retry-After window is enforced
    across RPC and git paths.

    ``is_vcs`` marks a VCS packaging repo (``-git``/``-svn``/``-hg``/``-bzr``)
    and affects **only the operator-facing message** on a dirty tree: when the
    working-tree diff is nothing but makepkg's ``pkgver()`` auto-bump, saying
    "local modifications" is a false positive. It deliberately does NOT relax
    the fast-forward gate — see the comment at the dirty branch below.
    """
    # Source freeze (3.0.0-F2): refuse before the repo probe. The scheduler is
    # the only caller that converts this into a status; other callers see the
    # raise. ``pkgbase`` is the authoritative --thaw name; callers that don't
    # track pkgbase separately (e.g. llvm_state) fall back to the checkout
    # dir name, which is only correct when it matches the pkgbase — the
    # scheduler always passes the real pkgbase explicitly to avoid that trap.
    get_policy().check(KIND_SOURCE_FETCH, pkgbase or Path(pkgbuild_dir).name)

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
        _log.info(f"{pkgbuild_dir.name}: no tracking branch — skipping fetch")
        return GitFetchOutcome(status="no_tracking", head_before=None, head_after=None)

    tracking = r.stdout.strip()
    # tracking looks like "origin/master" — split once, right-hand side is the branch.
    if "/" in tracking:
        remote, _, branch = tracking.partition("/")
    else:
        remote, branch = "origin", tracking

    head_before = _rev_parse(pkgbuild_dir, "HEAD")

    _log.info(f"Fetching {pkgbuild_dir.name} from {tracking}")

    # 3. Full-history fetch.
    #
    # A ``--depth=1`` fetch grafts the fetched tip as a parent-less root and
    # marks the whole repo shallow, which makes the ``merge-base
    # --is-ancestor`` fast-forward check below — and the ``rev-list`` counts
    # in ``classify_head_vs_upstream`` — see no shared history, so every
    # routine upstream advance falsely reports ``diverged``. Packaging repos
    # carry only PKGBUILD/metadata, so a full fetch is cheap; ``--unshallow``
    # also self-heals any repo previously shallowed by the old ``--depth=1``
    # path.
    shallow = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True,
    ).stdout.strip() == "true"
    fetch_cmd = ["git", "-C", str(pkgbuild_dir), "fetch"]
    if shallow:
        fetch_cmd.append("--unshallow")
    fetch_cmd += [remote, branch]
    try:
        r = _run(fetch_cmd)
    except subprocess.TimeoutExpired:
        err = f"git fetch timed out after {timeout}s"
        _log.warn(f"{pkgbuild_dir.name}: {err}")
        return GitFetchOutcome(
            status="failed", head_before=head_before, head_after=None, error=err,
        )

    if r.returncode != 0:
        combined = ((r.stdout or "") + (r.stderr or "")).strip()
        status = "rate_limited" if is_rate_limit_error(combined) else "failed"
        _log.warn(
            f"{pkgbuild_dir.name}: git fetch failed: "
            f"{combined.splitlines()[0] if combined else 'no output'}"
        )
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
        if ancestor.returncode != 0:
            err = f"divergent: HEAD {head_before[:10]} vs FETCH_HEAD {fetch_head[:10]}"
            _log.warn(f"{pkgbuild_dir.name}: {err} — keeping local PKGBUILD")
        elif is_vcs and not git_is_dirty(pkgbuild_dir, is_vcs=True):
            # 2.6.1-B1: the only working-tree change is makepkg's pkgver()
            # auto-bump (plus the regenerated .SRCINFO) — a build artifact, not
            # operator work. Warning that the operator has "local
            # modifications" is a false positive, and it contradicts the
            # caller: SourceSync re-asks this question VCS-aware, gets clean,
            # and hard-resets to FETCH_HEAD.
            #
            # The ff-merge is still skipped. Relaxing the gate above would let
            # `git merge --ff-only` run against a dirty tree, which aborts
            # ("local changes would be overwritten") whenever the upstream
            # commit also touched PKGBUILD — the common AUR case — returning
            # `failed`, a scheduler blocker. `diverged` is the outcome the
            # caller's reset already heals correctly.
            err = "working tree carries only VCS pkgver churn"
            _log.info(f"{pkgbuild_dir.name}: {err} — deferring to caller reset")
        else:
            err = "working tree has local modifications"
            _log.warn(f"{pkgbuild_dir.name}: {err} — keeping local PKGBUILD")
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
        _log.warn(f"{pkgbuild_dir.name}: ff-merge failed: {combined}")
        return GitFetchOutcome(
            status="failed", head_before=head_before, head_after=fetch_head,
            error=combined,
        )

    new_head = _rev_parse(pkgbuild_dir, "HEAD") or fetch_head
    _log.info(f"  {pkgbuild_dir.name}: {head_before[:10]} → {new_head[:10]}")
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


def _local_user_email(pkgbuild_dir: Path) -> str | None:
    """Return ``git config user.email`` for ``pkgbuild_dir`` (repo or global).

    Empty string / missing config → None. Whitespace is stripped.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "config", "--get", "user.email"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def classify_head_vs_upstream(
    pkgbuild_dir: Path,
) -> tuple[str, int, int]:
    """Classify HEAD relative to its upstream tracking branch.

    Returns ``(state, n_local, n_upstream)`` where ``state`` is one of:

    * ``"not_a_repo"``        — directory has no ``.git``.
    * ``"no_head"``            — git repo exists but ``HEAD`` is unresolvable
      (empty clone, ``.git/`` only). Trivially safe to purge.
    * ``"no_tracking"``        — ``@{u}`` does not resolve. Treated as a
      local-only repo by ``git_is_dirty``.
    * ``"clean"``              — HEAD == upstream.
    * ``"behind"``             — HEAD is a strict ancestor of upstream
      (local repo just out of date).
    * ``"ahead"``              — upstream is a strict ancestor of HEAD
      (operator has unpushed commits).
    * ``"diverged_user"``      — HEAD and upstream share a common ancestor
      but neither is descendant of the other, **and** at least one of the
      ``@{u}..HEAD`` commits is authored by ``git config user.email``.
    * ``"diverged_upstream"``  — same divergent shape, but no commits in
      ``@{u}..HEAD`` are authored by the local user. Most likely the
      upstream rewrote history (force-push); the local clone is logically
      in-sync, just out of date. Treated as not-dirty.

    ``n_local`` / ``n_upstream`` are commit counts for ``@{u}..HEAD`` and
    ``HEAD..@{u}`` respectively (both 0 for ``clean``; ``n_local`` is the
    "ahead" count for ``ahead``).
    """
    is_repo = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    ).returncode == 0
    if not is_repo:
        return "not_a_repo", 0, 0

    has_head = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True,
    ).returncode == 0
    if not has_head:
        return "no_head", 0, 0

    upstream = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse",
         "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if upstream.returncode != 0:
        return "no_tracking", 0, 0

    def _count(spec: str) -> int:
        r = subprocess.run(
            ["git", "-C", str(pkgbuild_dir), "rev-list", "--count", spec],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return 0
        n = r.stdout.strip()
        return int(n) if n.isdigit() else 0

    n_local = _count("@{u}..HEAD")
    n_upstream = _count("HEAD..@{u}")

    if n_local == 0 and n_upstream == 0:
        return "clean", 0, 0
    if n_local == 0:
        return "behind", 0, n_upstream
    if n_upstream == 0:
        return "ahead", n_local, 0

    # Divergent histories — split on authorship.
    local_email = _local_user_email(pkgbuild_dir)
    if not local_email:
        # No local identity to attribute commits to: be conservative and
        # treat any divergent commit as the operator's work.
        return "diverged_user", n_local, n_upstream

    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "log",
         "--format=%ae", "@{u}..HEAD"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return "diverged_user", n_local, n_upstream
    authors = {line.strip().lower() for line in r.stdout.splitlines() if line.strip()}
    if local_email.lower() in authors:
        return "diverged_user", n_local, n_upstream
    return "diverged_upstream", n_local, n_upstream


# A VCS packaging repo's PKGBUILD is routinely rewritten by makepkg's
# ``pkgver()`` flow (updates ``pkgver=`` and may reset ``pkgrel=1``). That
# auto-bump is not operator work and must not block ``--cleansrc`` for
# ``-git``/``-svn``/``-hg``/``-bzr`` packages. ``.SRCINFO`` is handled
# separately (treated as a generated artifact — see ``_uncommitted_dirty_paths``).
_PKGVER_LINE_RE = _re.compile(r"^[+-]\s*(pkgver|pkgrel)\s*=\s*\S.*$")


def _diff_is_pkgver_only(pkgbuild_dir: Path, path: str) -> bool:
    """Return True iff ``git diff -U0`` for ``path`` only touches pkgver/pkgrel lines.

    Used by ``git_is_dirty(..., is_vcs=True)`` to ignore makepkg's pkgver()
    auto-bump pattern in the PKGBUILD of a VCS packaging repo. Fail-safe: any
    git error or unexpected diff line makes this return False so the
    surrounding dirty check keeps protecting the operator's work.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir),
         "diff", "-U0", "--no-color", "--", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    saw_content = False
    for line in r.stdout.splitlines():
        if not line:
            continue
        # Skip diff metadata: file headers and hunk markers.
        if line.startswith(("diff ", "index ", "--- ", "+++ ", "@@ ")):
            continue
        if line.startswith(("+", "-")):
            if not _PKGVER_LINE_RE.match(line):
                return False
            saw_content = True
    return saw_content


def _uncommitted_dirty_paths(pkgbuild_dir: Path, *, is_vcs: bool) -> list[str]:
    """Return the list of modified-tracked paths that count as dirty.

    With ``is_vcs=True`` the helper ignores two classes of makepkg-generated
    churn on VCS packaging repos: a PKGBUILD whose diff is restricted to
    pkgver=/pkgrel= lines (the pkgver() auto-bump), and any change to the
    generated ``.SRCINFO``. Returns an empty list on git error.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "status",
         "--short", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    paths: list[str] = []
    for line in r.stdout.splitlines():
        # Porcelain v1 short format: ``XY <path>`` where XY is two status
        # chars, then a single space, then the path (rename targets use
        # ``-> new`` but ``--short`` keeps both names on one line).
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if not path:
            continue
        # Strip the rename-source half if present (``old -> new``); the
        # destination is what makepkg / the operator actually wrote.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if is_vcs and path == ".SRCINFO":
            # .SRCINFO is a generated artifact (makepkg --printsrcinfo). For
            # VCS packages makepkg's pkgver() rewrites not just pkgver/pkgrel
            # but every version-pinned depends/provides line, so a line-level
            # filter can't distinguish a mechanical bump from a real edit.
            # Operators don't meaningfully hand-edit .SRCINFO without also
            # editing PKGBUILD (caught below), so treat any .SRCINFO change as
            # not operator work.
            continue
        if is_vcs and path == "PKGBUILD" \
                and _diff_is_pkgver_only(pkgbuild_dir, path):
            continue
        paths.append(path)
    return paths


def git_is_dirty(pkgbuild_dir: Path, *, is_vcs: bool = False) -> bool:
    """
    Return True if pkgbuild_dir is a git repo with local modifications, defined as:

    1. Uncommitted changes — staged or unstaged modifications to tracked files.
       (Untracked files such as build artifacts are intentionally ignored.)
    2. Ahead-of-upstream commits — commits on HEAD not on the tracking branch
       (true unpushed work).
    3. ``diverged_user`` — divergent histories where the local user authored
       at least one of the ``@{u}..HEAD`` commits.
    4. No tracking branch — the repo is entirely local with no upstream to
       compare against, so it is treated as dirty by definition.

    Empty repos (no HEAD), clean trees, ``behind`` (out of date but no local
    work), and ``diverged_upstream`` (force-pushed upstream, no local commits
    authored by the user) are all reported as not dirty.

    Pass ``is_vcs=True`` for VCS packaging repos (``-git``/``-svn``/``-hg``/
    ``-bzr``) where makepkg's ``pkgver()`` flow auto-rewrites the working-tree
    ``PKGBUILD`` (updating ``pkgver=`` and possibly resetting ``pkgrel=1``)
    and regenerates ``.SRCINFO``. The PKGBUILD pkgver/pkgrel auto-bump and any
    ``.SRCINFO`` change (a generated artifact) are filtered out of the
    uncommitted-tracked check; deliberate edits to other PKGBUILD lines /
    other files still count. The head-vs-upstream classification is unchanged.

    Returns False if the directory is not a git repo or is clean and fully in sync
    with its tracking branch.
    """
    state, _, _ = classify_head_vs_upstream(pkgbuild_dir)
    if state in ("not_a_repo", "no_head"):
        return False

    # Uncommitted-tracked check is independent of the head-vs-upstream
    # classification — a clean classification can still co-exist with
    # uncommitted edits to PKGBUILD.
    if _uncommitted_dirty_paths(pkgbuild_dir, is_vcs=is_vcs):
        return True

    return state in ("no_tracking", "ahead", "diverged_user")


def head_reachable_from_remote(pkgbuild_dir: Path) -> bool:
    """True when HEAD's commit is an ancestor of (or equal to) any remote ref.

    A source=repo checkout pinned to a release tag sits on a detached HEAD
    with no tracking branch; that is upstream's history, not local-only work,
    and must not block a purge. Local-only commits (not on any remote ref)
    still refuse.
    """
    r = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "branch", "-r", "--contains", "HEAD"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _purge_refusal_message(pkgbuild_dir: Path, *, is_vcs: bool) -> str | None:
    """Return the operator-facing reason for refusing to purge, or None.

    Concrete causes (named in the message):
      * uncommitted tracked changes — lists the paths so the operator knows
        which file is protecting the repo.
      * ahead-of-upstream commits.
      * diverged_user (force-pushed upstream **with** local-user commits).
      * no upstream tracking branch.
    """
    state, n_local, _ = classify_head_vs_upstream(pkgbuild_dir)
    if state in ("not_a_repo", "no_head"):
        return None

    dirty_paths = _uncommitted_dirty_paths(pkgbuild_dir, is_vcs=is_vcs)
    causes: list[str] = []
    if dirty_paths:
        causes.append(
            "uncommitted tracked changes ("
            + ", ".join(sorted(dirty_paths)) + ")"
        )
    if state == "ahead":
        causes.append(f"{n_local} unpushed commit(s) ahead of upstream")
    elif state == "diverged_user":
        causes.append("diverged history with local-user-authored commits")
    elif state == "no_tracking" and not head_reachable_from_remote(pkgbuild_dir):
        causes.append("no upstream tracking branch")

    if not causes:
        return None
    return (
        f"refusing to purge {pkgbuild_dir}: " + "; ".join(causes)
        + " — commit/discard the change or pass --cleansrc-force"
    )


def purge_src(
    pkgbuild_dir: Path, *, force: bool = False, is_vcs: bool = False,
) -> None:
    """
    Remove pkgbuild_dir to allow a fresh clone on the next build.

    Refuses (raises RuntimeError) if the directory is a git repo with local
    work that would be destroyed: uncommitted tracked changes, unpushed
    commits, diverged history with local-user commits, or no upstream
    tracking branch (entirely-local repo). The error message names the
    actual cause so the operator can fix it precisely.

    Pass ``is_vcs=True`` for ``-git``/``-svn``/``-hg``/``-bzr`` packaging
    repos so makepkg's ``pkgver()`` auto-bump of PKGBUILD and its regenerated
    ``.SRCINFO`` do not falsely block the purge. See ``git_is_dirty`` for the
    filter rule.

    Pass ``force=True`` to bypass the dirty-tree guard and purge unconditionally
    — the caller has already decided the local work is not worth preserving
    (e.g. ``--cleansrc-force`` after upstream rewrote history).

    A non-existent directory is a no-op. A non-git directory is purged
    unconditionally — it has no commit history to protect.
    """
    if not pkgbuild_dir.exists():
        return

    is_git = subprocess.run(
        ["git", "-C", str(pkgbuild_dir), "rev-parse", "--git-dir"],
        capture_output=True,
    ).returncode == 0

    if is_git and not force:
        reason = _purge_refusal_message(pkgbuild_dir, is_vcs=is_vcs)
        if reason is not None:
            raise RuntimeError(reason)

    suffix = " (forced)" if force else ""
    _log.warn(f"purging {pkgbuild_dir} for clean re-clone{suffix}")
    shutil.rmtree(pkgbuild_dir)


def purge_srcdest(pkgbase, srcdest_dir, *, pkgbuild_dir=None) -> int:
    """Delete ``pkgbase``'s cached source tarballs from SRCDEST.

    Companion to :func:`purge_src` for ``--cleansrc``: the checkout rmtree
    doesn't touch makepkg's SRCDEST cache, so a purged-and-re-cloned package
    could still rebuild from a stale cached tarball (1.2.0-B14). Tarballs are
    cache — no local work to protect — so there is no dirty-tree guard and no
    force flag; failures warn and never raise (best-effort hygiene).

    Matches ``<pkgbase>-<something-starting-with-a-digit>`` archives only
    (makepkg names source tarballs ``name-version.ext``), so a different
    pkgbase sharing a dash-prefix (``foo`` vs ``foo-tools``) is not clobbered.

    No-ops: ``srcdest_dir`` is None/missing, or resolves inside
    ``pkgbuild_dir`` (makepkg default layout — the checkout purge covers it).
    Returns the number of entries removed.
    """
    if srcdest_dir is None:
        return 0
    srcdest_dir = Path(srcdest_dir)
    if not srcdest_dir.is_dir():
        return 0
    if pkgbuild_dir is not None:
        try:
            srcdest_dir.resolve().relative_to(Path(pkgbuild_dir).resolve())
            return 0  # srcdest inside the checkout — rmtree covers it
        except ValueError:
            pass

    removed = 0
    for entry in srcdest_dir.glob(f"{pkgbase}-[0-9]*"):
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as e:
            _log.warn(f"purge_srcdest: could not remove {entry}: {e}")
    if removed:
        _log.warn(f"purged {removed} cached source artifact(s) for {pkgbase} from {srcdest_dir}")
    return removed


TRANSIENT_GIT_ERRORS = (
    "Connection reset",
    "Recv failure",
    "Could not resolve host",
    "TLS connection",
    "early EOF",
    "unexpected eof",  # OpenSSL 3.x: peer closed without close_notify
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
