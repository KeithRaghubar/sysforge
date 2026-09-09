# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/gates.py — Gate 1/2, soname consumers, snapshot rollback.
"""
from sysforge.pipeline.stages.toolchain.gates import gate_soname_consumers
from sysforge.pipeline.stages.toolchain.gates import rebuild_soname_consumers
from sysforge.pipeline.state import PipelineState
import pytest

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    toolchain_cfg,
    _gate_map,
    _impact,
    _patch_rebuild,
    _toolchain_gates_clean,
    make_options,
)


def test_gate_soname_no_impact_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: None,
    )
    opts = make_options(dry_run=False)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == []

def test_gate_soname_dry_run_previews_no_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=True)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == []

def test_gate_soname_off_mode_warns_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=False)
    tcfg = toolchain_cfg(rebuild_soname_consumers="off")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == []

def test_gate_soname_auto_mode_returns_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(("mesa", "julia")),
    )
    opts = make_options(dry_run=False)
    tcfg = toolchain_cfg(rebuild_soname_consumers="auto")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == ["mesa", "julia"]

def test_gate_soname_prompt_non_tty_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: False)
    opts = make_options(dry_run=False)
    with pytest.raises(RuntimeError, match="non-interactive"):
        gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg())

def test_gate_soname_prompt_approve_returns_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: True)
    monkeypatch.setattr(
        "sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "y"
    )
    opts = make_options(dry_run=False)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == ["mesa"]

def test_gate_soname_prompt_decline_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: True)
    monkeypatch.setattr(
        "sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "n"
    )
    opts = make_options(dry_run=False)
    with pytest.raises(RuntimeError, match="not approved"):
        gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg())

def test_gate_soname_cli_flag_overrides_config(tmp_path, monkeypatch):
    # CLI --rebuild-soname-consumers=off beats toolchain.toml auto.
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=False, rebuild_soname_consumers="off")
    tcfg = toolchain_cfg(rebuild_soname_consumers="auto")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == []

def test_gate_soname_exclude_passed_to_assessor(tmp_path, monkeypatch):
    captured = {}

    def fake_assess(ver, *, exclude):
        captured["exclude"] = exclude
        return None

    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact", fake_assess
    )
    opts = make_options(dry_run=False)
    gate_soname_consumers(_gate_map(tmp_path), ["llvm", "lib32-llvm"], opts, toolchain_cfg())
    # LLVM lockstep suite + in-scope build names are all excluded.
    from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE
    assert set(LLVM_LOCKSTEP_SUITE) <= captured["exclude"]
    assert {"llvm", "lib32-llvm"} <= captured["exclude"]

def test_rebuild_consumers_success(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=["mesa"])
    _patch_rebuild(monkeypatch, out)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    # No raise == success.
    rebuild_soname_consumers(["mesa"], {}, opts, state)

def test_rebuild_consumers_build_failure_raises_with_manual_cmd(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=[], failed_pkgs=["mesa"])
    _patch_rebuild(monkeypatch, out)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="sysforge build mesa"):
        rebuild_soname_consumers(["mesa"], {}, opts, state)

def test_rebuild_consumers_all_unresolved_raises(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=[])
    _patch_rebuild(monkeypatch, out, resolve_fail=("mesa",))
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="could be resolved for rebuild"):
        rebuild_soname_consumers(["mesa"], {}, opts, state)
