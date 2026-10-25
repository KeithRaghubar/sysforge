# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pkgbuild_review.py — interactive review gate for changed PKGBUILD sources.

Before a package is built, the gate compares the source clone's HEAD against
its review baseline in build_state.toml (the clone HEAD the user last
accepted, ``BuildState.reviewed_commit``) and, when they differ, shows what
changed and asks for a decision. The diff covers the **full source tree** — not just the PKGBUILD —
so changes hiding in ``.install`` files, patches, or new sources are visible.

Decision model (returned to the caller, which owns the build loop):
    accept → build proceeds; the caller saves the new HEAD as the baseline
             right away (``build_core._save_review_baseline``, 3.5.0-B8), so
             a failed build never re-presents an accepted change.
    skip   → the package is dropped from this run's build list.
    abort  → the caller stops the run before anything is built or installed.
    clean  → HEAD matches the recorded reviewed_commit; nothing to review.
    no_git → the source dir is not a git checkout (local PKGBUILD); the gate
             does not apply.

A package with no review baseline (first review of the clone) is reviewed
against git's empty tree — a full-content review. The same fallback applies
when the saved commit no longer exists in the clone (purge + re-clone
rewrote history).

The comparison is commit-based (recorded commit → HEAD), not worktree-based:
upstream changes arrive as commits via source sync, while uncommitted local
edits are user-authored (the STATUS_DIVERGED build-with-local-PKGBUILD case)
and are deliberately not re-presented to their author.

Gate modes (3.0.0-F3), resolved once per run by :func:`resolve_review_policy`:
  * ``prompt`` (default): ask at a terminal; with no terminal (stdin or stdout
    not a TTY) *refuse* the change. A run that could not ask is never treated
    as consent: the refused package is dropped and the run continues.
  * ``accept``: accept without asking, log it, and list it in the summary.
  * ``off``: the gate does not run (``--no-review`` / ``[build] review = false``).
Sources on the trusted list (``[build] review_trusted_sources``, default
``["repo"]``) are accepted without asking in any active mode.

Owns the ``[REVIEW]`` log tag.

Public API:
    head_commit(pkgbuild_dir)            -> str | None
    commit_exists(pkgbuild_dir, sha)     -> bool
    resolve_review_policy(args, build_cfg) -> ReviewPolicy
    review_target(pkgbase, pkgbuild_dir, reviewed_commit,
                  *, mode, trusted)      -> str (DECISION_*)
    review_deps(deps, *, mode, trusted,
                changed_out)             -> str (accept | abort | refused | clean)
    change_summary(pkgbuild_dir, reviewed_commit) -> str | None
"""
import sys
from dataclasses import dataclass
from pathlib import Path

from sysforge import log

_log = log.get_logger("REVIEW")

from sysforge.primitives.pager import maybe_pager  # noqa: E402
from sysforge.primitives.prompt import is_interactive, no_prompt_reason, prompt_key  # noqa: E402
from sysforge.primitives import render, run

# git's well-known empty-tree object id: the diff base for a first review
# (no reviewed_commit recorded), so a brand-new clone gets a full-content
# review rather than silently passing the gate.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DECISION_ACCEPT = "accept"
DECISION_SKIP = "skip"
DECISION_ABORT = "abort"
DECISION_CLEAN = "clean"
DECISION_NO_GIT = "no_git"
DECISION_REFUSED = "refused"

# Gate modes (3.0.0-F3). ``prompt`` asks at a terminal and *refuses* without
# one: a run that could not ask is never treated as consent. ``accept`` is the
# old unattended behaviour made a named choice: accept, log, list in the summary.
MODE_PROMPT = "prompt"
MODE_ACCEPT = "accept"
MODE_OFF = "off"

VALID_SOURCES = frozenset({"repo", "aur", "git", "local"})
DEFAULT_TRUSTED = frozenset({"repo"})


@dataclass(frozen=True)
class ReviewPolicy:
    """One run's review decision, resolved once by :func:`resolve_review_policy`."""

    mode: str = MODE_PROMPT
    trusted: frozenset[str] = DEFAULT_TRUSTED

    @property
    def active(self) -> bool:
        return self.mode != MODE_OFF

    def trusts(self, source: str | None) -> bool:
        """An unknown source is treated as ``aur``, the same default the sync
        scheduler uses, so it is never trusted by accident."""
        return (source or "aur") in self.trusted

    def describe(self) -> str:
        if self.mode != MODE_PROMPT or not self.trusted:
            return self.mode
        return f"{self.mode} (trusted: {', '.join(sorted(self.trusted))})"


def _coerce_mode(raw) -> str:
    if raw is True or raw is None:
        return MODE_PROMPT
    if raw is False:
        return MODE_OFF
    if isinstance(raw, str) and raw.strip().lower() == MODE_ACCEPT:
        return MODE_ACCEPT
    _log.warn(f'[build] review = {raw!r} is not true, "accept" or false; '
              "reviewing changed sources")
    return MODE_PROMPT


def _coerce_trusted(raw) -> frozenset[str]:
    if raw is None:
        return DEFAULT_TRUSTED
    if not isinstance(raw, list):
        _log.warn('[build] review_trusted_sources must be a list; using ["repo"]')
        return DEFAULT_TRUSTED
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item in VALID_SOURCES:
            out.add(item)
        else:
            _log.warn(f"[build] review_trusted_sources: ignoring {item!r} "
                      f"(valid: {', '.join(sorted(VALID_SOURCES))})")
    return frozenset(out)


def resolve_review_policy(args, build_cfg: dict | None) -> ReviewPolicy:
    """The one resolver behind ``update`` and ``build``.

    Precedence: ``--no-review`` (off) > ``--review`` (prompt, nothing trusted)
    > ``[build] review`` (true | "accept" | false) in packages.toml.
    """
    cfg = build_cfg or {}
    trusted = _coerce_trusted(cfg.get("review_trusted_sources"))
    if getattr(args, "no_review", False):
        return ReviewPolicy(MODE_OFF, trusted)
    if getattr(args, "review", False):
        return ReviewPolicy(MODE_PROMPT, frozenset())
    return ReviewPolicy(_coerce_mode(cfg.get("review", True)), trusted)


def _no_terminal() -> bool:
    return not (is_interactive() and sys.stdout.isatty())


def _git(pkgbuild_dir: Path, *args: str) -> str | None:
    """Run git in ``pkgbuild_dir``; return stdout, or None on any failure."""
    try:
        proc = run.probe(["git", "-C", str(pkgbuild_dir), *args])
    except OSError:
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


def _color_flag() -> str:
    """git ``--color`` argument matching the active colour gate.

    The diff is captured through a pipe (so git would default to plain) and then
    paged through ``less -R`` (which passes ANSI through), so colour is decided
    here up front: ``always`` when :func:`log.use_color` is on, ``never`` so
    ``--color=never`` / NO_COLOR / non-TTY runs stay plain.
    """
    return "--color=always" if log.use_color() else "--color=never"


def _tag() -> str:
    return log.cyan("[REVIEW]")


def review_target(
    pkgbase: str,
    pkgbuild_dir: Path,
    reviewed_commit: str | None,
    *,
    mode: str = MODE_PROMPT,
    trusted: bool = False,
) -> str:
    """Run the review gate for one package; return a DECISION_* constant.

    ``trusted`` (the source is on the trusted list) and ``mode="accept"``
    accept without asking; ``prompt`` with no terminal refuses. Pure with
    respect to build state — the caller persists the accepted HEAD.
    """
    head = head_commit(pkgbuild_dir)
    if head is None:
        return DECISION_NO_GIT
    if reviewed_commit == head:
        return DECISION_CLEAN

    if reviewed_commit and commit_exists(pkgbuild_dir, reviewed_commit):
        base = reviewed_commit
        what = f"source changed since last accepted review ({_what(base, head)})"
    else:
        if reviewed_commit:
            _log.warn(
                f"{pkgbase}: recorded reviewed commit {_short(reviewed_commit)} "
                "no longer exists in the clone (re-clone?); "
                "falling back to a full-content review"
            )
        base = _EMPTY_TREE
        what = f"first review of this source (full content, HEAD {_short(head)})"

    if trusted:
        _log.info(f"{pkgbase}: {what}; accepted (trusted source)")
        return DECISION_ACCEPT
    if mode == MODE_ACCEPT:
        _log.info(f"{pkgbase}: {what}; accepted without review")
        return DECISION_ACCEPT
    if _no_terminal():
        _log.warn(f"{pkgbase}: {what}; refused ({no_prompt_reason()})")
        return DECISION_REFUSED

    stat = _git(pkgbuild_dir, "diff", _color_flag(), "--stat", base, head) or ""
    print(f"\n{_tag()} {pkgbase}: {what}")
    if stat:
        print(stat.rstrip())

    while True:
        try:
            answer = prompt_key(
                f"{_tag()} [v]iew diff / [a]ccept / [s]kip package / "
                "a[b]ort run? "
            )
        except (EOFError, KeyboardInterrupt):
            # No answer is not consent — fail to the safe side.
            print()
            return DECISION_ABORT
        if answer == "v":
            patch = _git(pkgbuild_dir, "diff", _color_flag(), base, head)
            if patch is None:
                _log.warn(f"{pkgbase}: git diff failed; cannot display patch")
                continue
            with maybe_pager(True):
                print(f"{_tag()} {pkgbase}: {what}\n{patch}")
        elif answer == "a":
            return DECISION_ACCEPT
        elif answer == "s":
            return DECISION_SKIP
        elif answer == "b":
            return DECISION_ABORT


def _changed_since_review(
    pkgbuild_dir: Path, reviewed_commit: str | None,
) -> tuple[str, str] | None:
    """Return ``(base, head)`` when the clone changed since review, else None.

    None covers both the clean case and non-git dirs — neither needs review.
    A missing reviewed_commit (or one rewritten away by a re-clone) falls back
    to the empty tree, same as :func:`review_target`.
    """
    head = head_commit(pkgbuild_dir)
    if head is None or reviewed_commit == head:
        return None
    if reviewed_commit and commit_exists(pkgbuild_dir, reviewed_commit):
        return reviewed_commit, head
    return _EMPTY_TREE, head


def change_summary(pkgbuild_dir, reviewed_commit: str | None) -> str | None:
    """One-line reason a source needs review, or None when it does not
    (clean, or not a git checkout). Used for refusal reasons and the plan."""
    pair = _changed_since_review(Path(pkgbuild_dir), reviewed_commit)
    if pair is None:
        return None
    base, head = pair
    if base == _EMPTY_TREE:
        return f"unreviewed source (first review, HEAD {_short(head)})"
    return f"unreviewed source change ({_short(base)} {render.arrow()} {_short(head)})"


def _what(base: str, head: str) -> str:
    """One dep's change, as the batched summary and its pager header show it."""
    if base == _EMPTY_TREE:
        return f"first review (full content, HEAD {_short(head)})"
    return f"{_short(base)} {render.arrow()} {_short(head)}"


def review_deps(
    deps: list[tuple[str, Path, str | None]],
    *,
    mode: str = MODE_PROMPT,
    trusted: bool = False,
    changed_out: list[str] | None = None,
) -> str:
    """Batched review gate for AUR dependency PKGBUILDs.

    ``deps`` is ``[(name, pkgbuild_dir, reviewed_commit), ...]`` — the AUR
    dependencies about to be built by the dep arm. Unlike the per-target gate
    there is no skip option: dropping a dependency breaks the package that
    needs it, so the decision is all-or-nothing — DECISION_ACCEPT or
    DECISION_ABORT (DECISION_CLEAN when nothing changed).

    Non-prompt paths mirror :func:`review_target`: trusted or ``accept`` mode
    accepts with one batched notice; ``prompt`` with no terminal returns
    DECISION_REFUSED. ``changed_out`` collects the changed dep names so the
    caller can drop exactly the targets that need them.
    """
    changed: list[tuple[str, Path, str, str]] = []
    for name, pkgbuild_dir, reviewed_commit in deps:
        pair = _changed_since_review(pkgbuild_dir, reviewed_commit)
        if pair is not None:
            changed.append((name, pkgbuild_dir, pair[0], pair[1]))
    if not changed:
        return DECISION_CLEAN

    names = ", ".join(c[0] for c in changed)
    if changed_out is not None:
        changed_out.extend(c[0] for c in changed)
    if trusted:
        _log.info(f"{len(changed)} dependency source change(s) accepted "
                  f"(trusted source): {names}")
        return DECISION_ACCEPT
    if mode == MODE_ACCEPT:
        _log.info(f"{len(changed)} dependency source change(s) accepted "
                  f"without review: {names}")
        return DECISION_ACCEPT
    if _no_terminal():
        _log.warn(f"{len(changed)} dependency source change(s) refused "
                  f"({no_prompt_reason()}): {names}")
        return DECISION_REFUSED

    print(f"\n{_tag()} {len(changed)} dependency source change(s):")
    for name, pkgbuild_dir, base, head in changed:
        what = _what(base, head)
        stat = _git(pkgbuild_dir, "diff", _color_flag(), "--shortstat", base, head) or ""
        print(f"  {name}: {what}{'  —' + stat.rstrip() if stat.strip() else ''}")

    while True:
        try:
            answer = prompt_key(
                f"{_tag()} [v]iew diffs / [a]ccept all / a[b]ort run? "
            )
        except (EOFError, KeyboardInterrupt):
            # No answer is not consent — fail to the safe side.
            print()
            return DECISION_ABORT
        if answer == "v":
            # One pager per dep, each opening on a header naming the dep and
            # its position, so consecutive patches can't run together.
            for i, (name, pkgbuild_dir, base, head) in enumerate(changed, 1):
                patch = _git(pkgbuild_dir, "diff", _color_flag(), base, head)
                if patch is None:
                    _log.warn(f"{name}: git diff failed; cannot display patch")
                    continue
                with maybe_pager(True):
                    print(f"{_tag()} {name} ({i}/{len(changed)}): "
                          f"{_what(base, head)}\n{patch}")
        elif answer == "a":
            return DECISION_ACCEPT
        elif answer == "b":
            return DECISION_ABORT
