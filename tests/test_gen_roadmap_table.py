"""test_gen_roadmap_table.py - tests for tools/gen_roadmap_table.py.

`test_real_repo_table_is_current` is the only test that reads the real
ROADMAP.md: it is the genuine "committed table is in sync with the entries"
guard. Every behavior test builds a **synthetic** ROADMAP.md in tmp_path instead
(`_make_repo`) and invokes the generator with --repo=<tmp_path>, so retagging an
entry makes --check fail until regenerated, and a malformed/untagged entry is a
hard error in both modes.

Synthetic fixtures on purpose: these tests used to clone the real ROADMAP.md and
mutate it with str.replace() on an exact prose substring. str.replace() cannot
fail, so when the roadmap was reworded (and when the entry the tests keyed on
shipped and was removed) the mutation silently became a no-op and the tests
asserted nothing about the code under test. `_retag` below anchors on the entry
ID and asserts it actually changed something, so a fixture that stops matching
fails loudly as a fixture error.
"""
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/gen_roadmap_table.py"

_BEGIN = "<!-- BEGIN roadmap-table"
_END = "<!-- END roadmap-table -->"

# (id, title, priority, effort, bump) — spans both priority and effort
# vocabularies so the sort test has something to order. Titles are deliberately
# generic.
_SYNTHETIC_ENTRIES = [
    ("2.0.0-B1", "Renderer bypasses the logger", "low", "small", "patch"),
    ("2.0.0-B2", "Guard couples to the local renderer version", "low", "medium", "minor"),
    ("2.0.0-F1", "Replace sentinel tags with an ordered pipeline", "med", "large", "major"),
]


def run(args=(), repo=None):
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd.append(f"--repo={repo}")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def _roadmap_doc(entries=_SYNTHETIC_ENTRIES) -> str:
    """A minimal ROADMAP.md the generator can parse: Planned + marker block.

    `### Entries` does not close the section (the parser's terminator is `^##\\s+`
    or a bare `---`), mirroring the real file's `### Bugs` / `### Features`.
    """
    body = "\n\n".join(
        f"- **`{eid}` — {title}.** Synthetic entry body.\n"
        f"  *Priority: {pri} · Effort: {eff} · Bump: {bump}* — synthetic rationale."
        for eid, title, pri, eff, bump in entries
    )
    return (
        "# Synthetic Roadmap (test fixture)\n"
        "\n"
        "## Planned\n"
        "\n"
        f"{_BEGIN} (generated) -->\n"
        f"{_END}\n"
        "\n"
        "### Entries\n"
        "\n"
        f"{body}\n"
        "\n"
        "---\n"
        "\n"
        "## Abandoned\n"
        "\n"
        "- Nothing abandoned in the fixture.\n"
    )


def _make_repo(dst: Path, entries=_SYNTHETIC_ENTRIES) -> Path:
    """Write a synthetic ROADMAP.md and seed it with an in-sync table."""
    (dst / "ROADMAP.md").write_text(_roadmap_doc(entries), encoding="utf-8")
    assert run([], repo=dst).returncode == 0, "fixture: seeding the table failed"
    assert run(["--check"], repo=dst).returncode == 0, "fixture: seed is not in sync"
    return dst


def _retag(repo: Path, entry_id: str, tag: str) -> None:
    """Replace `entry_id`'s Priority/Effort tag with raw `tag` ("" strips it).

    Anchors on the ID rather than prose, and hard-fails if nothing matched — a
    silently-ineffective mutation is what made the previous fixtures vacuous.
    """
    roadmap = repo / "ROADMAP.md"
    text = roadmap.read_text(encoding="utf-8")
    pat = re.compile(
        rf"(- \*\*`{re.escape(entry_id)}`.*?)\*Priority:[^*]*\*", re.DOTALL
    )
    new, n = pat.subn(lambda m: m.group(1) + tag, text, count=1)
    assert n == 1, f"fixture: no Priority/Effort tag found for {entry_id}"
    assert new != text, f"fixture: retag of {entry_id} was a no-op"
    roadmap.write_text(new, encoding="utf-8")


def _table(text: str) -> str:
    """Extract the generated table block (between the BEGIN/END markers)."""
    begin = text.index(_BEGIN)
    end = text.index(_END)
    return text[begin:end]


def _rows(repo: Path) -> list[str]:
    text = (repo / "ROADMAP.md").read_text(encoding="utf-8")
    return [ln for ln in _table(text).splitlines() if ln.startswith("| `")]


def test_real_repo_table_is_current():
    r = run(["--check"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_fails_when_priority_changed(tmp_path):
    repo = _make_repo(tmp_path)
    # Flip a tag without regenerating -> the committed table is now stale.
    _retag(repo, "2.0.0-B1", "*Priority: high · Effort: small · Bump: patch*")
    r = run(["--check"], repo=repo)
    assert r.returncode == 1
    assert "stale" in (r.stdout + r.stderr)


def test_regenerate_makes_check_pass(tmp_path):
    repo = _make_repo(tmp_path)
    _retag(repo, "2.0.0-B1", "*Priority: high · Effort: small · Bump: patch*")
    assert run([], repo=repo).returncode == 0
    assert run(["--check"], repo=repo).returncode == 0
    # A high-priority entry sorts to the top of the regenerated table.
    first_row = _rows(repo)[0]
    assert "2.0.0-B1" in first_row and "high" in first_row


def test_missing_tag_is_a_hard_error(tmp_path):
    repo = _make_repo(tmp_path)
    # Strip a Planned entry's tag -> no `*Priority: … · Effort: … · Bump: …*`
    # tag, so no valid table can be emitted.
    _retag(repo, "2.0.0-B1", "")
    for args in ([], ["--check"]):
        r = run(args, repo=repo)
        assert r.returncode == 1, args
        assert "no `*Priority" in r.stderr


def test_invalid_vocabulary_is_a_hard_error(tmp_path):
    repo = _make_repo(tmp_path)
    _retag(repo, "2.0.0-B1", "*Priority: urgent · Effort: small · Bump: patch*")
    r = run(["--check"], repo=repo)
    assert r.returncode == 1
    assert "priority 'urgent'" in r.stderr


def test_missing_bump_tag_is_an_error(tmp_path):
    repo = _make_repo(tmp_path)
    _retag(repo, "2.0.0-B1", "*Priority: low · Effort: small*")
    r = run(["--check"], repo=repo)
    assert r.returncode == 1
    assert "no `*Priority" in r.stderr


def test_invalid_bump_value_is_an_error(tmp_path):
    repo = _make_repo(tmp_path)
    _retag(repo, "2.0.0-B1", "*Priority: low · Effort: small · Bump: enormous*")
    r = run(["--check"], repo=repo)
    assert r.returncode == 1
    assert "bump 'enormous'" in r.stderr


def test_rendered_table_has_a_bump_column(tmp_path):
    repo = _make_repo(tmp_path)
    text = (repo / "ROADMAP.md").read_text(encoding="utf-8")
    assert "| ID | Item | Priority | Effort | Bump |" in text
    rows = _rows(repo)
    assert any("| patch |" in r for r in rows)
    assert any("| minor |" in r for r in rows)
    assert any("| major |" in r for r in rows)


def test_sort_is_priority_then_effort(tmp_path):
    repo = _make_repo(tmp_path)
    rows = _rows(repo)
    assert len(rows) == len(_SYNTHETIC_ENTRIES)
    rank = {"high": 0, "med": 1, "low": 2}
    erank = {"small": 0, "medium": 1, "large": 2}
    keys = [(rank[r.split("|")[3].strip()], erank[r.split("|")[4].strip()]) for r in rows]
    assert keys == sorted(keys)
