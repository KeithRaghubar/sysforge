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
        "_phase_toolchain_drift", "_phase_flag_drift", "_phase_build",
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


def test_flag_drift_phase_dry_run_gate_ends_the_run(tmp_path):
    run = _run(tmp_path, dry_run=True)
    assert update._phase_flag_drift(run) == 0


def test_flag_drift_phase_promotes_toolchain_drift_when_opted_in(tmp_path):
    run = _run(tmp_path)
    run._rebuild_tc_drift = True
    run.results = [_result("foo"), _result("bar")]
    run.drifted = [("foo", "stock_llvm", "different variant")]
    assert update._phase_flag_drift(run) is None
    foo, bar = run.results
    assert (foo.action, foo.force_rebuild) == ("NEEDS_REBUILD", True)
    assert bar.action == "UP_TO_DATE"


def test_build_phase_nothing_to_rebuild_ends_the_run(tmp_path, capsys):
    run = _run(tmp_path)
    run.results = [_result("foo")]
    assert update._phase_build(run) == 0
    assert "Nothing to rebuild." in capsys.readouterr().out
