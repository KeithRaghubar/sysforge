"""
test_release_resume.py — tools/release.sh --resume mode (2.0.0-B1).

A failure *after* the release tag exists (e.g. chroot validation) needs fix
commits on top of the release commit; the exact-HEAD auto-resume then never
fires and a re-run computes a fresh bump from the already-bumped version.
`--resume` re-enters Phase 3 for the current version when the v$CUR tag is an
ancestor of a clean HEAD.

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
        ".PHONY: check-shipped check-personal check-design check-standards man\n"
        "check-shipped check-personal check-design check-standards man:\n"
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


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "tools/release.sh", *args, "--dry-run"],
        cwd=repo, input="n\n", capture_output=True, text=True,
    )


def test_resume_accepts_tag_ancestor_of_clean_head(scaffold):
    _git(scaffold, "tag", "v0.1.0")
    (scaffold / "fix.txt").write_text("post-tag fix\n")
    _git(scaffold, "add", "fix.txt")
    _git(scaffold, "commit", "-q", "-m", "fix: post-tag chroot failure")
    res = _run(scaffold, "--resume")
    assert res.returncode == 0, res.stderr
    assert "resuming v0.1.0" in res.stdout


def test_resume_still_works_with_tag_exactly_at_head(scaffold):
    _git(scaffold, "tag", "v0.1.0")
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
