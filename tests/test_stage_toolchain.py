"""
test_stage_toolchain.py — tests for the toolchain stage.

Mocks makepkg_wrapper.run() and subprocess so nothing real is built.
"""
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.pipeline.stages.toolchain import (
    ToolchainStage,
    _load_toolchain_config,
    _package_lists,
    _resolve_all_pkgbuilds,
    _extract_pass2_to_staging,
    _build_pass,
    _do_profraw_merge,
    _profraw_merge_daemon,
    _merge_profraw,
    _remove_staging,
    _DEFAULT_LLVM_PGO,
    _DEFAULT_LLVM_NON_PGO,
    _DEFAULT_LLVM_LIB32,
    _DEFAULT_GCC,
    _PGO_ALLOWED_MAKEPKG_FLAGS,
    _PROFRAW_MERGE_BATCH_MAX,
    _PROFRAW_MERGE_BATCH_MIN,
    _PROFRAW_SETTLE_SECS,
    _collect_pgo_packages,
    _has_llvm_cmake_config,
    _pgo_install,
    _pgo_pass1_install,
    _system_llvm_is_instrumented,
    _profile_runtime_ldflag,
    _validate_pgo_environment,
    _sudo_keepalive_daemon,
    _SUDO_KEEPALIVE_INTERVAL,
    _PGO_PROFDATA_MIN_BYTES,
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
    assert pgo == []
    assert non_pgo == _DEFAULT_GCC
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
    assert pgo == []
    assert non_pgo == ["gcc", "gcc-libs", "binutils"]


# ---------------------------------------------------------------------------
# _resolve_all_pkgbuilds
# ---------------------------------------------------------------------------

def test_resolve_all_pkgbuilds_finds_local(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    result = _resolve_all_pkgbuilds(["llvm", "clang"], config)
    assert "llvm" in result
    assert "clang" in result
    assert result["llvm"].name == "PKGBUILD"


def test_resolve_all_pkgbuilds_missing_raises(tmp_path):
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            _resolve_all_pkgbuilds(["nonexistent-pkg"], config)


def test_resolve_all_pkgbuilds_partial_miss_reports_all(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve"):
            _resolve_all_pkgbuilds(["llvm", "clang", "lld"], config)


def test_resolve_all_pkgbuilds_split_found_after_pass3_clone(tmp_path):
    """gcc-libs must be resolved via split-package scan after gcc is cloned in Pass 3,
    not attempted as a standalone pkgctl clone (which would require auth)."""
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    # gcc PKGBUILD declares both gcc and gcc-libs in pkgname
    gcc_dir = tmp_path / "gcc"
    gcc_dir.mkdir()
    (gcc_dir / "PKGBUILD").write_text(
        "pkgbase=gcc\npkgname=('gcc' 'gcc-libs')\npkgver=14.0\npkgrel=1\n"
    )

    def fake_pkgctl_checkout(name, dest, *, timeout=60):
        # Simulate pkgctl cloning gcc (copies our prepared dir)
        import shutil
        shutil.copytree(gcc_dir, dest)

    with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
         patch("sysforge.primitives.aur.pkgctl_checkout", side_effect=fake_pkgctl_checkout):
        result = _resolve_all_pkgbuilds(["gcc", "gcc-libs"], config)

    assert "gcc" in result
    assert "gcc-libs" in result
    # Both must resolve to the same PKGBUILD — not two separate clones
    assert result["gcc"].parent == result["gcc-libs"].parent


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
    with patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}):
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
    with patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("subprocess.run", return_value=fake_result) as mock_run:
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
    with patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("subprocess.run", return_value=fake_result):
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
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_GCC:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
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
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_LLVM_PGO + _DEFAULT_LLVM_NON_PGO + _DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
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
        f'enabled = true\ncompiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in _DEFAULT_LLVM_PGO + _DEFAULT_LLVM_NON_PGO + _DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"


def test_toolchain_stage_pgo_calls_makepkg_three_passes(tmp_path):
    """Verify makepkg_wrapper.run is called once per package per pass with correct PGO flags."""
    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False)

    call_log = []
    def fake_run(pkgbuild_path, options=None):
        call_log.append({
            "cc": options.cc_override if options else None,
            "flags": list(options.extra_flags or []) if options else [],
            "cfe": options.compiler_flags_extra if options else None,
        })
        # Simulate Pass 2: instrumented clang running as CC writes a profraw file
        if options and options.cc_override == "/usr/bin/clang":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / "default_0.profraw")

    # Fake .pkg.tar.zst for pass-2 staging extraction
    pkg_dir = pkgbuild_dir / "llvm"
    (pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst").touch()

    def fake_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        # Create the --output file so atomic rename in _do_profraw_merge succeeds
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.makepkg_run", side_effect=fake_run), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain._pgo_pass1_install"), \
         patch("sysforge.pipeline.stages.toolchain._pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain._run_llvm_preflight"), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    # 3 calls: pass1 (system CC), pass2 (clang CC), pass3 (staged CC)
    assert len(call_log) == 3
    assert call_log[0]["cc"] is None                        # pass 1: system
    assert call_log[1]["cc"] == "/usr/bin/clang"            # pass 2: pass-1 clang
    assert call_log[2]["cc"].endswith("/usr/bin/clang")     # pass 3: staged clang
    # All PGO passes force a clean build; none pass --install (install is via _pgo_install)
    assert "--cleanbuild" in call_log[0]["flags"]
    assert "--cleanbuild" in call_log[1]["flags"]
    assert "--cleanbuild" in call_log[2]["flags"]
    assert "--install" not in call_log[0]["flags"]
    assert "--install" not in call_log[1]["flags"]
    assert "--install" not in call_log[2]["flags"]
    # Pass 1 injects -fprofile-generate so the installed clang is instrumented
    assert call_log[0]["cfe"] is not None
    assert "-fprofile-generate=" in call_log[0]["cfe"]
    assert str(pgo_store) in call_log[0]["cfe"]
    # Pass 2 has no extra compiler flags; profraw is generated by the instrumented CC running
    assert call_log[1]["cfe"] is None
    # Pass 3 injects -fprofile-use (IR PGO, matches -fprofile-generate)
    assert call_log[2]["cfe"] is not None
    assert "-fprofile-use=" in call_log[2]["cfe"]
    assert "-fprofile-correction" not in call_log[2]["cfe"]


# ---------------------------------------------------------------------------
# Helpers shared across profraw tests
# ---------------------------------------------------------------------------

def _make_old_profraw(path: Path) -> Path:
    """Touch a profraw file and backdate its mtime by 30 seconds so it passes the settle filter."""
    path.touch()
    past = time.time() - 30
    os.utime(path, (past, past))
    return path


def fake_profdata_merge(cmd, **kwargs):
    """subprocess.run side_effect that creates the --output file."""
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    if cmd and "llvm-profdata" in cmd[0]:
        idx = cmd.index("--output")
        Path(cmd[idx + 1]).touch()
    return result


def fake_profdata_merge_fail(cmd, **kwargs):
    result = MagicMock()
    result.returncode = 1
    result.stderr = "error: bad input"
    return result


# ---------------------------------------------------------------------------
# _do_profraw_merge
# ---------------------------------------------------------------------------

def test_do_profraw_merge_merges_and_deletes(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    _make_old_profraw(tmp_path / "b.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        count, n_batches = _do_profraw_merge(tmp_path, "test")

    assert count == 2
    assert n_batches == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "llvm-profdata"
    assert "--output" in cmd
    assert str(tmp_path / "clang.profdata.tmp") in cmd
    assert not (tmp_path / "a.profraw").exists()
    assert not (tmp_path / "b.profraw").exists()
    assert (tmp_path / "clang.profdata").exists()


def test_do_profraw_merge_no_files_returns_zero(tmp_path):
    with patch("subprocess.run") as mock_run:
        count, n_batches = _do_profraw_merge(tmp_path, "test")
    assert count == 0
    assert n_batches == 0
    mock_run.assert_not_called()


def test_do_profraw_merge_includes_existing_profdata(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        _do_profraw_merge(tmp_path, "test")

    cmd = mock_run.call_args[0][0]
    assert str(tmp_path / "clang.profdata") in cmd


def test_do_profraw_merge_failure_returns_zero(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge_fail):
        count, n_batches = _do_profraw_merge(tmp_path, "test")

    assert count == 0
    assert n_batches == 0
    assert (tmp_path / "a.profraw").exists()   # not deleted on failure


def test_do_profraw_merge_batches_large_sets(tmp_path):
    """With N > _PROFRAW_MERGE_BATCH_MAX settled files, llvm-profdata is called
    in multiple batches rather than once with all files."""
    n = _PROFRAW_MERGE_BATCH_MAX + 3
    for i in range(n):
        _make_old_profraw(tmp_path / f"p{i}.profraw")

    call_counts = []
    def counting_merge(cmd, **kwargs):
        raws = [a for a in cmd if a.endswith(".profraw")]
        call_counts.append(len(raws))
        result = MagicMock()
        result.returncode = 0
        out_idx = cmd.index("--output")
        Path(cmd[out_idx + 1]).touch()
        return result

    with patch("subprocess.run", side_effect=counting_merge):
        count, n_batches = _do_profraw_merge(tmp_path, "test")

    assert count == n
    assert n_batches == 2
    assert len(call_counts) == 2
    assert call_counts[0] == _PROFRAW_MERGE_BATCH_MAX
    assert call_counts[1] == 3
    assert all(c <= _PROFRAW_MERGE_BATCH_MAX for c in call_counts)


def test_do_profraw_merge_adaptive_shrink_on_failure(tmp_path):
    """On merge failure, batch size halves and retries the same position."""
    for i in range(4):
        _make_old_profraw(tmp_path / f"p{i}.profraw")

    attempts = []
    call_n = [0]
    def fail_first_only(cmd, **kwargs):
        call_n[0] += 1
        raws = [a for a in cmd if a.endswith(".profraw")]
        attempts.append(len(raws))
        result = MagicMock()
        result.stderr = "OOM"
        result.returncode = 1 if call_n[0] == 1 else 0  # first call fails
        if result.returncode == 0:
            out_idx = cmd.index("--output")
            Path(cmd[out_idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain._PROFRAW_MERGE_BATCH_MAX", 4), \
         patch("sysforge.pipeline.stages.toolchain._PROFRAW_MERGE_BATCH_MIN", 1), \
         patch("subprocess.run", side_effect=fail_first_only):
        count, n_batches = _do_profraw_merge(tmp_path, "test")

    # First attempt: batch=4 (max) → fails
    # Second attempt: batch=2 → succeeds, then another batch=2 → succeeds
    assert attempts[0] == 4   # first try at max batch
    assert attempts[1] == 2   # retry at half
    assert count == 4
    assert n_batches == 2


def test_do_profraw_merge_gives_up_at_min_batch(tmp_path):
    """If merge keeps failing down to min batch size, returns partial count."""
    for i in range(_PROFRAW_MERGE_BATCH_MIN):
        _make_old_profraw(tmp_path / f"p{i}.profraw")

    def always_fail(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "OOM"
        return result

    with patch("subprocess.run", side_effect=always_fail):
        count, n_batches = _do_profraw_merge(tmp_path, "test")

    assert count == 0  # nothing merged, gives up at min batch
    assert n_batches == 0


# ---------------------------------------------------------------------------
# _profraw_merge_daemon
# ---------------------------------------------------------------------------

def test_profraw_merge_daemon_merges_on_wakeup(tmp_path):
    """Daemon merges profraw files when stop_event fires while raws exist."""
    _make_old_profraw(tmp_path / "a.profraw")
    stop_event = threading.Event()

    with patch("sysforge.pipeline.stages.toolchain._PGO_MERGE_INTERVAL", 0), \
         patch("subprocess.run", side_effect=fake_profdata_merge):
        t = threading.Thread(target=_profraw_merge_daemon,
                             args=(tmp_path, stop_event), daemon=True)
        t.start()
        t.join(timeout=2)
        stop_event.set()
        t.join(timeout=2)

    assert not (tmp_path / "a.profraw").exists()
    assert (tmp_path / "clang.profdata").exists()


def test_profraw_merge_daemon_stops_cleanly_with_no_files(tmp_path):
    """Daemon exits cleanly when stop_event fires with no profraw present."""
    stop_event = threading.Event()
    stop_event.set()  # fire immediately

    with patch("subprocess.run") as mock_run:
        t = threading.Thread(target=_profraw_merge_daemon,
                             args=(tmp_path, stop_event), daemon=True)
        t.start()
        t.join(timeout=2)

    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _collect_pgo_packages / _pgo_install
# ---------------------------------------------------------------------------

def test_collect_pgo_packages_uses_makepkg_packagelist(tmp_path):
    """_collect_pgo_packages calls 'makepkg --packagelist' and returns existing paths."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    def fake_packagelist(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = str(fake_pkg)
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_packagelist):
        pkgs = _collect_pgo_packages(pkgbuild_map)

    assert pkgs == [fake_pkg]


def test_collect_pgo_packages_deduplicates_by_dir(tmp_path):
    """Split packages sharing a PKGBUILD dir are only queried once."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    pkgbuild_map = {
        "llvm":      pkg_dir / "PKGBUILD",
        "llvm-libs": pkg_dir / "PKGBUILD",
    }

    call_count = []
    def fake_packagelist(cmd, **kwargs):
        call_count.append(1)
        result = MagicMock()
        result.returncode = 0
        result.stdout = str(fake_pkg)
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_packagelist):
        _collect_pgo_packages(pkgbuild_map)

    assert len(call_count) == 1


def test_collect_pgo_packages_excludes_missing_and_sig(tmp_path):
    """Non-existent paths and .sig files are filtered out."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    real_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    real_pkg.touch()
    missing = pkg_dir / "llvm-missing-1-x86_64.pkg.tar.zst"
    sig = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst.sig"
    sig.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    def fake_packagelist(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "\n".join([str(real_pkg), str(missing), str(sig)])
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_packagelist):
        pkgs = _collect_pgo_packages(pkgbuild_map)

    assert pkgs == [real_pkg]


def test_pgo_install_calls_pacman_u(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    pacman_calls = []
    def fake_run(cmd, **kwargs):
        pacman_calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = str(fake_pkg)
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        _pgo_install("test", pkgbuild_map, dry_run=False)

    assert any("pacman" in c and "-U" in c for c in pacman_calls)


def test_pgo_install_dry_run_skips_pacman(tmp_path):
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    with patch("subprocess.run") as mock_run:
        _pgo_install("test", pkgbuild_map, dry_run=True)
    mock_run.assert_not_called()


def test_pgo_install_raises_when_no_packages(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    def fake_packagelist(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_packagelist):
        with pytest.raises(RuntimeError, match="No built packages"):
            _pgo_install("test", pkgbuild_map, dry_run=False)


def test_pgo_install_raises_on_pacman_failure(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = str(fake_pkg)
        result.stderr = ""
        if "pacman" in cmd:
            result.returncode = 1
        return result

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="pacman -U failed"):
            _pgo_install("test", pkgbuild_map, dry_run=False)


# ---------------------------------------------------------------------------
# _has_llvm_cmake_config / _pgo_pass1_install
# ---------------------------------------------------------------------------


def test_has_llvm_cmake_config_true(tmp_path):
    """Returns True when tar listing contains cmake/llvm."""
    pkg = tmp_path / "llvm-18.pkg.tar.zst"
    pkg.touch()
    listing = (
        "./usr/lib/cmake/llvm/LLVMConfig.cmake\n"
        "./usr/lib/libLLVMSupport.a\n"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=listing, stderr="")
        assert _has_llvm_cmake_config(pkg) is True
    mock_run.assert_called_once()
    assert "tar" in mock_run.call_args[0][0]


def test_has_llvm_cmake_config_false(tmp_path):
    """Returns False for packages that have no cmake/llvm entries (e.g. llvm-libs)."""
    pkg = tmp_path / "llvm-libs-18.pkg.tar.zst"
    pkg.touch()
    listing = "./usr/lib/libLLVM-18.so.1\n./usr/lib/libLLVM-18.so\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=listing, stderr="")
        assert _has_llvm_cmake_config(pkg) is False


def test_pgo_pass1_install_excludes_cmake_pkg(tmp_path):
    """cmake-config package (llvm) is excluded; shared-lib package (llvm-libs) is installed."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    llvm_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    llvm_libs_pkg = pkg_dir / "llvm-libs-18.0.0-1-x86_64.pkg.tar.zst"
    llvm_pkg.touch()
    llvm_libs_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD", "llvm-libs": pkg_dir / "PKGBUILD"}

    cmake_listing = "./usr/lib/cmake/llvm/LLVMConfig.cmake\n./usr/lib/libLLVMSupport.a\n"
    libs_listing = "./usr/lib/libLLVM-18.so.1\n"

    pacman_args = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "makepkg" in cmd:
            result.stdout = f"{llvm_pkg}\n{llvm_libs_pkg}\n"
        elif "tar" in cmd and str(llvm_pkg) in cmd:
            result.stdout = cmake_listing
        elif "tar" in cmd and str(llvm_libs_pkg) in cmd:
            result.stdout = libs_listing
        elif "pacman" in cmd:
            pacman_args.extend(cmd)
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        _pgo_pass1_install(pkgbuild_map, dry_run=False)

    assert any("pacman" in str(a) for a in pacman_args)
    assert str(llvm_libs_pkg) in pacman_args
    assert str(llvm_pkg) not in pacman_args


def test_pgo_pass1_install_dry_run(tmp_path):
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    with patch("subprocess.run") as mock_run:
        _pgo_pass1_install(pkgbuild_map, dry_run=True)
    mock_run.assert_not_called()


def test_pgo_pass1_install_all_excluded_warns_and_returns(tmp_path):
    """When all packages have cmake/llvm, warn and return without calling pacman."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    llvm_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    llvm_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}
    cmake_listing = "./usr/lib/cmake/llvm/LLVMConfig.cmake\n"
    pacman_called = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "makepkg" in cmd:
            result.stdout = str(llvm_pkg)
        elif "tar" in cmd:
            result.stdout = cmake_listing
        else:
            if "pacman" in cmd:
                pacman_called.append(True)
            result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        _pgo_pass1_install(pkgbuild_map, dry_run=False)

    assert not pacman_called, "pacman should not be called when all packages are excluded"


def test_pgo_pass1_install_raises_when_no_packages(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="No built packages"):
            _pgo_pass1_install(pkgbuild_map, dry_run=False)


# ---------------------------------------------------------------------------
# _system_llvm_is_instrumented / _profile_runtime_ldflag
# ---------------------------------------------------------------------------


def test_system_llvm_is_instrumented_true(tmp_path):
    """Returns True when nm output contains __llvm_profile_ symbols."""
    fake_lib = tmp_path / "libLLVMSupport.a"
    fake_lib.touch()
    nm_output = "0000 T __llvm_profile_instrument_target\n0000 T __llvm_profile_instrument_memop\n"
    with patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=nm_output, stderr="")
        result = _system_llvm_is_instrumented()
    assert result is True


def test_system_llvm_is_instrumented_false(tmp_path):
    """Returns False when nm output has no profile symbols."""
    nm_output = "0000 T some_other_symbol\n"
    with patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=nm_output, stderr="")
        result = _system_llvm_is_instrumented()
    assert result is False


def test_system_llvm_is_instrumented_missing_lib():
    """Returns False when libLLVMSupport.a does not exist."""
    with patch("pathlib.Path.exists", return_value=False):
        result = _system_llvm_is_instrumented()
    assert result is False


def test_profile_runtime_ldflag_returns_flag(tmp_path):
    """Returns the -L/-l flag string when the profile runtime lib exists."""
    runtime_dir = tmp_path / "clang" / "lib" / "linux"
    runtime_dir.mkdir(parents=True)
    profile_lib = runtime_dir / "libclang_rt.profile-x86_64.a"
    profile_lib.touch()

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "--print-runtime-dir" in cmd:
            result.stdout = str(runtime_dir) + "\n"
        elif "uname" in cmd:
            result.stdout = "x86_64\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        flag = _profile_runtime_ldflag()

    assert flag is not None
    assert str(runtime_dir) in flag
    assert "clang_rt.profile-x86_64" in flag


def test_profile_runtime_ldflag_missing_lib_returns_none(tmp_path):
    """Returns None when the profile runtime lib does not exist."""
    runtime_dir = tmp_path / "clang" / "lib" / "linux"
    runtime_dir.mkdir(parents=True)
    # No .a file created

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "--print-runtime-dir" in cmd:
            result.stdout = str(runtime_dir) + "\n"
        elif "uname" in cmd:
            result.stdout = "x86_64\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        flag = _profile_runtime_ldflag()

    assert flag is None


# ---------------------------------------------------------------------------
# _sudo_keepalive_daemon
# ---------------------------------------------------------------------------

def test_sudo_keepalive_daemon_calls_sudo_v():
    """Daemon calls 'sudo -v' at least once before stop_event fires."""
    stop_event = threading.Event()
    sudo_calls = []

    def fake_run(cmd, **kwargs):
        sudo_calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    with patch("sysforge.pipeline.stages.toolchain._SUDO_KEEPALIVE_INTERVAL", 0), \
         patch("subprocess.run", side_effect=fake_run):
        t = threading.Thread(target=_sudo_keepalive_daemon, args=(stop_event,), daemon=True)
        t.start()
        t.join(timeout=1)
        stop_event.set()
        t.join(timeout=1)

    assert any(cmd == ["sudo", "-v"] for cmd in sudo_calls)


def test_sudo_keepalive_daemon_stops_immediately():
    """If stop_event is pre-set, daemon exits without calling sudo."""
    stop_event = threading.Event()
    stop_event.set()

    with patch("subprocess.run") as mock_run:
        t = threading.Thread(target=_sudo_keepalive_daemon, args=(stop_event,), daemon=True)
        t.start()
        t.join(timeout=2)

    mock_run.assert_not_called()


def test_sudo_keepalive_interval_under_sudoers_default():
    """Keepalive interval must be under 5 minutes (minimum reasonable sudoers timeout)."""
    assert _SUDO_KEEPALIVE_INTERVAL < 5 * 60


# ---------------------------------------------------------------------------
# _merge_profraw (final sweep)
# ---------------------------------------------------------------------------

def test_merge_profraw_merges_and_deletes_raws(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    _make_old_profraw(tmp_path / "b.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge):
        result = _merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    assert (tmp_path / "clang.profdata").exists()
    assert not (tmp_path / "a.profraw").exists()
    assert not (tmp_path / "b.profraw").exists()


def test_merge_profraw_no_files_no_profdata_raises(tmp_path):
    with pytest.raises(RuntimeError, match="No .profraw files and no profdata"):
        _merge_profraw(tmp_path, dry_run=False)


def test_merge_profraw_existing_profdata_no_raws_returns_it(tmp_path):
    """Daemon already merged everything — final sweep returns existing profdata."""
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run") as mock_run:
        result = _merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()


def test_merge_profraw_combines_raws_with_existing_profdata(tmp_path):
    """Final sweep merges remaining raws together with daemon's profdata."""
    _make_old_profraw(tmp_path / "late.profraw")
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        result = _merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    cmd = mock_run.call_args[0][0]
    # Both the existing profdata and the remaining profraw were inputs
    assert str(tmp_path / "clang.profdata") in cmd
    assert str(tmp_path / "late.profraw") in cmd


def test_merge_profraw_dry_run_skips_everything(tmp_path):
    with patch("subprocess.run") as mock_run:
        result = _merge_profraw(tmp_path, dry_run=True)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()


def test_merge_profraw_fresh_raws_with_profdata_warns_and_returns(tmp_path):
    """Fresh profraw (< _PROFRAW_SETTLE_SECS old) + existing profdata → warn, return profdata."""
    # Fresh profraw: use default touch() mtime (now)
    (tmp_path / "fresh.profraw").touch()
    (tmp_path / "clang.profdata").touch()

    # _do_profraw_merge returns 0 because all profraw is too fresh to merge;
    # _merge_profraw should warn and return the existing profdata rather than raising
    with patch("subprocess.run") as mock_run:
        result = _merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()  # llvm-profdata not invoked


def test_merge_profraw_fresh_raws_no_profdata_raises(tmp_path):
    """Fresh profraw + no profdata → error (no usable data at all)."""
    (tmp_path / "fresh.profraw").touch()

    with patch("subprocess.run"):
        with pytest.raises(RuntimeError, match="too fresh"):
            _merge_profraw(tmp_path, dry_run=False)


def test_settle_secs_constant_is_positive():
    assert _PROFRAW_SETTLE_SECS > 0


# ---------------------------------------------------------------------------
# PGO makepkg flag whitelist
# ---------------------------------------------------------------------------

def test_pgo_allowed_flags_whitelist():
    assert "-f" in _PGO_ALLOWED_MAKEPKG_FLAGS
    assert "--force" in _PGO_ALLOWED_MAKEPKG_FLAGS


def test_build_pass_pgo_drops_disallowed_flags(tmp_path):
    """pgo_build=True: only -f/--force pass through from user makepkg_flags."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    pkgbuild_map = {"llvm": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append(list(options.extra_flags or []) if options else [])

    # Simulate user passing -m '-f --noextract --noprepare'
    options = make_options(dry_run=False,
                           makepkg_flags=["-f", "--noextract", "--noprepare"])

    with patch("sysforge.pipeline.stages.toolchain.makepkg_run", side_effect=fake_run):
        _build_pass("test pass", pkgbuild_map, options,
                    install=False, pgo_build=True)

    # Only -f should survive; --noextract and --noprepare dropped
    flags = captured[0]
    assert "-f" in flags
    assert "--noextract" not in flags
    assert "--noprepare" not in flags


def test_build_pass_non_pgo_passes_all_flags(tmp_path):
    """pgo_build=False (default): all user flags pass through unchanged."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    pkgbuild_map = {"llvm": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append(list(options.extra_flags or []) if options else [])

    options = make_options(dry_run=False,
                           makepkg_flags=["-f", "--noextract"])

    with patch("sysforge.pipeline.stages.toolchain.makepkg_run", side_effect=fake_run):
        _build_pass("test pass", pkgbuild_map, options, install=False)

    flags = captured[0]
    assert "-f" in flags
    assert "--noextract" in flags


# ---------------------------------------------------------------------------
# ToolchainStage.run() — custom package list
# ---------------------------------------------------------------------------

def test_toolchain_stage_custom_packages(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        'enabled = true\ncompiler = "llvm"\npgo = false\n'
        '[packages]\npgo = ["llvm", "clang"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")
    make_pkgbuild(pkgbuild_dir, "clang")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"


# ---------------------------------------------------------------------------
# ToolchainStage.run() — skip_build
# ---------------------------------------------------------------------------

def test_toolchain_skip_build_gcc(tmp_path):
    """skip_build=true registers gcc paths in state without building anything."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\nskip_build = true\n')
    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"
    assert "ld" not in result


def test_toolchain_skip_build_llvm(tmp_path):
    """skip_build=true registers clang paths in state without building anything."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\nskip_build = true\n')
    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"


# ---------------------------------------------------------------------------
# ToolchainStage.run() — PKGBUILD resolution error
# ---------------------------------------------------------------------------

def test_toolchain_stage_missing_pkgbuild_raises(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options()

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            ToolchainStage().run(config, state, options)


# ---------------------------------------------------------------------------
# PGO flow integration helpers
#
# These helpers back tests that validate the real failure modes we've hit:
#   - ccache/sccache bypassing the instrumented compiler in Pass 2
#   - residual instrumented system LLVM static libs breaking Pass 2 or Pass 3
#   - non_pgo packages absent from the Pass 2 training run
#   - silently inadequate profdata going undetected
#
# Each test drives ToolchainStage().run() end-to-end with makepkg and
# subprocess mocked, so no real builds execute but the full control-flow
# (including env dict assembly and linker flag injection) runs for real.
# ---------------------------------------------------------------------------


def _pgo_setup(tmp_path, pgo_pkgs, non_pgo_pkgs=None, lib32_pkgs=None):
    """
    Prepare filesystem and objects for a full PGO ToolchainStage run.

    Returns (toml_path, pkgbuild_dir, staging, pgo_store, state, config, options).
    Each package in every list gets a PKGBUILD directory and a fake .pkg.tar.zst
    so staging extraction does not error.
    """
    import json as _json
    non_pgo_pkgs = non_pgo_pkgs or []
    lib32_pkgs   = lib32_pkgs   or []

    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        f"[packages]\n"
        f"pgo = {_json.dumps(pgo_pkgs)}\n"
        f"non_pgo = {_json.dumps(non_pgo_pkgs)}\n"
        f"lib32 = {_json.dumps(lib32_pkgs)}\n"
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in pgo_pkgs + non_pgo_pkgs + lib32_pkgs:
        pb = make_pkgbuild(pkgbuild_dir, name)
        (pb.parent / f"{name}-18.0.0-1-x86_64.pkg.tar.zst").touch()

    state   = PipelineState(tmp_path / "state")
    config  = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False)
    return toml_path, pkgbuild_dir, staging, pgo_store, state, config, options


def _pgo_fake_run_factory(pgo_store, call_log):
    """
    Return a fake makepkg_wrapper.run() that records every invocation and
    writes a settled profraw file when Pass 2 runs (identified by CCACHE_DISABLE
    in extra_env, which is only injected during the training pass).
    """
    def fake_run(pkgbuild_path, options=None):
        env = dict(options.extra_env or {}) if options else {}
        call_log.append({
            "cc":      options.cc_override if options else None,
            "pkgbuild": str(pkgbuild_path),
            "cfe":     options.compiler_flags_extra if options else None,
            "lfe":     options.linker_flags_extra if options else None,
            "env":     env,
        })
        # Pass 2 is the training run: it injects CCACHE_DISABLE into extra_env.
        if env.get("CCACHE_DISABLE") == "1":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / f"p{len(call_log)}.profraw")
    return fake_run


def _fake_subprocess_factory(profdata_size=100 * 1024 * 1024):
    """
    Return a subprocess.run side_effect that handles llvm-profdata by writing
    profdata_size bytes to the --output path, and returns success for everything else.
    """
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr     = ""
        result.stdout     = ""
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).write_bytes(b"\x00" * profdata_size)
        return result
    return fake_run


def _run_pgo(tmp_path, pgo_pkgs, non_pgo_pkgs=None, lib32_pkgs=None,
             instrumented=False, runtime_flag="-L/fake -lclang_rt.profile-x86_64",
             profdata_size=100 * 1024 * 1024):
    """
    Run a full PGO ToolchainStage with standard mocking.  Returns call_log.
    instrumented=True simulates a prior aborted Pass 1 leaving the system
    LLVM static libs in an instrumented state.
    """
    toml_path, pkgbuild_dir, staging, pgo_store, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs, non_pgo_pkgs, lib32_pkgs)

    call_log = []
    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.makepkg_run",
               side_effect=_pgo_fake_run_factory(pgo_store, call_log)), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain._pgo_pass1_install"), \
         patch("sysforge.pipeline.stages.toolchain._pgo_install"), \
         patch("subprocess.run", side_effect=_fake_subprocess_factory(profdata_size)), \
         patch("sys.stdin.isatty", return_value=False), \
         patch("sysforge.pipeline.stages.toolchain._validate_pgo_environment"), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=instrumented), \
         patch("sysforge.pipeline.stages.toolchain._profile_runtime_ldflag",
               return_value=runtime_flag):
        ToolchainStage().run(config, state, options)

    return call_log


def _pass1(call_log):
    return [c for c in call_log if "-fprofile-generate" in (c["cfe"] or "")]


def _pass2(call_log):
    return [c for c in call_log if c["env"].get("CCACHE_DISABLE") == "1"]


def _pass3(call_log):
    return [c for c in call_log if "-fprofile-use" in (c["cfe"] or "")]


# ---------------------------------------------------------------------------
# CCACHE_DISABLE / SCCACHE_DISABLE in Pass 2
# ---------------------------------------------------------------------------


def test_pgo_pass2_disables_ccache_and_sccache(tmp_path):
    """
    Pass 2 must inject CCACHE_DISABLE=1 and SCCACHE_DISABLE=1 into the build
    environment.  If either cache tool intercepts a compilation it bypasses the
    instrumented compiler entirely, producing no profraw data and silently
    degrading the PGO profile.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"])
    p2 = _pass2(call_log)
    assert p2, "Pass 2 must have run"
    for call in p2:
        assert call["env"].get("CCACHE_DISABLE") == "1", \
            "CCACHE_DISABLE=1 missing from Pass 2 env"
        assert call["env"].get("SCCACHE_DISABLE") == "1", \
            "SCCACHE_DISABLE=1 missing from Pass 2 env"


def test_pgo_pass1_and_pass3_do_not_disable_cache_tools(tmp_path):
    """
    CCACHE/SCCACHE_DISABLE must only be set in Pass 2 (the training run).
    Passes 1 and 3 use distinct compiler flags (-fprofile-generate /
    -fprofile-use) that already produce cache misses naturally; disabling
    cache tools there would throw away legitimate cache benefit on reruns.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"])
    for call in _pass1(call_log) + _pass3(call_log):
        assert "CCACHE_DISABLE" not in call["env"], \
            f"CCACHE_DISABLE must not be set in Pass {1 if call['cc'] is None else 3}"
        assert "SCCACHE_DISABLE" not in call["env"], \
            f"SCCACHE_DISABLE must not be set in Pass {1 if call['cc'] is None else 3}"


# ---------------------------------------------------------------------------
# Pass 2 training coverage: non_pgo packages included
# ---------------------------------------------------------------------------


def test_pgo_pass2_includes_non_pgo_packages(tmp_path):
    """
    Pass 2 must build pgo + non_pgo packages (not just pgo) so the training
    run exercises additional clang code paths: OpenMP pragmas, compiler-rt
    intrinsics, Polly polyhedral analysis.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"])

    p2_pkgbuilds = {c["pkgbuild"] for c in _pass2(call_log)}
    # Both pgo and non_pgo packages must appear in Pass 2
    assert any("llvm" in pb          for pb in p2_pkgbuilds), "pgo pkg missing from Pass 2"
    assert any("compiler-rt" in pb   for pb in p2_pkgbuilds), "non_pgo pkg missing from Pass 2"


def test_pgo_pass1_does_not_include_non_pgo_packages(tmp_path):
    """
    Pass 1 builds only pgo packages with -fprofile-generate; non_pgo packages
    must not be included there (they don't need instrumentation, and building
    them against the instrumented static libs would fail at link time).
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"])

    p1_pkgbuilds = {c["pkgbuild"] for c in _pass1(call_log)}
    assert not any("compiler-rt" in pb for pb in p1_pkgbuilds), \
        "non_pgo package must not be built in Pass 1"


def test_pgo_pass3_includes_non_pgo_and_lib32(tmp_path):
    """Pass 3 must build all package groups: pgo, non_pgo, and lib32."""
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                        non_pgo_pkgs=["compiler-rt"], lib32_pkgs=["lib32-llvm"])

    p3_pkgbuilds = {c["pkgbuild"] for c in _pass3(call_log)}
    assert any("llvm" in pb          for pb in p3_pkgbuilds)
    assert any("compiler-rt" in pb   for pb in p3_pkgbuilds)
    assert any("lib32-llvm" in pb    for pb in p3_pkgbuilds)


# ---------------------------------------------------------------------------
# Residual instrumented LLVM static libs → linker flag injection
# ---------------------------------------------------------------------------


def test_pgo_residual_instrumented_llvm_injects_runtime_into_pass2_and_pass3(tmp_path):
    """
    When system LLVM static libs are still instrumented (left by a prior aborted
    Pass 1 install), packages that call find_package(LLVM) in Pass 2 or Pass 3
    link against them and fail with undefined __llvm_profile_* symbols unless the
    profile runtime is added to LDFLAGS.

    Regression test: the linker flag was previously injected into Pass 2 only.
    Pass 3 was missing it, causing clang-tblgen link failures in production.
    """
    fake_rt_flag = "-L/usr/lib/clang/18/lib/linux -lclang_rt.profile-x86_64"
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                        instrumented=True, runtime_flag=fake_rt_flag)

    p2 = _pass2(call_log)
    p3 = _pass3(call_log)
    assert p2, "Pass 2 must have run"
    assert p3, "Pass 3 must have run"

    for call in p2:
        assert call["lfe"] == fake_rt_flag, \
            "Profile runtime must be injected into Pass 2 LDFLAGS when system LLVM is instrumented"
    for call in p3:
        assert call["lfe"] == fake_rt_flag, \
            "Profile runtime must be injected into Pass 3 LDFLAGS when system LLVM is instrumented " \
            "(regression: clang-tblgen undefined __llvm_profile_* link failure)"


def test_pgo_uninstrumented_llvm_no_linker_flags(tmp_path):
    """When system LLVM is clean, no profile runtime linker flag is injected."""
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"], instrumented=False)

    for call in call_log:
        assert call["lfe"] is None, \
            f"No linker flags expected when LLVM is uninstrumented (cc={call['cc']})"


def test_pgo_instrumented_llvm_pass1_not_affected(tmp_path):
    """
    Pass 1 runs before the instrumented clang is even installed; the residual
    linker flag guard is checked after Pass 1 completes, so Pass 1 itself must
    never receive a linker_flags_extra injection.
    """
    fake_rt_flag = "-L/fake -lclang_rt.profile-x86_64"
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                        instrumented=True, runtime_flag=fake_rt_flag)

    for call in _pass1(call_log):
        assert call["lfe"] is None, \
            "Pass 1 must not receive the residual-instrumentation linker flag"


# ---------------------------------------------------------------------------
# Profdata size quality check
# ---------------------------------------------------------------------------


def test_pgo_warns_when_profdata_suspiciously_small(tmp_path):
    """
    A warn is emitted when merged profdata is smaller than _PGO_PROFDATA_MIN_BYTES.
    This is the canary for cache bypass: if ccache/sccache slipped through, clang
    never ran, no profraw was generated, and the profdata will be tiny or empty.
    """
    warn_calls = []
    with patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                 profdata_size=_PGO_PROFDATA_MIN_BYTES - 1)

    assert any("unexpectedly small" in str(a) for a in warn_calls), \
        "Expected a warning about suspiciously small profdata"


def test_pgo_no_size_warning_when_profdata_adequate(tmp_path):
    """No profdata size warning when profdata is large enough to represent real training."""
    warn_calls = []
    with patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                 profdata_size=_PGO_PROFDATA_MIN_BYTES + 1)

    assert not any("unexpectedly small" in str(a) for a in warn_calls), \
        "Unexpected profdata size warning for adequate profdata"


# ---------------------------------------------------------------------------
# Staging directory clean-up on run start
# ---------------------------------------------------------------------------


def test_pgo_stale_staging_purged_at_run_start(tmp_path):
    """
    A staging directory left by a prior failed run must be purged at the
    start of the next run, not accumulated on top of.  Stale Pass 2 binaries
    from an aborted build could otherwise shadow freshly extracted ones.
    """
    toml_path, pkgbuild_dir, staging, pgo_store, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs=["llvm"])

    # Simulate a stale staging dir left by a prior aborted Pass 2
    staging.mkdir(parents=True)
    stale_marker = staging / "stale_sentinel"
    stale_marker.touch()

    call_log = []
    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.makepkg_run",
               side_effect=_pgo_fake_run_factory(pgo_store, call_log)), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain._pgo_pass1_install"), \
         patch("sysforge.pipeline.stages.toolchain._pgo_install"), \
         patch("subprocess.run", side_effect=_fake_subprocess_factory()), \
         patch("sys.stdin.isatty", return_value=False), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=False):
        ToolchainStage().run(config, state, options)

    assert not stale_marker.exists(), \
        "Stale staging sentinel must be removed before Pass 1 starts"


# ---------------------------------------------------------------------------
# _validate_pgo_environment — pre-flight checks
# ---------------------------------------------------------------------------


def test_validate_pgo_environment_dry_run_skips_all_checks():
    """dry_run=True must skip all checks — no subprocess calls."""
    with patch("subprocess.run") as mock_run:
        _validate_pgo_environment(dry_run=True)
    mock_run.assert_not_called()


def test_validate_pgo_environment_raises_if_clang_missing(tmp_path):
    """Raises RuntimeError immediately when /usr/bin/clang does not exist."""
    with patch("pathlib.Path.exists", return_value=False), \
         patch("shutil.which", return_value="/usr/bin/lld"):
        with pytest.raises(RuntimeError, match="clang not found"):
            _validate_pgo_environment(dry_run=False)


def test_validate_pgo_environment_raises_if_clang_broken():
    """Raises RuntimeError when clang compile probe fails (e.g. symbol lookup error
    in libclang-cpp.so due to mismatched packages from prior aborted PGO runs).
    Note: --version is intentionally not used because it doesn't load libclang-cpp.so."""
    broken_stderr = (
        "/usr/bin/clang: symbol lookup error: /usr/lib/libclang-cpp.so.22.1: "
        "undefined symbol: _ZNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEE"
        "9_M_assignERKS4_, version LLVM_22.1"
    )

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if "/dev/null" in cmd:  # compile probe
            result.returncode = 127
            result.stdout = ""
            result.stderr = broken_stderr
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="not functional"):
            _validate_pgo_environment(dry_run=False)


def test_validate_pgo_environment_raises_if_clang_exits_nonzero():
    """Raises RuntimeError when clang compile probe exits non-zero for any reason."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1 if "/dev/null" in cmd else 0  # compile probe fails
        result.stdout = ""
        result.stderr = "some internal error"
        return result

    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="not functional"):
            _validate_pgo_environment(dry_run=False)


def test_validate_pgo_environment_raises_if_lld_missing():
    """Raises RuntimeError when lld cannot be found on PATH."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "clang version 22.1.1"
        result.stderr = ""
        return result

    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value=None), \
         patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="lld not found"):
            _validate_pgo_environment(dry_run=False)


def _fake_healthy_clang(_cmd, **_kwargs):
    """subprocess.run side_effect: clang compile probe succeeds, everything else no-ops."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result


def test_validate_pgo_environment_clean_logs_info():
    """Logs a clean-environment info message when no instrumentation is detected."""
    info_calls = []
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=False), \
         patch("sysforge.log.info", side_effect=lambda *a: info_calls.append(a)):
        _validate_pgo_environment(dry_run=False)

    assert any("clean" in str(a) for a in info_calls), \
        "Expected 'clean' confirmation in info log"


def test_validate_pgo_environment_instrumented_shared_lib_warns_then_prompts(tmp_path):
    """When libLLVM-*.so is instrumented and stdin is a TTY, emits a warning
    then prompts the user.  Answering 'y' allows the build to continue."""
    fake_so = tmp_path / "libLLVM-22.so"
    fake_so.touch()

    warn_calls = []

    def fake_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        if "readelf" in cmd:
            # Simulate instrumented shared lib with __llvm_prf_* sections
            result.stdout = "  [11] __llvm_prf_names  PROGBITS\n  [12] __llvm_prf_cnts  PROGBITS\n"
            result.stderr = ""
        else:
            result.stdout = ""
            result.stderr = ""
        return result

    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[fake_so]), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=False), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _validate_pgo_environment(dry_run=False)   # must not raise

    assert warn_calls, "Expected a warning for instrumented shared lib"
    assert any("libLLVM-22.so" in str(a) for a in warn_calls)


def test_validate_pgo_environment_instrumented_static_libs_warns_then_prompts():
    """When libLLVMSupport.a is instrumented and stdin is a TTY, emits a warning
    then prompts.  Answering 'y' allows the build to continue."""
    warn_calls = []

    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _validate_pgo_environment(dry_run=False)   # must not raise

    assert warn_calls
    assert any("libLLVMSupport.a" in str(a) for a in warn_calls)


def test_validate_pgo_environment_instrumented_tty_decline_raises():
    """User declines the dirty-env prompt → RuntimeError before any build starts."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="n"):
        with pytest.raises(RuntimeError, match="Aborted"):
            _validate_pgo_environment(dry_run=False)


def test_validate_pgo_environment_instrumented_non_tty_raises():
    """In non-interactive mode, residual instrumentation is a hard failure —
    an unattended build must not silently proceed with a degraded environment."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain._system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Aborting unattended"):
            _validate_pgo_environment(dry_run=False)


def test_validate_pgo_environment_runs_before_pass1(tmp_path):
    """
    Pre-flight validation must fire before Pass 1 starts.
    If it raises (e.g. missing lld), no makepkg_run calls should occur.
    """
    toml_path, _, _, _, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs=["llvm"])

    makepkg_calls = []

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.makepkg_run",
               side_effect=lambda *a, **_: makepkg_calls.append(a)), \
         patch("sysforge.pipeline.stages.toolchain._validate_pgo_environment",
               side_effect=RuntimeError("lld not found")):
        with pytest.raises(RuntimeError, match="lld not found"):
            ToolchainStage().run(config, state, options)

    assert not makepkg_calls, "No builds should run when pre-flight check fails"
