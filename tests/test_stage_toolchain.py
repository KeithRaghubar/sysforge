"""
test_stage_toolchain.py — tests for the toolchain stage.

Mocks makepkg_wrapper.run() and subprocess so nothing real is built.
"""
import sys
import tempfile
import threading
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
    _do_profraw_merge,
    _profraw_merge_daemon,
    _merge_profraw,
    _remove_staging,
    TOOLCHAIN_PATH,
    _DEFAULT_LLVM_PGO,
    _DEFAULT_LLVM_NON_PGO,
    _DEFAULT_LLVM_LIB32,
    _DEFAULT_GCC,
    _DEFAULT_STAGING,
    _DEFAULT_PGO_STORE,
    _PGO_ALLOWED_MAKEPKG_FLAGS,
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


def test_resolve_all_pkgbuilds_split_found_after_pass3_clone(tmp_path):
    """gcc-libs must be resolved via split-package scan after gcc is cloned in Pass 3,
    not attempted as a standalone pkgctl clone (which would require auth)."""
    config = {"paths": {"pkgbuild_dir": str(tmp_path)}}

    # gcc PKGBUILD declares both gcc and gcc-libs in pkgname
    gcc_dir = tmp_path / "gcc"
    gcc_dir.mkdir()
    (gcc_dir / "PKGBUILD").write_text(
        "pkgbase=gcc\npkgname=('gcc' 'gcc-libs')\npkgver=14.0\npkgrel=1\n"
    )

    def fake_pkgctl_checkout(name, dest):
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
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')

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
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

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
        f'enabled = true\ncompiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
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
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False)

    call_log = []
    def fake_run(pkgbuild_path, extra_flags=None, interactive=False,
                 pkg_log=True, persist_log=False, log_dir=None, profile_conf=None,
                 cc_override=None, cxx_override=None, ld_override=None,
                 cache_report=False, init_session=True, update=True,
                 compiler_flags_extra=None, strip_full_lto=False,
                 profile_override=None, state_dir=None):
        call_log.append({
            "cc": cc_override,
            "flags": list(extra_flags or []),
            "cfe": compiler_flags_extra,
        })
        # Simulate Pass 2: instrumented clang running as CC writes a profraw file
        if cc_override == "/usr/bin/clang":
            pgo_store.mkdir(parents=True, exist_ok=True)
            (pgo_store / "default_0.profraw").touch()

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
         patch("subprocess.run", side_effect=fake_subprocess), \
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
    # Pass 1 injects -fprofile-generate so the installed clang is instrumented
    assert call_log[0]["cfe"] is not None
    assert "-fprofile-generate=" in call_log[0]["cfe"]
    assert str(pgo_store) in call_log[0]["cfe"]
    # Pass 2 has no extra compiler flags; profraw is generated by the instrumented CC running
    assert call_log[1]["cfe"] is None
    # Pass 3 injects -fprofile-use (IR PGO, matches -fprofile-generate) + -fprofile-correction
    assert call_log[2]["cfe"] is not None
    assert "-fprofile-use=" in call_log[2]["cfe"]
    assert "-fprofile-correction" in call_log[2]["cfe"]


# ---------------------------------------------------------------------------
# Helpers shared across profraw tests
# ---------------------------------------------------------------------------

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
    (tmp_path / "a.profraw").touch()
    (tmp_path / "b.profraw").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        count = _do_profraw_merge(tmp_path, "test")

    assert count == 2
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "llvm-profdata"
    assert "--output" in cmd
    assert str(tmp_path / "clang.profdata.tmp") in cmd
    assert not (tmp_path / "a.profraw").exists()
    assert not (tmp_path / "b.profraw").exists()
    assert (tmp_path / "clang.profdata").exists()


def test_do_profraw_merge_no_files_returns_zero(tmp_path):
    with patch("subprocess.run") as mock_run:
        count = _do_profraw_merge(tmp_path, "test")
    assert count == 0
    mock_run.assert_not_called()


def test_do_profraw_merge_includes_existing_profdata(tmp_path):
    (tmp_path / "a.profraw").touch()
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        _do_profraw_merge(tmp_path, "test")

    cmd = mock_run.call_args[0][0]
    assert str(tmp_path / "clang.profdata") in cmd


def test_do_profraw_merge_failure_returns_zero(tmp_path):
    (tmp_path / "a.profraw").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge_fail):
        count = _do_profraw_merge(tmp_path, "test")

    assert count == 0
    assert (tmp_path / "a.profraw").exists()   # not deleted on failure


# ---------------------------------------------------------------------------
# _profraw_merge_daemon
# ---------------------------------------------------------------------------

def test_profraw_merge_daemon_merges_on_wakeup(tmp_path):
    """Daemon merges profraw files when stop_event fires while raws exist."""
    (tmp_path / "a.profraw").touch()
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
# _merge_profraw (final sweep)
# ---------------------------------------------------------------------------

def test_merge_profraw_merges_and_deletes_raws(tmp_path):
    (tmp_path / "a.profraw").touch()
    (tmp_path / "b.profraw").touch()

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
    (tmp_path / "late.profraw").touch()
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
    def fake_run(pb, extra_flags=None, compiler_flags_extra=None, **kwargs):
        captured.append(list(extra_flags or []))

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
    def fake_run(pb, extra_flags=None, compiler_flags_extra=None, **kwargs):
        captured.append(list(extra_flags or []))

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
    config = {"paths": {"pkgbuild_dir": str(pkgbuild_dir)}}
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
    config = {"paths": {"pkgbuild_dir": str(tmp_path / "empty")}}
    options = make_options()

    with patch("sysforge.pipeline.stages.toolchain.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            ToolchainStage().run(config, state, options)
