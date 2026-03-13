"""
test_pipeline_state.py — unit tests for PipelineState read/write.

Uses a temporary directory for all state file operations.
"""
import tempfile
import tomllib
from pathlib import Path

import pytest

from sysforge.pipeline.state import PipelineState, resolve_state_dir


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


# ---------------------------------------------------------------------------
# resolve_state_dir
# ---------------------------------------------------------------------------

def test_resolve_default(monkeypatch):
    monkeypatch.delenv("SYSFORGE_STATE_DIR", raising=False)
    path, source = resolve_state_dir()
    assert source == "default"
    assert str(path) == "/var/lib/sysforge"

def test_resolve_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path))
    path, source = resolve_state_dir()
    assert source == "SYSFORGE_STATE_DIR"
    assert path == tmp_path

def test_resolve_cli_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SYSFORGE_STATE_DIR", "/some/env/path")
    path, source = resolve_state_dir(cli_override=str(tmp_path))
    assert source == "--state-dir"
    assert path == tmp_path
