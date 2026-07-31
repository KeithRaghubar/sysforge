"""
test_help_cmd.py — unit tests for the ``sysforge help`` verb (2.5.0-F2).

`help` is a read-only, non-sentinel verb that prints the same text argparse
would emit for ``--help``, either for the top level or for a named COMMAND
(including nested subverbs like ``help state failed``). Unknown topics are a
usage error, not a traceback.
"""
import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent / ".."))

from sysforge.cli import _build_parser
from sysforge.help_cmd import HelpVerb


def _run(capsys, topic):
    args = _build_parser().parse_args(["help", *topic])
    verb = HelpVerb()
    pre = verb.pre_check(args)
    assert pre.skip_reason is None and pre.blocker is None
    result = verb.execute(args, pre)
    return result, capsys.readouterr()


def test_help_verb_is_read_only():
    assert HelpVerb.requires_sentinel is False


def test_help_parser_wires_the_verb_class():
    args = _build_parser().parse_args(["help"])
    assert args.verb_cls is HelpVerb


def test_bare_help_prints_top_level_help(capsys):
    result, out = _run(capsys, [])
    assert result.exit_code == 0
    assert out.out == _build_parser().format_help()


def test_help_command_prints_that_commands_help(capsys):
    result, out = _run(capsys, ["build"])
    assert result.exit_code == 0
    assert "sysforge build" in out.out
    # Top-level-only content must not leak into a per-verb dump.
    assert "Everyday:" not in out.out


def test_help_walks_nested_subverbs(capsys):
    result, out = _run(capsys, ["state", "failed"])
    assert result.exit_code == 0
    assert "sysforge state failed" in out.out


def test_unknown_topic_is_a_usage_error(capsys):
    result, out = _run(capsys, ["nosuchverb"])
    assert result.exit_code == 2
    combined = out.out + out.err
    assert "nosuchverb" in combined
    # The error lists the valid topics at that level so the user can recover.
    assert "build" in combined


def test_unknown_nested_topic_names_the_parent(capsys):
    result, out = _run(capsys, ["state", "nosuchsub"])
    assert result.exit_code == 2
    combined = out.out + out.err
    assert "nosuchsub" in combined
    assert "state" in combined


def test_help_matches_the_dash_dash_help_flag(capsys):
    """The verb is an alias, not a re-implementation: identical bytes."""
    _, out = _run(capsys, ["resolve"])
    parser = _build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    assert out.out == sub.choices["resolve"].format_help()
