# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
update's phases as functions of an ``UpdateRun`` (3.2.0-F3).

Each phase is exercised by constructing the run record directly — the point of
making the phases functions — instead of driving the whole verb through
``update_scenario``, which ``tests/test_update.py`` already does end to end.
"""
from argparse import Namespace
from pathlib import Path

import pytest

from sysforge import update
from sysforge.primitives.build_state import BuildState
from sysforge.update import UpdateRun
from sysforge.update_result import _UpdateResult


def _run(tmp_path, **args) -> UpdateRun:
    run = UpdateRun(args=Namespace(**args))
    run.state_dir = tmp_path
    run.bs = BuildState(tmp_path)
    run.unified_log_path = tmp_path / "sysforge-update.log"
    return run


def _result(pkgbase, action="UP_TO_DATE", path=Path("/x/PKGBUILD")):
    return _UpdateResult(pkgbase=pkgbase, pkgnames=[pkgbase], action=action,
                         installed_ver="1-1", pkgbuild_ver="1-1", pkgbuild_path=path)


def test_body_is_the_ordered_phase_list():
    assert [p.__name__ for p in update._PHASES] == [
        "_phase_init", "_phase_assemble", "_phase_llvm_preflight",
        "_phase_source_sync", "_phase_version_check", "_phase_summary",
        "_phase_toolchain_drift", "_phase_flag_drift", "_phase_plan", "_phase_confirm",
        "_phase_build",
        "_phase_install_and_report",
    ]


def test_body_stops_at_the_first_phase_returning_an_exit_code(monkeypatch):
    seen = []

    def phase(rc):
        def _p(run):
            seen.append(rc)
            return rc
        return _p

    monkeypatch.setattr(update, "_PHASES", (phase(None), phase(3), phase(None)))
    assert update._cmd_update_body(Namespace()) == 3
    assert seen == [None, 3]


def test_summary_phase_resolves_drift_axes(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_print_summary", lambda *a, **k: None)
    monkeypatch.setattr(update, "load_sysforge_toml", lambda: {})
    run = _run(tmp_path, rebuild_on_drift=True)
    assert update._phase_summary(run) is None
    assert run._rebuild_tc_drift and run._rebuild_fl_drift


def test_toolchain_drift_phase_flags_a_variant_change(tmp_path):
    run = _run(tmp_path)
    run.bs.record("foo", "1", "1", "0", "foo", tmp_path, build_mode="source_built",
                  toolchain_variant="stock_llvm")
    run.active_variant = "pgo_llvm"
    run.results = [_result("foo"), _result("bar")]
    assert update._phase_toolchain_drift(run) is None
    assert [(pb, rv) for pb, rv, _ in run.drifted] == [("foo", "stock_llvm")]


def test_toolchain_drift_phase_is_silent_without_a_toolchain_stage(tmp_path):
    run = _run(tmp_path)
    run.active_variant = "system"
    run.results = [_result("foo")]
    update._phase_toolchain_drift(run)
    assert run.drifted == []


def test_plan_phase_dry_run_gate_ends_the_run(tmp_path):
    run = _run(tmp_path, dry_run=True)
    assert update._phase_plan(run) == 0


def test_plan_phase_promotes_toolchain_drift_when_opted_in(tmp_path):
    run = _run(tmp_path)
    run._rebuild_tc_drift = True
    run.results = [_result("foo"), _result("bar")]
    run.drifted = [("foo", "stock_llvm", "different variant")]
    assert update._phase_plan(run) is None
    foo, bar = run.results
    assert (foo.action, foo.force_rebuild) == ("NEEDS_REBUILD", True)
    assert bar.action == "UP_TO_DATE"
    assert run.promoted == {"foo": "toolchain drifted"}
    assert [r.pkgbase for r in run.to_build] == ["foo"]


def test_dry_run_lists_flag_drift_promotions(tmp_path):
    """3.5.0-B5: --dry-run used to return before promotions."""
    run = _run(tmp_path, dry_run=True)
    run._rebuild_fl_drift = True
    run.results = [_result("foo")]
    run.flag_drifted = [("foo", ["  CFLAGS: a → b"])]
    assert update._phase_plan(run) == 0
    assert run.results[0].action == "NEEDS_REBUILD"
    assert run.promoted == {"foo": "flags drifted"}


def test_build_phase_nothing_to_rebuild_ends_the_run(tmp_path, capsys):
    run = _run(tmp_path)
    run.results = [_result("foo")]
    assert update._phase_plan(run) is None
    assert update._phase_build(run) == 0
    assert "Nothing to rebuild." in capsys.readouterr().out


def test_failure_exit_code_counts_review_refusals():
    assert update._failure_exit_code(failed_pkgs=[], review_refused={"x": "r"}) == 1
    assert update._failure_exit_code(failed_pkgs=[]) == 0


def _plan_run(tmp_path, monkeypatch, *, tty=True, **args):
    run = _run(tmp_path, **args)
    run.plan = update.update_plan.Plan(
        posture="P", rows=(update.update_plan.PlanRow("rebuild", "foo", "1 → 2"),))
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("sys.stdout.isatty", lambda: tty)
    return run


def test_confirm_yes_continues(tmp_path, monkeypatch):
    run = _plan_run(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert update._phase_confirm(run) is None


def test_confirm_no_ends_with_nothing_changed(tmp_path, monkeypatch, capsys):
    run = _plan_run(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert update._phase_confirm(run) == 0
    assert "Nothing changed." in capsys.readouterr().out


def test_confirm_eof_is_no(tmp_path, monkeypatch):
    run = _plan_run(tmp_path, monkeypatch)
    def _eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert update._phase_confirm(run) == 0


def _never_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input",
                        lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))


def test_confirm_skipped_without_terminal(tmp_path, monkeypatch):
    run = _plan_run(tmp_path, monkeypatch, tty=False)
    _never_prompt(monkeypatch)
    assert update._phase_confirm(run) is None


def test_confirm_skipped_when_stdout_piped(tmp_path, monkeypatch):
    run = _plan_run(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    _never_prompt(monkeypatch)
    assert update._phase_confirm(run) is None


def test_confirm_skipped_by_yes_and_config(tmp_path, monkeypatch):
    _never_prompt(monkeypatch)
    assert update._phase_confirm(_plan_run(tmp_path, monkeypatch, yes=True)) is None
    run = _plan_run(tmp_path, monkeypatch)
    run.build_cfg = {"confirm_plan": False}
    assert update._phase_confirm(run) is None


def test_confirm_skipped_for_an_empty_plan(tmp_path, monkeypatch):
    run = _plan_run(tmp_path, monkeypatch)
    run.plan = update.update_plan.Plan(posture="P", rows=())
    _never_prompt(monkeypatch)
    assert update._phase_confirm(run) is None


def test_confirm_runs_before_the_build_phase():
    names = [p.__name__ for p in update._PHASES]
    assert names.index("_phase_confirm") == names.index("_phase_plan") + 1
    assert names.index("_phase_confirm") + 1 == names.index("_phase_build")


def test_source_sync_phase_picks_up_rpc_versions_recorded_this_run(tmp_path, monkeypatch):
    """3.5.0-B108: a split pkgbase first resolved by this run's RPC (through a
    member pkgname) is usable by the version check now, not one run later."""
    from types import SimpleNamespace
    run = _run(tmp_path)
    run.rpc_version_by_base = {"htop": "3.3-1"}
    cache = {"htop": {"rpc_version": "3.3-1"},
             "lib32-libdbusmenu": {"rpc_version": "16.04.0-6"}}
    monkeypatch.setattr(update, "_sync_sources", lambda *a: {})
    monkeypatch.setattr(update, "get_scheduler",
                        lambda: SimpleNamespace(cache=SimpleNamespace(all=lambda: cache)))
    assert update._phase_source_sync(run) is None
    assert run.rpc_version_by_base == {"htop": "3.3-1", "lib32-libdbusmenu": "16.04.0-6"}


# 3.5.0-B125: a package built on the other family on purpose records that
# family's variant; drift skips it only while the config still picks it.

@pytest.mark.parametrize("recorded, active, wanted_cc, drifts", [
    ("gcc", "pgo_llvm", "gcc", False),         # row keeps it on gcc
    ("gcc", "pgo_llvm", "clang", True),        # one-off --toolchain gcc
    ("stock_llvm", "gcc", "clang", False),     # row keeps it on llvm
    ("stock_llvm", "gcc", "gcc", True),        # one-off --toolchain llvm
    ("stock_llvm", "pgo_llvm", "clang", True),  # same family, new toolchain
    ("gcc", "pgo_llvm", None, True),           # PKGBUILD gone: report as before
])
def test_toolchain_drift_phase_honours_a_deliberate_other_family(
        tmp_path, monkeypatch, recorded, active, wanted_cc, drifts):
    monkeypatch.setattr(update, "load_conflict_groups", lambda: {})
    monkeypatch.setattr(update, "current_compiler", lambda *a: wanted_cc)
    run = _run(tmp_path)
    run.bs.record("foo", "1", "1", "0", "foo", tmp_path, build_mode="source_built",
                  toolchain_variant=recorded)
    run.active_variant = active
    run.results = [_result("foo")]
    update._phase_toolchain_drift(run)
    assert bool(run.drifted) is drifts
