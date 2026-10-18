"""
test_run_cmd.py — RunVerb argument → RunOptions plumbing.

Guards the "last-mile" wiring where a CLI flag must be copied into the
``RunOptions`` the stage actually reads (the class of bug that left
``--reuse-built`` silently dead: defined in the parser, read by the stage via
``getattr(options, "reuse_built", False)``, but never forwarded by the verb).
"""
from sysforge.cli import _build_parser


def _capture_options(monkeypatch):
    captured = {}

    def _fake(stage, config, options):
        captured["options"] = options

    monkeypatch.setattr(
        "sysforge.pipeline.runner.run_stage_standalone", _fake)
    return captured


def _run_toolchain(argv, monkeypatch):
    captured = _capture_options(monkeypatch)
    args = _build_parser().parse_args(["run", "toolchain", *argv])
    args.verb_cls().execute(args, pre=None)
    return captured["options"]


def test_run_toolchain_forwards_reuse_built(monkeypatch):
    opts = _run_toolchain(["--reuse-built"], monkeypatch)
    assert opts.reuse_built is True


def test_run_toolchain_reuse_built_defaults_false(monkeypatch):
    opts = _run_toolchain([], monkeypatch)
    assert opts.reuse_built is False


def test_run_options_has_reuse_built_field():
    from sysforge.pipeline.stages.base import RunOptions
    assert RunOptions().reuse_built is False


# --- euid == 0 guard on the standalone build verbs (1.2.0-B11, 2.1.0-B4) -----
#
# The standalone build verbs (packages/kernel/toolchain) are always run as the
# regular user and must fail fast in pre_check when re-entered as root. The
# full-pipeline verb is deliberately exempt: it spans the root-run bootstrap
# phase on the live ISO, so the runner enforces the no-root rule per stage
# (see test_pipeline_runner.py::test_makepkg_bearing_stage_refuses_root).

def _pre_check_as(euid, verb_argv, monkeypatch):
    import sysforge.run_cmd as run_cmd
    monkeypatch.setattr(run_cmd.os, "geteuid", lambda: euid)
    args = _build_parser().parse_args(["run", *verb_argv])
    return args.verb_cls().pre_check(args)


def test_standalone_build_verbs_block_as_root(monkeypatch):
    for verb in (["packages"], ["kernel"], ["toolchain"]):
        pre = _pre_check_as(0, verb, monkeypatch)
        assert pre.blocker is not None, f"run {verb[0]} must block as root"
        assert "root" in pre.blocker
        assert "sudo -u" in pre.blocker


def test_pipeline_and_bootstrap_verbs_proceed_as_root(monkeypatch):
    # pipeline must NOT block at the verb level — the runner guards per stage so
    # the root-run bootstrap phase (ISO) is allowed through.
    for verb in (["pipeline"], ["hardware"], ["reconfigure"]):
        pre = _pre_check_as(0, verb, monkeypatch)
        assert pre.blocker is None, f"run {verb[0]} must not block as root"


def test_run_verbs_proceed_as_normal_user(monkeypatch):
    for verb in (["pipeline"], ["packages"], ["kernel"], ["toolchain"]):
        pre = _pre_check_as(1000, verb, monkeypatch)
        assert pre.blocker is None


# --- Hardware/reconfigure unified logging (2.1.0-F4) -----

def test_run_hardware_enables_unified_log(monkeypatch):
    captured = _capture_options(monkeypatch)
    args = _build_parser().parse_args(["run", "hardware"])
    args.verb_cls().execute(args, pre=None)
    assert captured["options"].no_unified_log is False


def test_run_reconfigure_enables_unified_log(monkeypatch):
    captured = _capture_options(monkeypatch)
    args = _build_parser().parse_args(["run", "reconfigure"])
    args.verb_cls().execute(args, pre=None)
    assert captured["options"].no_unified_log is False


# --- 3.5.0-F7: --bolt-only ----------------------------------------------------

def test_run_toolchain_bolt_only_forwards_and_implies_no_update(monkeypatch):
    opts = _run_toolchain(["--bolt-only"], monkeypatch)
    assert opts.bolt_only is True and opts.no_update is True
    assert _run_toolchain([], monkeypatch).bolt_only is False


def test_run_toolchain_bolt_only_refuses_build_flags(monkeypatch):
    for flag in ("--rebuild-profdata", "--reuse-built", "--cleansrc", "--cleansrc-force"):
        pre = _pre_check_as(1000, ["toolchain", "--bolt-only", flag], monkeypatch)
        assert pre.blocker is not None and flag in pre.blocker, flag
        assert pre.exit_code == 2
    assert _pre_check_as(1000, ["toolchain", "--bolt-only"], monkeypatch).blocker is None


# --- 3.5.0-F8: --install-only -------------------------------------------------

def test_run_toolchain_install_only_forwards_and_implies_no_update(monkeypatch):
    opts = _run_toolchain(["--install-only"], monkeypatch)
    assert opts.install_only is True and opts.no_update is True
    assert _run_toolchain([], monkeypatch).install_only is False


def test_run_toolchain_install_only_refuses_build_flags_and_bolt_only(monkeypatch):
    for flag in ("--rebuild-profdata", "--reuse-built", "--cleansrc", "--cleansrc-force",
                 "--bolt-only"):
        pre = _pre_check_as(1000, ["toolchain", "--install-only", flag], monkeypatch)
        assert pre.blocker is not None and flag in pre.blocker, flag
        assert pre.exit_code == 2
    assert _pre_check_as(1000, ["toolchain", "--install-only"], monkeypatch).blocker is None


def test_cache_report_help_does_not_say_pipeline_on_every_run_verb():
    """3.5.0-B34: --cache-report is shared by every `run` subcommand; its help
    must describe the run it is on, not the bootstrap pipeline."""
    parser = _build_parser()
    run = next(a for a in parser._subparsers._group_actions[0].choices["run"]._actions
               if hasattr(a, "choices") and a.choices and "toolchain" in a.choices)
    for verb in ("pipeline", "toolchain", "packages", "kernel"):
        action = next(a for a in run.choices[verb]._actions if a.dest == "cache_report")
        assert "pipeline" not in action.help, verb


# --- 3.5.0-B35: help text outside the bootstrap says what it means -----------

def _help_strings(parser, path="sysforge"):
    import argparse
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for ca in a._choices_actions:
                yield f"{path} {ca.dest}", ca.help or ""
            for name, sub in a.choices.items():
                yield from _help_strings(sub, f"{path} {name}")
        elif a.help:
            yield f"{path} {'/'.join(a.option_strings) or a.dest}", str(a.help)


def test_help_says_pipeline_only_for_the_bootstrap_command():
    """'pipeline' names `run pipeline`; elsewhere it was shorthand for 'a build
    stage' or 'what the last run toolchain recorded', which a steady-state
    user cannot decode."""
    import re
    offenders = []
    for where, text in _help_strings(_build_parser()):
        if where.startswith("sysforge run pipeline"):
            continue  # the bootstrap command describes itself
        text = text.replace("(pipeline, hardware, reconfigure)", "")
        if re.search(r"\bpipeline\b", text, re.IGNORECASE):
            offenders.append(f"{where}: {text}")
    assert offenders == []


def test_top_level_description_is_the_package_description():
    import tomllib
    from pathlib import Path
    project = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]
    assert _build_parser().description == project["description"]
