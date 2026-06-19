"""test_check_personal.py - tests for tools/check_personal.py.

Happy path runs the de-personalization gate against the real repo and expects
clean. The behavior tests write tiny synthetic trees in tmp_path and invoke the
checker with --repo=<tmp_path>, asserting that personal prose is flagged while
legitimate attribution, the functional repo URL, and hardware facts pass.

(tests/ is excluded from the checker's scan, so the personal-token literals in
this file never trip the real-repo happy-path run.)
"""
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/check_personal.py"


def run(repo=None):
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd.append(f"--repo={repo}")
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def test_real_repo_is_clean():
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK]" in r.stdout


def test_flags_personal_possessive(tmp_path):
    (tmp_path / "DESIGN.md").write_text("This is Keith's dev box.\n")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "personal possessive" in r.stdout


def test_flags_absolute_home_path(tmp_path):
    (tmp_path / "README.md").write_text("Config lives in /home/keith/.config.\n")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "home path" in r.stdout


def test_flags_personal_state_dir(tmp_path):
    (tmp_path / "notes.md").write_text("State at ~/sf-state by default.\n")
    r = run(tmp_path)
    assert r.returncode == 1


def test_allows_attribution_lines(tmp_path):
    # The bare name on a copyright / maintainer / --author line is legitimate.
    (tmp_path / "a.md").write_text(
        "Copyright (c) 2026 Keith Raghubar\n"
        "# Maintainer: Keith Raghubar <x@y>\n"
        '  --author "Keith Raghubar"\n'
    )
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout


def test_allows_functional_repo_url(tmp_path):
    # The repo URL (KeithRaghubar org slug) must never be flagged.
    (tmp_path / "b.md").write_text(
        "clone https://github.com/KeithRaghubar/sysforge.git\n"
    )
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout


def test_keeps_hardware_facts(tmp_path):
    # Hardware strings are not identity -- they must pass untouched.
    (tmp_path / "c.md").write_text(
        "Tested on Ryzen 7 5800X3D, RTX 5070, nvidia-open-dkms.\n"
    )
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout


def test_excludes_tests_and_internal_dirs(tmp_path):
    # A personal token inside an excluded dir is ignored: .remember/ (rolling
    # handoffs), tests/ (fixtures), and .claude/worktrees/ (local checkouts).
    for sub in (".remember", "tests", ".claude/worktrees"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / "x.md").write_text("Keith's private note\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout


def _git_init(repo: Path):
    for args in (["init", "-q"], ["config", "user.email", "x@y"],
                 ["config", "user.name", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)


def test_gitignored_files_are_skipped(tmp_path):
    # A .gitignore'd file with a personal token must NOT be flagged: the
    # publication surface is what git would ship. .aider* logs are the real case.
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(".aider*\n")
    (tmp_path / ".aider.chat.history.md").write_text("ran in /home/keith/src\n")
    r = run(tmp_path)
    assert r.returncode == 0, r.stdout


def test_tracked_personal_token_still_flagged_in_git_repo(tmp_path):
    # The gitignore filter must not suppress a non-ignored (publishable) file.
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text(".aider*\n")
    (tmp_path / "DESIGN.md").write_text("Config lives in /home/keith/.config.\n")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "home path" in r.stdout


def test_scans_shared_claude_config(tmp_path):
    # The committed .claude/ subset (hooks, agents, skills, hookify rules) IS
    # scanned now that it travels with the repo -- PII there must be caught.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/hookify.x.local.md").write_text("Keith's private note\n")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "personal possessive" in r.stdout
