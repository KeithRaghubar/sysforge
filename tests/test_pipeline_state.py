"""
test_pipeline_state.py — unit tests for PipelineState read/write.

Uses a temporary directory for all state file operations.

The stage-result section at the end covers ``[stages.toolchain.result]`` and the
two canonical accessors over it. Those tests used to live in the toolchain
stage's test file; they moved here with the class in 3.2.0-F12, since what they
actually exercise is this file's format rather than anything the stage does.
"""
import tomllib

import pytest

from sysforge.primitives.pipeline_state import PipelineState, resolve_state_dir


@pytest.fixture
def state(tmp_path):
    return PipelineState(tmp_path)


# ---------------------------------------------------------------------------
# Init / save / load roundtrip
# ---------------------------------------------------------------------------

def test_fresh_state_no_file(tmp_path):
    s = PipelineState(tmp_path)
    assert not s.path.exists()

def test_save_creates_file(state, tmp_path):
    state.init_meta()
    state.save()
    assert state.path.exists()

def test_roundtrip_stage_status(state, tmp_path):
    state.mark_running("packages")
    state.save()
    s2 = PipelineState(tmp_path)
    assert s2.stage_status("packages") == "running"

def test_roundtrip_package_progress(state, tmp_path):
    state.init_package_list(["llvm", "mesa", "htop"])
    state.mark_package_built("llvm")
    state.mark_package_failed("mesa", "build exploded")
    state.save()
    s2 = PipelineState(tmp_path)
    p = s2.get_package_progress()
    assert "llvm" in p["built"]
    assert "mesa" in p["failed"]
    assert "htop" in p["remaining"]

def test_state_file_is_valid_toml(state):
    state.init_meta()
    state.mark_done("partition")
    state.save()
    with open(state.path, "rb") as f:
        data = tomllib.load(f)
    assert data["stages"]["partition"]["status"] == "done"


# ---------------------------------------------------------------------------
# Stage status transitions
# ---------------------------------------------------------------------------

def test_default_status_is_pending(state):
    assert state.stage_status("packages") == "pending"

def test_mark_running(state):
    state.mark_running("packages")
    assert state.stage_status("packages") == "running"
    assert "started_at" in state._data["stages"]["packages"]

def test_mark_done(state):
    state.mark_done("packages")
    assert state.stage_status("packages") == "done"
    assert "completed_at" in state._data["stages"]["packages"]

def test_mark_failed(state):
    state.mark_failed("packages", "something broke")
    assert state.stage_status("packages") == "failed"
    assert state._data["stages"]["packages"]["error"] == "something broke"

def test_mark_skipped_to(state):
    state.mark_skipped_to("partition")
    assert state.stage_status("partition") == "skipped_to"


# ---------------------------------------------------------------------------
# Package progress
# ---------------------------------------------------------------------------

def test_init_package_list(state):
    state.init_package_list(["a", "b", "c"])
    p = state.get_package_progress()
    assert p["remaining"] == ["a", "b", "c"]
    assert p["built"] == []
    assert p["failed"] == []

def test_init_package_list_idempotent(state):
    state.init_package_list(["a", "b"])
    state.init_package_list(["a", "b"])  # second call should not reset
    state.mark_package_built("a")
    state.init_package_list(["a", "b"])  # still should not reset
    p = state.get_package_progress()
    assert "a" in p["built"]

def test_mark_package_built(state):
    state.init_package_list(["llvm", "mesa"])
    state.mark_package_built("llvm")
    p = state.get_package_progress()
    assert "llvm" in p["built"]
    assert "llvm" not in p["remaining"]

def test_mark_package_failed(state):
    state.init_package_list(["llvm", "mesa"])
    state.mark_package_failed("llvm", "exit code 1")
    p = state.get_package_progress()
    assert "llvm" in p["failed"]
    assert "llvm" not in p["remaining"]
    assert state.get_package_errors()["llvm"] == "exit code 1"

def test_mark_package_skipped(state):
    state.init_package_list(["llvm", "mesa"])
    state.mark_package_failed("llvm", "error")
    state.mark_package_skipped("llvm")
    p = state.get_package_progress()
    assert "llvm" in p["skipped"]
    assert "llvm" not in p["failed"]

def test_mark_package_built_clears_failed(state):
    state.init_package_list(["llvm"])
    state.mark_package_failed("llvm", "err")
    state.mark_package_built("llvm")  # retry succeeded
    p = state.get_package_progress()
    assert "llvm" in p["built"]
    assert "llvm" not in p["failed"]

def test_get_package_errors_empty(state):
    assert state.get_package_errors() == {}


def test_package_errors_survive_roundtrip(tmp_path):
    """Errors written by mark_package_failed must survive a save/reload cycle."""
    state = PipelineState(tmp_path)
    state.init_package_list(["llvm", "mesa-git"])
    state.mark_package_building("llvm")
    state.mark_package_failed("llvm", "makepkg exit 1: configure failed")
    state.mark_package_building("mesa-git")
    state.mark_package_failed("mesa-git", 'soname mismatch: libLLVM.so.19 not found')
    state.save()

    reloaded = PipelineState(tmp_path)
    errors = reloaded.get_package_errors()
    assert errors["llvm"] == "makepkg exit 1: configure failed"
    assert errors["mesa-git"] == "soname mismatch: libLLVM.so.19 not found"


# ---------------------------------------------------------------------------
# resolve_state_dir re-export (3.2.0-F1a)
# ---------------------------------------------------------------------------

def test_resolve_state_dir_is_the_paths_primitive():
    """The name still imports from here, but the implementation lives in the
    leaf layer — behaviour tests moved to tests/test_paths.py."""
    assert resolve_state_dir.__module__ == "sysforge.primitives.paths"


# ---------------------------------------------------------------------------
# Stage result: [stages.<name>.result] and the toolchain accessors over it
# ---------------------------------------------------------------------------

def test_state_set_get_result(tmp_path):
    state = PipelineState(tmp_path)
    state.set_stage_result(
        "toolchain", {"cc": "/usr/bin/clang", "cxx": "/usr/bin/clang++", "ld": "lld"})
    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"

def test_state_get_result_missing_returns_empty(tmp_path):
    state = PipelineState(tmp_path)
    assert state.get_stage_result("toolchain") == {}

def test_state_result_serialized_to_toml(tmp_path):
    state = PipelineState(tmp_path)
    state.mark_running("toolchain")
    state.set_stage_result("toolchain", {"cc": "/usr/bin/clang", "ld": "lld"})
    state.save()

    text = (tmp_path / "pipeline_state.toml").read_text()
    assert "[stages.toolchain.result]" in text
    assert 'cc = "/usr/bin/clang"' in text
    assert 'ld = "lld"' in text

def test_state_result_round_trips(tmp_path):
    state = PipelineState(tmp_path)
    state.mark_done("toolchain")
    state.set_stage_result("toolchain", {"cc": "/usr/bin/gcc", "cxx": "/usr/bin/g++"})
    state.save()

    state2 = PipelineState(tmp_path)
    result = state2.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"

def test_get_toolchain_fingerprint_none_when_system(tmp_path):
    from sysforge.pipeline.state import get_toolchain_fingerprint
    state = PipelineState(tmp_path)  # toolchain stage never ran → "system"
    assert get_toolchain_fingerprint(state) is None

def test_get_toolchain_fingerprint_uses_active_cc_and_method(tmp_path, monkeypatch):
    from sysforge.pipeline import state as state_mod
    from sysforge.primitives import build_fingerprint, config
    state = PipelineState(tmp_path)
    state.set_stage_result("toolchain", {"cc": "/opt/clang", "variant": "pgo_llvm"})

    monkeypatch.setattr(config, "resolve_drift_detect", lambda: "content_hash")
    monkeypatch.setattr(
        build_fingerprint, "toolchain_fingerprint",
        lambda method, cc: f"{method}:{cc}",
    )
    assert state_mod.get_toolchain_fingerprint(state) == "content_hash:/opt/clang"

def test_get_toolchain_variant_helper(tmp_path):
    """get_toolchain_variant returns the canonical variant or 'system' fallback."""
    from sysforge.pipeline.state import get_toolchain_variant

    state = PipelineState(tmp_path)
    assert get_toolchain_variant(state) == "system"  # no result yet

    state.set_stage_result("toolchain", {"cc": "/usr/bin/gcc", "variant": "gcc"})
    assert get_toolchain_variant(state) == "gcc"

    state.set_stage_result("toolchain", {"cc": "/usr/bin/clang", "variant": "pgo_llvm"})
    assert get_toolchain_variant(state) == "pgo_llvm"
