"""
pkgbuild_review.py — interactive review gate for changed PKGBUILD sources.

Before a package is built, the gate compares the source clone's HEAD against
the ``reviewed_commit`` recorded in build_state.toml (the clone HEAD at the
last successful build) and, when they differ, shows what changed and asks for
a decision. The diff covers the **full source tree** — not just the PKGBUILD —
so changes hiding in ``.install`` files, patches, or new sources are visible.

Decision model (returned to the caller, which owns the build loop):
    accept → build proceeds; the new HEAD is persisted as ``reviewed_commit``
             (via BuildOptions → BuildState.record) once the build succeeds.
    skip   → the package is dropped from this run's build list.
    abort  → the caller stops the run before anything is built or installed.
    clean  → HEAD matches the recorded reviewed_commit; nothing to review.
    no_git → the source dir is not a git checkout (local PKGBUILD); the gate
             does not apply.

A package with no recorded ``reviewed_commit`` (first profiled build of the
clone) is reviewed against git's empty tree — a full-content review. The same
fallback applies when the recorded commit no longer exists in the clone
(purge + re-clone rewrote history).

The comparison is commit-based (recorded commit → HEAD), not worktree-based:
upstream changes arrive as commits via source sync, while uncommitted local
edits are user-authored (the STATUS_DIVERGED build-with-local-PKGBUILD case)
and are deliberately not re-presented to their author.

Auto-accept paths (no prompt, decision logged per package):
  * non-interactive runs (stdin or stdout not a TTY) — an unattended
    ``sysforge update`` must not hang on a prompt;
  * callers passing ``interactive=False`` — ``sysforge update`` defaults to
    this so routine batch updates stay unattended; ``--review`` opts back in.
Callers disable the gate entirely via ``--no-review`` /
``[build] review = false``.

Owns the ``[REVIEW]`` log tag.

Public API:
    head_commit(pkgbuild_dir)            -> str | None
    commit_exists(pkgbuild_dir, sha)     -> bool
    review_target(pkgbase, pkgbuild_dir, reviewed_commit,
                  interactive=True)      -> str (DECISION_*)
"""
import subprocess
import sys
from pathlib import Path

from sysforge import log

_log = log.get_logger("REVIEW")

from sysforge.primitives.pager import maybe_pager  # noqa: E402
from sysforge.primitives.prompt import prompt_key  # noqa: E402

# git's well-known empty-tree object id: the diff base for a first review
# (no reviewed_commit recorded), so a brand-new clone gets a full-content
# review rather than silently passing the gate.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DECISION_ACCEPT = "accept"
DECISION_SKIP = "skip"
DECISION_ABORT = "abort"
DECISION_CLEAN = "clean"
DECISION_NO_GIT = "no_git"


def _git(pkgbuild_dir: Path, *args: str) -> str | None:
    """Run git in ``pkgbuild_dir``; return stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(pkgbuild_dir), *args],
            capture_output=True, text=True,
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def head_commit(pkgbuild_dir: Path) -> str | None:
    """HEAD commit sha of the clone, or None when not a git checkout."""
    out = _git(pkgbuild_dir, "rev-parse", "HEAD")
    return out.strip() if out else None


def commit_exists(pkgbuild_dir: Path, sha: str) -> bool:
    """True when ``sha`` resolves to a commit object in the clone."""
    return _git(pkgbuild_dir, "cat-file", "-e", f"{sha}^{{commit}}") is not None


def _short(sha: str) -> str:
    return sha[:7] if sha else "(none)"


def review_target(
    pkgbase: str,
    pkgbuild_dir: Path,
    reviewed_commit: str | None,
    interactive: bool = True,
) -> str:
    """Run the review gate for one package; return a DECISION_* constant.

    ``interactive=False`` auto-accepts source changes with a logged notice
    instead of prompting (the ``sysforge update`` default). Pure with
    respect to build state — the caller persists the accepted HEAD.
    """
    head = head_commit(pkgbuild_dir)
    if head is None:
        return DECISION_NO_GIT
    if reviewed_commit == head:
        return DECISION_CLEAN

    if reviewed_commit and commit_exists(pkgbuild_dir, reviewed_commit):
        base = reviewed_commit
        what = f"source changed since last accepted build ({_short(base)} → {_short(head)})"
    else:
        if reviewed_commit:
            _log.warn(
                f"{pkgbase}: recorded reviewed commit {_short(reviewed_commit)} "
                "no longer exists in the clone (re-clone?) — "
                "falling back to a full-content review"
            )
        base = _EMPTY_TREE
        what = f"first review of this source (full content, HEAD {_short(head)})"

    if not interactive:
        _log.ui(
            f"auto-accepted: {pkgbase} — {what}; "
            "rerun with --review to inspect the diff"
        )
        return DECISION_ACCEPT
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _log.warn(
            f"{pkgbase}: {what} — auto-accepting (non-interactive run); "
            "run interactively or use `sysforge build` to review the diff"
        )
        return DECISION_ACCEPT

    stat = _git(pkgbuild_dir, "diff", "--stat", base, head) or ""
    print(f"\n[REVIEW] {pkgbase}: {what}")
    if stat:
        print(stat.rstrip())

    while True:
        try:
            answer = prompt_key(
                "[REVIEW] [v]iew diff / [a]ccept / [s]kip package / "
                "a[b]ort run? "
            )
        except (EOFError, KeyboardInterrupt):
            # No answer is not consent — fail to the safe side.
            print()
            return DECISION_ABORT
        if answer == "v":
            patch = _git(pkgbuild_dir, "diff", base, head)
            if patch is None:
                _log.warn(f"{pkgbase}: git diff failed — cannot display patch")
                continue
            with maybe_pager(True):
                print(patch)
        elif answer == "a":
            return DECISION_ACCEPT
        elif answer == "s":
            return DECISION_SKIP
        elif answer == "b":
            return DECISION_ABORT
