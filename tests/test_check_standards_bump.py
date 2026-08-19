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


# ---------------------------------------------------------------------------
# 3.0.0-STD3 — a `## Removed` entry is major *unless* the registry says shim
# ---------------------------------------------------------------------------

_REGISTRY_HEAD = '''\
from dataclasses import dataclass

CONFIG_KEY = "config_key"
CLI_FLAG = "cli_flag"
COMPAT = "compat"
SHIM = "shim"


@dataclass(frozen=True)
class Deprecation:
    surface: str
    kind: str
    function: str
    deprecated_in: str
    removed_in: str
    replacement: str
    anchor: str | None = None


_REGISTRY = (
'''

_SHIM_REC = '''\
    Deprecation(
        surface="doctor.flat_flags",
        kind=CLI_FLAG,
        function=SHIM,
        deprecated_in="3.0.0",
        removed_in="3.1.0",
        replacement="doctor system",
        anchor="sysforge/doctor.py::doctor_migration_hint",
    ),
'''

_COMPAT_REC = '''\
    Deprecation(
        surface="profiles.build_mode=patched_pkgbuild",
        kind=CONFIG_KEY,
        function=COMPAT,
        deprecated_in="2.0.0",
        removed_in="4.0.0",
        replacement="source_built",
    ),
'''


def _mk_repo(tmp_path, committed_records, worktree_records, git=True):
    """A throwaway repo whose deprecations.py was committed with one set of
    records and now carries another — the shape a removal commit produces."""
    import subprocess
    src = tmp_path / "sysforge" / "primitives"
    src.mkdir(parents=True)
    f = src / "deprecations.py"
    f.write_text(_REGISTRY_HEAD + "".join(committed_records) + ")\n",
                 encoding="utf-8")
    if git:
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": __import__("os").environ.get("PATH", "")}
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"],
                       check=True, env=env)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"],
                       check=True, env=env)
    f.write_text(_REGISTRY_HEAD + "".join(worktree_records) + ")\n",
                 encoding="utf-8")
    return tmp_path


def _removed(body):
    return f"# sysforge (unreleased)\n\n## Removed\n\n- {body}\n"


def test_shim_removal_derives_minor(tmp_path):
    """The record is deleted by the commit that does the removal, so only git
    history can still say it was a shim — and a shim already failed, so its
    deletion is not breaking."""
    repo = _mk_repo(tmp_path, [_SHIM_REC], [])
    bump, evidence = cs.derive_bump(
        _removed("`doctor.flat_flags` is gone (3.0.0-STD3)."), repo)
    assert bump == "minor"
    assert "shim" in evidence


def test_compat_removal_still_derives_major(tmp_path):
    """A compat surface still works, so removing it breaks a live config."""
    repo = _mk_repo(tmp_path, [_COMPAT_REC], [])
    bump, evidence = cs.derive_bump(
        _removed("`profiles.build_mode=patched_pkgbuild` is gone (9.9.9-STD1)."),
        repo)
    assert bump == "major"
    assert "compat" in evidence


def test_mixed_removal_section_takes_the_stronger_bump(tmp_path):
    """One compat entry in the section is enough to force major."""
    repo = _mk_repo(tmp_path, [_SHIM_REC, _COMPAT_REC], [])
    text = (_removed("`doctor.flat_flags` is gone (3.0.0-STD3).")
            + "- `profiles.build_mode=patched_pkgbuild` is gone (9.9.9-STD1).\n")
    assert cs.derive_bump(text, repo)[0] == "major"


def test_one_entry_naming_both_kinds_is_major(tmp_path):
    """A single entry that removes a shim *and* a compat surface is breaking."""
    repo = _mk_repo(tmp_path, [_SHIM_REC, _COMPAT_REC], [])
    assert cs.derive_bump(_removed(
        "`doctor.flat_flags` and `profiles.build_mode=patched_pkgbuild` go "
        "(9.9.9-STD1)."), repo)[0] == "major"


def test_unrecognised_surface_falls_back_to_major(tmp_path):
    """Prose the registry cannot identify must never weaken the bump — the
    derivation only ever guesses in the strengthening direction."""
    repo = _mk_repo(tmp_path, [_SHIM_REC], [])
    assert cs.derive_bump(_removed("some undocumented thing (9.9.9-B1)."),
                          repo)[0] == "major"


def test_non_repo_falls_back_to_major(tmp_path):
    """No git history to consult (a tarball, a `--repo` pointing outside a
    checkout) and a record already deleted from the working tree: nothing can
    classify the surface, so the safe default holds."""
    repo = _mk_repo(tmp_path, [_SHIM_REC], [], git=False)
    assert cs.derive_bump(_removed("`doctor.flat_flags` is gone."), repo)[0] \
        == "major"


def test_working_tree_record_classifies_without_git(tmp_path):
    """History is only needed once the record is deleted. While the record is
    still in the working tree, git never has to be consulted."""
    repo = _mk_repo(tmp_path, [], [_SHIM_REC], git=False)
    assert cs.derive_bump(_removed("`doctor.flat_flags` is gone."), repo)[0] \
        == "minor"


def test_missing_registry_falls_back_to_major(tmp_path):
    """No registry at all — nothing can be classified, so major."""
    assert cs.derive_bump(_removed("`doctor.flat_flags` is gone."),
                          tmp_path)[0] == "major"


def test_working_tree_record_wins_over_history(tmp_path):
    """A record edited before removal is read as it last stood, not as first
    committed — newest revision wins, working tree last."""
    flipped = _SHIM_REC.replace("function=SHIM", "function=COMPAT") \
                       .replace('        anchor="sysforge/doctor.py::doctor_migration_hint",\n', "")
    repo = _mk_repo(tmp_path, [_SHIM_REC], [flipped])
    assert cs.derive_bump(_removed("`doctor.flat_flags` is gone."),
                          repo)[0] == "major"


def test_no_repo_argument_keeps_removed_at_major(tmp_path):
    """The default call path is unchanged: without a repo there is nothing to
    consult, so Removed stays major."""
    assert cs.derive_bump(_removed("`doctor.flat_flags` is gone."))[0] == "major"
