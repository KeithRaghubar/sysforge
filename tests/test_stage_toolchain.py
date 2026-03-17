"""
test_stage_toolchain.py — tests for the toolchain stage.

Mocks makepkg_wrapper.run() and subprocess so nothing real is built.
"""
import sys
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from sysforge.pipeline.stages.toolchain import (
    ToolchainStage,
    _load_toolchain_config,
    _package_lists,
    _resolve_all_pkgbuilds,
    _show_resolution_table,
    _extract_pass2_to_staging,
    _build_pass,
    _remove_staging,
    TOOLCHAIN_PATH,
    _DEFAULT_LLVM_PGO,
    _DEFAULT_LLVM_NON_PGO,
    _DEFAULT_LLVM_LIB32,
    _DEFAULT_GCC,
    _DEFAULT_STAGING,
)
from sysforge.pipeline.state import PipelineState
from sysforge.pipeline.stages.base import RunOptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_options(**kwargs):
    opts = RunOptions(no_pkg_logs=True)
    for k, v in kwargs.items():
        setattr(opts, k, v)
    return opts


def write_toolchain_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def make_pkgbuild(pkgbuild_dir: Path, name: str) -> Path:
    d = pkgbuild_dir / name
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")
    return pb


# ---------------------------------------------------------------------------
# _load_toolchain_config
# ---------------------------------------------------------------------------

def test_load_toolchain_config_absent_returns_none(tmp_path):
    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH",
               tmp_path / "nonexistent.toml"):
        result = _load_toolchain_config()
    assert result is None


def test_load_toolchain_config_reads_toml(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text('compiler = "llvm"\npgo = false\n')
    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", p):
        result = _load_toolchain_config()
    assert result == {"compiler": "llvm", "pgo": False}


def test_load_toolchain_config_bad_toml_raises(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text("compiler = [[[bad toml")
    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", p):
        with pytest.raises(RuntimeError, match="Failed to parse"):
            _load_toolchain_config()


# ---------------------------------------------------------------------------
# _package_lists
# ---------------------------------------------------------------------------

def test_package_lists_llvm_defaults():
    pgo, non_pgo, lib32 = _package_lists({"compiler": "llvm", "pgo": True})
    assert pgo == _DEFAULT_LLVM_PGO
    assert non_pgo == _DEFAULT_LLVM_NON_PGO
    assert lib32 == _DEFAULT_LLVM_LIB32


def test_package_lists_gcc():
    pgo, non_pgo, lib32 = _package_lists({"compiler": "gcc"})
    assert pgo == _DEFAULT_GCC
    assert non_pgo == []
    assert lib32 == []


def test_package_lists_custom_override():
    tcfg = {
        "compiler": "llvm",
        "pgo": True,
        "packages": {
            "pgo": ["llvm", "clang"],
            "non_pgo": ["compiler-rt"],
            "lib32": [],
        },
    }
    pgo, non_pgo, lib32 = _package_lists(tcfg)
    assert pgo == ["llvm", "clang"]
    assert non_pgo == ["compiler-rt"]
    assert lib32 == []


def test_package_lists_gcc_custom():
    tcfg = {"compiler": "gcc", "packages": {"non_pgo": ["gcc", "gcc-libs", "binutils"]}}
    pgo, non_pgo, lib32 = _package_lists(tcfg)
    assert pgo == ["gcc", "gcc-libs", "binutils"]


# ---------------------------------------------------------------------------
# _resolve_all_pkgbuilds
# ---------------------------------------------------------------------------

def test_resolve_all_pkgbuilds_finds_local(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    config = {"paths": {"pkgbuild_dir": str(tmp_path)}}
    result = _resolve_all_pkgbuilds(["llvm", "clang"], config)
    assert "llvm" in result
    assert "clang" in result
    assert result["llvm"].name == "PKGBUILD"


def test_resolve_all_pkgbuilds_missing_raises(tmp_path):
    config = {"paths": {"pkgbuild_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            _resolve_all_pkgbuilds(["nonexistent-pkg"], config)


def test_resolve_all_pkgbuilds_partial_miss_reports_all(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve"):
            _resolve_all_pkgbuilds(["llvm", "clang", "lld"], config)


# ---------------------------------------------------------------------------
# _extract_pass2_to_staging
# ---------------------------------------------------------------------------

def test_extract_pass2_dry_run_skips(tmp_path):
    staging = tmp_path / "staging"
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    # dry_run=True: should not touch filesystem
    _extract_pass2_to_staging(pkgbuild_map, staging, dry_run=True)
    assert not staging.exists()


def test_extract_pass2_no_pkg_raises(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").touch()
    staging = tmp_path / "staging"
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}
    with pytest.raises(RuntimeError, match="No .pkg.tar"):
        _extract_pass2_to_staging(pkgbuild_map, staging, dry_run=False)


def test_extract_pass2_calls_tar(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    (pkg_dir / "PKGBUILD").touch()
    staging = tmp_path / "staging"
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    fake_result = MagicMock()
    fake_result.returncode = 0
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        _extract_pass2_to_staging(pkgbuild_map, staging, dry_run=False)

    assert mock_run.called
    cmd = mock_run.call_args[0][0]
    assert "tar" in cmd
    assert str(fake_pkg) in cmd
    assert str(staging) in cmd


def test_extract_pass2_tar_failure_raises(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}
    staging = tmp_path / "staging"

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = b"extraction failed"
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(RuntimeError, match="tar extraction failed"):
            _extract_pass2_to_staging(pkgbuild_map, staging, dry_run=False)


# ---------------------------------------------------------------------------
# _remove_staging
# ---------------------------------------------------------------------------

def test_remove_staging_removes_dir(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "file").touch()
    _remove_staging(staging)
    assert not staging.exists()


def test_remove_staging_no_dir_noop(tmp_path):
    staging = tmp_path / "nonexistent"
    _remove_staging(staging)  # should not raise


# ---------------------------------------------------------------------------
# PipelineState.set_stage_result / get_stage_result
# ---------------------------------------------------------------------------

def test_state_set_get_result(tmp_path):
    state = PipelineState(tmp_path)
    state.set_stage_result("toolchain", {"cc": "/usr/bin/clang", "cxx": "/usr/bin/clang++", "ld": "lld"})
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


# ---------------------------------------------------------------------------
# ToolchainStage.run() — no-op when toolchain.toml absent
# ---------------------------------------------------------------------------

def test_toolchain_stage_noop_when_absent(tmp_path):
    state = PipelineState(tmp_path / "state")
    options = make_options()
    config = {}

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH",
               tmp_path / "nonexistent.toml"):
        ToolchainStage().run(config, state, options)

    # No result written
    assert state.get_stage_result("toolchain") == {}


# ---------------------------------------------------------------------------
# ToolchainStage.run() — GCC single pass
# ---------------------------------------------------------------------------

def test_toolchain_stage_gcc_dry_run(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('compiler = "gcc"\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_GCC:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"
    assert "ld" not in result


# ---------------------------------------------------------------------------
# ToolchainStage.run() — LLVM single pass (pgo=false)
# ---------------------------------------------------------------------------

def test_toolchain_stage_llvm_no_pgo_dry_run(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('compiler = "llvm"\npgo = false\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_LLVM_PGO + _DEFAULT_LLVM_NON_PGO + _DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"


# ---------------------------------------------------------------------------
# ToolchainStage.run() — LLVM PGO 3-pass
# ---------------------------------------------------------------------------

def test_toolchain_stage_llvm_pgo_dry_run(tmp_path):
    staging = tmp_path / "staging"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'compiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_LLVM_PGO + _DEFAULT_LLVM_NON_PGO + _DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"


def test_toolchain_stage_pgo_calls_makepkg_three_passes(tmp_path):
    """Verify makepkg_wrapper.run is called once per package per pass."""
    staging = tmp_path / "staging"
    toml_path = tmp_path / "toolchain.toml"
    # Use a minimal 1-package set to keep the count predictable
    toml_path.write_text(
        f'compiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False)

    call_log = []
    def fake_run(pkgbuild_path, extra_flags=None, interactive=False,
                 pkg_log=True, persist_log=False, log_dir=None, profile_conf=None,
                 cc_override=None, cxx_override=None, ld_override=None,
                 cache_report=False, init_session=True, update=True):
        call_log.append({"cc": cc_override, "flags": list(extra_flags or [])})

    fake_tar = MagicMock()
    fake_tar.returncode = 0

    # Create a fake .pkg.tar.zst so pass 2 staging extraction works
    pkg_dir = pkgbuild_dir / "llvm"
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.makepkg_run", side_effect=fake_run), \
         patch("subprocess.run", return_value=fake_tar), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    # 3 calls: pass1 (system CC), pass2 (clang CC), pass3 (staged CC)
    assert len(call_log) == 3
    assert call_log[0]["cc"] is None                        # pass 1: system
    assert call_log[1]["cc"] == "/usr/bin/clang"            # pass 2: pass-1 clang
    assert call_log[2]["cc"].endswith("/usr/bin/clang")     # pass 3: staged clang
    # Pass 1 and 3 install; pass 2 does not
    assert "--install" in call_log[0]["flags"]
    assert "--install" not in call_log[1]["flags"]
    assert "--install" in call_log[2]["flags"]


# ---------------------------------------------------------------------------
# ToolchainStage.run() — custom package list
# ---------------------------------------------------------------------------

def test_toolchain_stage_custom_packages(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        'compiler = "llvm"\npgo = false\n'
        '[packages]\npgo = ["llvm", "clang"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")
    make_pkgbuild(pkgbuild_dir, "clang")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"


# ---------------------------------------------------------------------------
# ToolchainStage.run() — PKGBUILD resolution error
# ---------------------------------------------------------------------------

def test_toolchain_stage_missing_pkgbuild_raises(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('compiler = "gcc"\n')

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_dir": str(tmp_path / "empty")}}
    options = make_options()

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            ToolchainStage().run(config, state, options)
