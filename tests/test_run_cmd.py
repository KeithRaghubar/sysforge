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
