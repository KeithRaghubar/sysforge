"""test_build_design.py - tests for tools/build_design.py.

The happy-path test runs the drift check against the real repo and expects the
committed DESIGN.md to match its docs/design/ sources. The behavior tests copy
docs/design/ + DESIGN.md into tmp_path and invoke the generator with
--repo=<tmp_path>, so an edited source makes --check fail until regenerated.
"""
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/build_design.py"


def run(args=(), repo=None):
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd.append(f"--repo={repo}")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def _clone_design_tree(dst: Path) -> Path:
    """Copy docs/design/ and DESIGN.md into dst so the generator can run there."""
    shutil.copytree(REPO / "docs/design", dst / "docs/design")
    shutil.copyfile(REPO / "DESIGN.md", dst / "DESIGN.md")
    return dst


def test_real_repo_design_is_current():
    r = run(["--check"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK]" in r.stdout


def test_check_fails_when_source_edited(tmp_path):
    _clone_design_tree(tmp_path)
    # Edit a source without regenerating -> DESIGN.md is now stale.
    src = tmp_path / "docs/design/01-philosophy.md"
    src.write_text(src.read_text() + "\nAn extra design note.\n")
    r = run(["--check"], repo=tmp_path)
    assert r.returncode == 1
    assert "out of date" in (r.stdout + r.stderr)


def test_regenerate_makes_check_pass(tmp_path):
    _clone_design_tree(tmp_path)
    src = tmp_path / "docs/design/01-philosophy.md"
    src.write_text(src.read_text() + "\nAn extra design note.\n")
    # Regenerate, then the drift check passes again.
    assert run([], repo=tmp_path).returncode == 0
    r = run(["--check"], repo=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "An extra design note." in (tmp_path / "DESIGN.md").read_text()


def test_generated_design_starts_with_banner(tmp_path):
    _clone_design_tree(tmp_path)
    run([], repo=tmp_path)
    text = (tmp_path / "DESIGN.md").read_text()
    assert text.startswith("<!-- GENERATED FILE")
    # The original title must immediately follow the banner (content preserved).
    assert "# SysForge Design Document" in text
