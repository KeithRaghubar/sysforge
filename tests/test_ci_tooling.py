"""CI job tooling: one gate list, readable failures (3.5.0-DEV5..DEV8)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from tests.test_standards_compliance import _load_check_standards

REPO = Path(__file__).resolve().parent.parent


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _print_gates() -> list[str]:
    r = subprocess.run(
        ["make", "-s", "--no-print-directory", "-C", str(REPO), "print-gates"],
        capture_output=True, text=True, check=True)
    return r.stdout.split()


def test_pre_release_and_ci_resolve_to_the_same_gates():
    """`pre-release` depends on `$(GATES)` and ci.sh runs `print-gates`, so the
    two lists are one list (3.5.0-DEV8); the rest of `pre-release` is
    `$(CI_JOBS)`, which CI runs as jobs of their own (3.5.0-DEV21)."""
    cs = _load_check_standards()
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^pre-release:([^#\n]*)", text, re.MULTILINE)
    assert m is not None
    jobs = cs._expand_make_vars(["$(CI_JOBS)"], text)
    assert cs._expand_make_vars(m.group(1).split(), text) == _print_gates() + jobs


def test_every_ci_job_gate_has_its_own_ci_job():
    """A `pre-release` prerequisite outside GATES (`CI_JOBS`) is run by a step
    of .github/workflows/ci.yml, or CI would not run what `pre-release` does."""
    cs = _load_check_standards()
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = cs._expand_make_vars(["$(CI_JOBS)"], text)
    assert jobs and jobs != ["$(CI_JOBS)"]
    for job in jobs:
        assert re.search(rf"^\s*run: make {re.escape(job)}\b", workflow, re.MULTILINE), job


def test_ci_sh_reads_the_gate_list_instead_of_spelling_it():
    script = (REPO / "tools" / "ci.sh").read_text(encoding="utf-8")
    assert "print-gates" in script
    # A hard-coded list is how `test` and `coverage` drifted apart.
    for gate in _print_gates():
        if gate == "test":
            continue
        assert not re.search(rf"^\s*[^#\n]*\bmake\b[^\n]*\b{re.escape(gate)}\b",
                             script, re.MULTILINE), gate


def test_make_test_overlays_pytest_and_tomlkit():
    """A bare `pytest` in the overlay resolves to /usr/bin/pytest, whose
    interpreter cannot see `--with tomlkit` (3.5.0-DEV8)."""
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    for target in ("test", "test-x"):
        m = re.search(rf"^{re.escape(target)}:[^\n]*\n\t([^\n]*)", text, re.MULTILINE)
        assert m is not None, target
        assert "--with pytest " in m.group(1) and "--with tomlkit" in m.group(1), target


def test_ci_sh_groups_gates_and_summarises_only_inside_actions():
    script = (REPO / "tools" / "ci.sh").read_text(encoding="utf-8")
    assert "::group::$gate" in script and "::endgroup::" in script
    assert "GITHUB_STEP_SUMMARY" in script
    # ci-local runs outside Actions; its output stays the gates' own.
    for line in script.splitlines():
        if "::group::" in line or "::endgroup::" in line:
            assert "in_actions == true" in line, line


def test_ci_sh_prints_the_environment_before_the_gates():
    script = (REPO / "tools" / "ci.sh").read_text(encoding="utf-8")
    env_at = script.index("== environment")
    assert env_at < script.index("print-gates")
    block = script[env_at:script.index("print-gates")]
    for probe in ("pacman -Q 2>/dev/null |", "llvm", "clang", "python3 --version",
                  "pytest --version", "uv --version"):
        assert probe in block, probe


_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="1">
  <testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3"/>
  <testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="9">
    <failure message="assert 1 == 2&#10;  where 1 = f()">long traceback</failure>
  </testcase>
  <testcase classname="tests.test_b" name="test_boom" file="tests/test_b.py" line="0">
    <error message="">fixture exploded</error>
  </testcase>
</testsuite></testsuites>
"""


def test_junit_annotate_emits_one_error_per_failure(tmp_path):
    ja = _load_tool("junit_annotate")
    report = tmp_path / "junit.xml"
    report.write_text(_JUNIT, encoding="utf-8")
    lines = ja.error_lines(str(report))
    assert lines == [
        "::error file=tests/test_a.py,line=10,title=tests.test_a%3A%3Atest_bad"
        "::assert 1 == 2%0A  where 1 = f()",
        "::error file=tests/test_b.py,line=1,title=tests.test_b%3A%3Atest_boom"
        "::fixture exploded",
    ]


def test_junit_annotate_never_fails_the_run_on_a_bad_report(tmp_path):
    bad = tmp_path / "junit.xml"
    bad.write_text("<not xml", encoding="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "tools" / "junit_annotate.py"), str(bad)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == ""
    assert "cannot read" in r.stderr
