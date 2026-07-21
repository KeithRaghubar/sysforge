"""test_gen_roadmap_table.py - tests for tools/gen_roadmap_table.py.

The happy-path test runs the drift check against the real repo and expects the
committed Planned summary table to match the entries' Priority/Effort tags. The
behavior tests copy ROADMAP.md into tmp_path and invoke the generator with
--repo=<tmp_path>, so an edited entry makes --check fail until regenerated, and a
malformed/untagged entry is a hard error in both modes.
"""
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/gen_roadmap_table.py"


def run(args=(), repo=None):
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd.append(f"--repo={repo}")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def _clone(dst: Path) -> Path:
    shutil.copyfile(REPO / "ROADMAP.md", dst / "ROADMAP.md")
    return dst


def _table(text: str) -> str:
    """Extract the generated table block (between the BEGIN/END markers)."""
    begin = text.index("<!-- BEGIN roadmap-table")
    end = text.index("<!-- END roadmap-table -->")
    return text[begin:end]


def test_real_repo_table_is_current():
    r = run(["--check"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_fails_when_priority_changed(tmp_path):
    _clone(tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    # Flip a tag without regenerating -> the committed table is now stale.
    roadmap.write_text(
        roadmap.read_text().replace(
            "*Priority: low · Effort: small* — observability completeness",
            "*Priority: high · Effort: small* — cosmetic + run-log",
        )
    )
    r = run(["--check"], repo=tmp_path)
    assert r.returncode == 1
    assert "stale" in (r.stdout + r.stderr)


def test_regenerate_makes_check_pass(tmp_path):
    _clone(tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text().replace(
            "*Priority: low · Effort: small* — observability completeness",
            "*Priority: high · Effort: small* — cosmetic + run-log",
        )
    )
    assert run([], repo=tmp_path).returncode == 0
    assert run(["--check"], repo=tmp_path).returncode == 0
    # A high-priority entry sorts to the top of the regenerated table.
    table = _table(roadmap.read_text())
    first_row = [ln for ln in table.splitlines() if ln.startswith("| `")][0]
    assert "2.4.0-F1" in first_row and "high" in first_row


def test_missing_tag_is_a_hard_error(tmp_path):
    _clone(tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    # Strip a Planned entry's tag markers -> no `*Priority: … · Effort: …*` tag,
    # so no valid table can be emitted.
    roadmap.write_text(
        roadmap.read_text().replace(
            "*Priority: low · Effort: small* — observability completeness",
            "observability completeness",
        )
    )
    for args in ([], ["--check"]):
        r = run(args, repo=tmp_path)
        assert r.returncode == 1, args
        assert "no `*Priority" in r.stderr


def test_invalid_vocabulary_is_a_hard_error(tmp_path):
    _clone(tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text().replace(
            "*Priority: low · Effort: small* — observability completeness",
            "*Priority: urgent · Effort: small* — cosmetic + run-log",
        )
    )
    r = run(["--check"], repo=tmp_path)
    assert r.returncode == 1
    assert "priority 'urgent'" in r.stderr


def test_sort_is_priority_then_effort(tmp_path):
    _clone(tmp_path)
    roadmap = tmp_path / "ROADMAP.md"
    run([], repo=tmp_path)
    rows = [ln for ln in _table(roadmap.read_text()).splitlines() if ln.startswith("| `")]
    rank = {"high": 0, "med": 1, "low": 2}
    erank = {"small": 0, "medium": 1, "large": 2}
    keys = [(rank[r.split("|")[3].strip()], erank[r.split("|")[4].strip()]) for r in rows]
    assert keys == sorted(keys)
