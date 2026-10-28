"""
test_release_resume.py — tools/release.sh --resume mode (2.0.0-B1).

A failure *after* the release tag exists (e.g. chroot validation) needs fix
commits on top of the release commit; the exact-HEAD auto-resume then never
fires and a re-run computes a fresh bump from the already-bumped version.
`--resume` re-enters Phase 3 for the current version when the v$CUR tag is an
ancestor of a clean HEAD on the release/v$CUR branch (4.0.0-DEV1: the main
ruleset requires a PR, so a release is cut and finished on its own branch).

The script is exercised for real in a scaffold git repo (it cd's to its own
tools/.. root, so it is copied in); every run answers "n" at the approval
prompt, so only Phase 0a/0b logic executes.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "tools" / "release.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def scaffold(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    shutil.copy(RELEASE_SH, repo / "tools" / "release.sh")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "scaffold"\nversion = "0.1.0"\n'
    )
    # Minimal fresh-mode preflight surface: PKGBUILD without the placeholder
    # sentinel, an authored release-notes accumulator, and no-op check targets.
    (repo / "PKGBUILD").write_text(
        "pkgver=0.1.0\nvalidpgpkeys=('0000000000000000000000000000000000000000')\n"
    )
    (repo / "docs" / "release-notes").mkdir(parents=True)
    (repo / "docs" / "release-notes" / "unreleased.md").write_text(
        "# scaffold (unreleased)\n\n## Fixed\n\n- something\n"
    )
    (repo / "Makefile").write_text(
        ".PHONY: lint typecheck check-shipped check-personal check-design "
        "check-roadmap-table check-standards check-surface check-bump check-standards-at man\n"
        "lint typecheck check-shipped check-personal check-design check-roadmap-table "
        "check-standards check-surface check-bump check-standards-at man:\n"
        "\t@true\n"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "tag.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release: v0.1.0")
    return repo


def _run(repo: Path, *args: str, answer: str = "n") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "tools/release.sh", *args, "--dry-run"],
        cwd=repo, input=f"{answer}\n", capture_output=True, text=True,
    )


def _tag_with_sha256sums(repo: Path) -> None:
    (repo / "PKGBUILD").write_text(
        "pkgver=0.1.0\nsha256sums=('SKIP')\n"
        "validpgpkeys=('0000000000000000000000000000000000000000')\n"
    )
    _git(repo, "commit", "-q", "-am", "pkgbuild")
    _git(repo, "tag", "v0.1.0")
    _git(repo, "switch", "-q", "-c", "release/v0.1.0")


def test_resume_accepts_tag_ancestor_of_clean_head(scaffold):
    _git(scaffold, "tag", "v0.1.0")
    _git(scaffold, "switch", "-q", "-c", "release/v0.1.0")
    (scaffold / "fix.txt").write_text("post-tag fix\n")
    _git(scaffold, "add", "fix.txt")
    _git(scaffold, "commit", "-q", "-m", "fix: post-tag chroot failure")
    res = _run(scaffold, "--resume")
    assert res.returncode == 0, res.stderr
    assert "resuming v0.1.0" in res.stdout


def test_resume_still_works_with_tag_exactly_at_head(scaffold):
    _git(scaffold, "tag", "v0.1.0")
    _git(scaffold, "switch", "-q", "-c", "release/v0.1.0")
    res = _run(scaffold, "--resume")
    assert res.returncode == 0, res.stderr
    assert "resuming v0.1.0" in res.stdout


def test_resume_errors_when_tag_missing(scaffold):
    res = _run(scaffold, "--resume")
    assert res.returncode != 0
    assert "v0.1.0" in res.stderr


def test_resume_errors_when_tag_not_ancestor(scaffold):
    # Tag on a divergent branch: HEAD does not contain the release commit.
    _git(scaffold, "checkout", "-q", "-b", "side")
    (scaffold / "side.txt").write_text("side\n")
    _git(scaffold, "add", "side.txt")
    _git(scaffold, "commit", "-q", "-m", "side")
    _git(scaffold, "tag", "v0.1.0")
    _git(scaffold, "checkout", "-q", "main")
    _git(scaffold, "switch", "-q", "-c", "release/v0.1.0")
    (scaffold / "other.txt").write_text("other\n")
    _git(scaffold, "add", "other.txt")
    _git(scaffold, "commit", "-q", "-m", "other")
    res = _run(scaffold, "--resume")
    assert res.returncode != 0
    assert "ancestor" in res.stderr


def test_resume_rejects_bump(scaffold):
    _git(scaffold, "tag", "v0.1.0")
    res = _run(scaffold, "--resume", "--bump=patch")
    assert res.returncode != 0


def test_fresh_release_does_not_auto_resume_from_old_tag(scaffold):
    # A *completed* release also leaves its tag as an ancestor of HEAD —
    # a later plain --bump run must stay a fresh bump, never auto-resume.
    _git(scaffold, "tag", "v0.1.0")
    (scaffold / "feature.txt").write_text("next cycle\n")
    _git(scaffold, "add", "feature.txt")
    _git(scaffold, "commit", "-q", "-m", "feat: next cycle")
    res = _run(scaffold, "--bump=patch")
    assert res.returncode == 0, res.stderr
    assert "0.1.0 -> 0.1.1" in res.stdout
    assert "resuming" not in res.stdout


def test_resume_refuses_off_the_release_branch(scaffold):
    # The sha256 commit Phase 3 makes must land on the release branch, which
    # reaches main through the release PR; main itself only accepts PRs.
    _git(scaffold, "tag", "v0.1.0")
    res = _run(scaffold, "--resume")
    assert res.returncode != 0
    assert "not on release/v0.1.0" in res.stderr


def test_fresh_release_cuts_a_release_branch_and_never_pushes_main(scaffold):
    res = _run(scaffold, "--bump=patch")
    assert res.returncode == 0, res.stderr
    assert "git switch -c release/v0.1.1" in res.stdout
    assert "git push -u origin release/v0.1.1 && git push origin v0.1.1" in res.stdout
    assert "push origin main" not in res.stdout


def test_fresh_release_refuses_an_existing_release_branch(scaffold):
    _git(scaffold, "branch", "release/v0.1.1")
    res = _run(scaffold, "--bump=patch")
    assert res.returncode != 0
    assert "release/v0.1.1 already exists" in res.stderr


def test_resume_tolerates_the_sha256sums_rewrite_of_a_failed_phase3(scaffold):
    # 4.0.0-DEV2: Phase 3 rewrites sha256sums before publishing the release, so
    # a failure there left PKGBUILD modified and the resume refused to start.
    _tag_with_sha256sums(scaffold)
    (scaffold / "PKGBUILD").write_text(
        "pkgver=0.1.0\nsha256sums=('" + "a" * 64 + "'\n            'SKIP')\n"
        "validpgpkeys=('0000000000000000000000000000000000000000')\n"
    )
    res = _run(scaffold, "--resume")
    assert res.returncode == 0, res.stderr
    assert "resuming v0.1.0" in res.stdout


def test_resume_still_refuses_any_other_pkgbuild_edit(scaffold):
    _tag_with_sha256sums(scaffold)
    (scaffold / "PKGBUILD").write_text(
        "pkgver=0.1.1\nsha256sums=('SKIP')\n"
        "validpgpkeys=('0000000000000000000000000000000000000000')\n"
    )
    res = _run(scaffold, "--resume")
    assert res.returncode != 0
    assert "beyond PKGBUILD sha256sums" in res.stderr


def test_oversize_notes_publish_their_summary_and_a_link(scaffold):
    # 4.0.0-DEV2: GitHub rejects a release body over 125000 characters.
    notes = scaffold / "docs" / "release-notes" / "v0.1.0.md"
    notes.write_text(
        "# scaffold v0.1.0\n\nThe summary.\n\n---\n\n## Fixed\n\n"
        + "- an entry\n" * 20000
    )
    _git(scaffold, "add", "-A")
    _git(scaffold, "commit", "-q", "-m", "release: v0.1.0")
    _git(scaffold, "tag", "v0.1.0")
    _git(scaffold, "switch", "-q", "-c", "release/v0.1.0")
    res = _run(scaffold, "--resume", answer="y")
    assert res.returncode == 0, res.stderr
    assert "exceeds GitHub's 125000-character limit" in res.stdout


def test_short_notes_publish_whole(scaffold):
    notes = scaffold / "docs" / "release-notes" / "v0.1.0.md"
    notes.write_text("# scaffold v0.1.0\n\nThe summary.\n\n## Fixed\n\n- an entry\n")
    _git(scaffold, "add", "-A")
    _git(scaffold, "commit", "-q", "-m", "release: v0.1.0")
    _git(scaffold, "tag", "v0.1.0")
    _git(scaffold, "switch", "-q", "-c", "release/v0.1.0")
    res = _run(scaffold, "--resume", answer="y")
    assert res.returncode == 0, res.stderr
    assert "exceeds" not in res.stdout
