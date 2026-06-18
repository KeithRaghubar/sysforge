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
