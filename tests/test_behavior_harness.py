"""Proves the verb behavior harness (conftest: run_cli / cli_capture).

These validate the harness mechanics the P0.4+ rewrites depend on: exit-code
capture across the three pre-check terminal states, log tag/level parsing, and
the real-parser dispatch path.
"""
import argparse

from sysforge import log
from sysforge.verbs import ExecResult, PreCheckResult, Verb, run_verb


class _DemoVerb(Verb):
    name = "demo"

    def __init__(self, *, rc=0, skip=None, blocker=None):
        self._rc = rc
        self._skip = skip
        self._blocker = blocker

    def pre_check(self, args):
        if self._skip:
            return PreCheckResult(skip_reason=self._skip)
        if self._blocker:
            return PreCheckResult(blocker=self._blocker, exit_code=3)
        return PreCheckResult()

    def execute(self, args, pre):
        log.get_logger("DEMO").info("did the work")
        return ExecResult(exit_code=self._rc)


def _run_demo(cli_capture, **kw):
    return cli_capture(
        lambda: run_verb(_DemoVerb(**kw), argparse.Namespace(state_dir=None))
    )


def test_capture_exit_code_and_tags(cli_capture):
    r = _run_demo(cli_capture, rc=0)
    assert r.exit_code == 0
    assert "DEMO" in r.tags
    assert r.logged("did the work", tag="DEMO", level="INFO")


def test_capture_custom_exit_code(cli_capture):
    assert _run_demo(cli_capture, rc=42).exit_code == 42


def test_capture_skip_short_circuits(cli_capture):
    r = _run_demo(cli_capture, skip="not applicable")
    assert r.exit_code == 0
    assert not r.logged("did the work")  # execute never ran


def test_capture_blocker_logs_error(cli_capture):
    r = _run_demo(cli_capture, blocker="bad args")
    assert r.exit_code == 3
    assert r.logged("bad args", tag="DEMO", level="ERROR")


def test_run_cli_help_exits_zero(run_cli):
    r = run_cli(["--help"])
    assert r.exit_code == 0
    assert "usage" in r.output.lower()


def test_run_cli_unknown_verb_errors(run_cli):
    r = run_cli(["definitely-not-a-verb"])
    assert r.exit_code == 2
    assert "invalid choice" in r.output or "usage" in r.output.lower()


def test_run_cli_real_readonly_verb(run_cli, state_dir):
    # `state list` is read-only; against an empty state dir it must exit 0.
    r = run_cli(["state", "list"])
    assert r.exit_code == 0
