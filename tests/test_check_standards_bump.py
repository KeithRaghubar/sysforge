# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Standards row 3 — required bump derived from the release-notes accumulator.

Every case uses a SYNTHETIC accumulator. No test may assert on the real
docs/release-notes/unreleased.md: it would pass now and fail the moment
tools/release.sh Phase 1 reseeds it.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import check_standards as cs  # noqa: E402
sys.path.pop(0)


_ADDED = "# sysforge (unreleased)\n\n## Added\n\n- a new axis (2.6.1-F2).\n"
_FIXED = "# sysforge (unreleased)\n\n## Fixed\n\n- a bug (2.6.1-B1).\n"
_REMOVED = "# sysforge (unreleased)\n\n## Removed\n\n- the flat flags (2.6.1-F1).\n"
_BREAKING_IN_CHANGED = (
    "# sysforge (unreleased)\n\n## Changed\n\n"
    "- **Breaking:** doctor is now two subcommands (2.6.1-F1).\n")
_MIXED = _ADDED + "\n## Fixed\n\n- a bug (2.6.1-B1).\n"
_EMPTY = "# sysforge (unreleased)\n\n<!-- nothing yet -->\n"


@pytest.mark.parametrize("text,expected", [
    (_ADDED, "minor"),
    (_FIXED, "patch"),
    (_REMOVED, "major"),
    (_BREAKING_IN_CHANGED, "major"),
    (_MIXED, "minor"),
    (_EMPTY, None),
])
def test_derive_bump(text, expected):
    bump, _evidence = cs.derive_bump(text)
    assert bump == expected


def test_evidence_names_its_source():
    bump, evidence = cs.derive_bump(_REMOVED)
    assert bump == "major"
    assert "Removed" in evidence


def test_breaking_marker_outranks_its_section():
    """A **Breaking:** bullet under Changed must beat Changed's own `patch`."""
    bump, evidence = cs.derive_bump(_BREAKING_IN_CHANGED)
    assert bump == "major"
    assert "Breaking" in evidence


def test_strongest_signal_wins_regardless_of_order():
    reversed_order = _FIXED + "\n## Removed\n\n- gone (2.6.1-F5).\n"
    assert cs.derive_bump(reversed_order)[0] == "major"


def test_comment_block_is_not_an_entry():
    """The accumulator ships with an HTML instruction comment naming sections;
    it must not be mistaken for content."""
    text = ("# sysforge (unreleased)\n\n<!--\nsections: Added, Changed,\n"
            "Removed, Fixed\n-->\n\n## Fixed\n\n- a bug (2.6.1-B1).\n")
    assert cs.derive_bump(text)[0] == "patch"


def _synthetic_repo(tmp_path, accumulator: str, removed_in: str = "3.0.0"):
    """A tree with one compat record and a controllable accumulator."""
    (tmp_path / "sysforge" / "primitives").mkdir(parents=True)
    (tmp_path / "sysforge" / "primitives" / "deprecations.py").write_text(
        'CONFIG_KEY = "config_key"\n'
        'COMPAT = "compat"\n'
        '_REGISTRY = (\n'
        '    Deprecation(\n'
        '        surface="git.pull_timeout",\n'
        '        kind=CONFIG_KEY,\n'
        '        function=COMPAT,\n'
        '        deprecated_in="1.0.0",\n'
        f'        removed_in="{removed_in}",\n'
        '        replacement="git.fetch_timeout",\n'
        '    ),\n'
        ')\n', encoding="utf-8")
    notes = tmp_path / "docs" / "release-notes"
    notes.mkdir(parents=True)
    (notes / "unreleased.md").write_text(accumulator, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.6.1"\n', encoding="utf-8")
    return tmp_path


def test_removal_without_a_release_note_is_an_error(tmp_path):
    repo = _synthetic_repo(tmp_path, _ADDED)
    findings = cs.check_semver_bump(repo, target_version="3.0.0")
    msgs = [f.message for f in findings]
    assert any("git.pull_timeout" in m and "Removed" in m for m in msgs), msgs


def test_removal_with_a_matching_release_note_passes(tmp_path):
    acc = ("# sysforge (unreleased)\n\n## Removed\n\n"
           "- **Breaking:** `git.pull_timeout` is gone (2.6.1-F5).\n")
    repo = _synthetic_repo(tmp_path, acc)
    findings = cs.check_semver_bump(repo, target_version="3.0.0")
    assert [f for f in findings if f.severity == "error"] == []


def test_removal_not_yet_due_needs_no_release_note(tmp_path):
    repo = _synthetic_repo(tmp_path, _ADDED, removed_in="4.0.0")
    findings = cs.check_semver_bump(repo, target_version="3.0.0")
    assert [f for f in findings if f.severity == "error"] == []


# --- gaps found in review ---------------------------------------------------

_DEPRECATED = ("# sysforge (unreleased)\n\n## Deprecated\n\n"
               "- `[git] pull_timeout` is scheduled for removal (2.6.1-STD2).\n")
_EMPTY_SECTION = ("# sysforge (unreleased)\n\n## Added\n\n## Fixed\n\n"
                  "- a bug (2.6.1-B1).\n")


def test_deprecated_section_implies_minor():
    """`## Deprecated` is one of six mapped sections; it had no coverage."""
    assert cs.derive_bump(_DEPRECATED)[0] == "minor"


def test_section_heading_with_no_bullets_contributes_nothing():
    """An empty `## Added` mid-edit must not imply minor on its own."""
    bump, evidence = cs.derive_bump(_EMPTY_SECTION)
    assert bump == "patch"
    assert "Fixed" in evidence


def test_semver_bump_runs_through_the_group_dispatch(tmp_path):
    """The inspect.signature dispatch is the mechanism by which a group opts
    into --target-version. check_semver_bump is the SECOND group to opt in, so
    exercise it through main() rather than only calling it directly."""
    import subprocess
    import sys as _sys
    repo = _synthetic_repo(tmp_path, _ADDED)
    r = subprocess.run(
        [_sys.executable, "tools/check_standards.py", f"--repo={repo}",
         "--check=semver_bump", "--target-version=3.0.0"],
        cwd=REPO, capture_output=True, text=True)
    # The record declares removed_in=3.0.0 and the accumulator has no `##
    # Removed` entry naming it, so R4 must fire -> exit 1 with the surface named.
    assert r.returncode == 1, r.stdout + r.stderr
    assert "git.pull_timeout" in r.stdout + r.stderr


def _cli_repo(tmp_path, accumulator: str):
    """A minimal tree check_standards.py can run against via --repo."""
    notes = tmp_path / "docs" / "release-notes"
    notes.mkdir(parents=True)
    (notes / "unreleased.md").write_text(accumulator, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "2.6.1"\n', encoding="utf-8")
    return tmp_path


def _run(args, repo):
    import subprocess
    import sys as _sys
    return subprocess.run(
        [_sys.executable, "tools/check_standards.py", f"--repo={repo}", *args],
        cwd=REPO, capture_output=True, text=True)


def test_derive_bump_flag_prints_the_word(tmp_path):
    repo = _cli_repo(tmp_path, _REMOVED)
    r = _run(["--derive-bump"], repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "major"


def test_derive_bump_on_an_empty_accumulator_exits_1(tmp_path):
    repo = _cli_repo(tmp_path, _EMPTY)
    r = _run(["--derive-bump"], repo)
    assert r.returncode == 1
    assert "no authored entries" in r.stderr


def test_require_bump_accepts_an_equal_bump(tmp_path):
    repo = _cli_repo(tmp_path, _REMOVED)
    r = _run(["--require-bump=major"], repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "required = major" in r.stdout


def test_require_bump_accepts_a_stronger_bump(tmp_path):
    repo = _cli_repo(tmp_path, _ADDED)          # requires minor
    r = _run(["--require-bump=major"], repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_require_bump_rejects_a_weaker_bump(tmp_path):
    repo = _cli_repo(tmp_path, _REMOVED)        # requires major
    r = _run(["--require-bump=patch"], repo)
    assert r.returncode == 1
    assert "required = major" in r.stdout
    assert "weaker" in r.stderr


def test_require_bump_rejects_an_unknown_kind(tmp_path):
    repo = _cli_repo(tmp_path, _REMOVED)
    r = _run(["--require-bump=nonsense"], repo)
    assert r.returncode == 2


def test_require_bump_unknown_kind_outranks_empty_accumulator(tmp_path):
    """A usage error (bad KIND) must be reported even when the accumulator
    itself is empty — exit 2 outranks exit 1."""
    repo = _cli_repo(tmp_path, _EMPTY)
    r = _run(["--require-bump=nonsense"], repo)
    assert r.returncode == 2


def test_empty_target_version_is_rejected(tmp_path):
    """An explicitly-empty --target-version must fail the SemVer check, not
    silently fall back to the pyproject.toml version."""
    repo = _cli_repo(tmp_path, _REMOVED)
    r = _run(["--target-version=", "--check=deprecations"], repo)
    assert r.returncode == 2
    assert "not strict" in r.stderr


def test_empty_require_bump_is_rejected(tmp_path):
    """An explicitly-empty --require-bump must fail, not silently skip the
    whole gate (which would exit 0)."""
    repo = _cli_repo(tmp_path, _REMOVED)
    r = _run(["--require-bump="], repo)
    assert r.returncode == 2


def test_bump_vocabulary_has_one_home():
    """Both tools must agree on the vocabulary because both import it."""
    sys.path.insert(0, str(REPO / "tools"))
    try:
        import _semver_vocab
        import gen_roadmap_table as g
    finally:
        sys.path.pop(0)
    assert cs.BUMP_ORDER is _semver_vocab.BUMP_ORDER
    assert g.BUMP_ORDER is _semver_vocab.BUMP_ORDER
