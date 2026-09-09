# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/profdata.py — staging, profraw merging, profile validation.
"""
from pathlib import Path
from sysforge.pipeline.stages.toolchain.constants import PROFRAW_MERGE_BATCH_MAX
from sysforge.pipeline.stages.toolchain.constants import PROFRAW_MERGE_BATCH_MIN
from sysforge.pipeline.stages.toolchain.profdata import assert_pass_links_shipped_libllvm
from sysforge.pipeline.stages.toolchain.profdata import assert_staging_has_llvm_cmake
from sysforge.pipeline.stages.toolchain.profdata import collect_pgo_packages
from sysforge.pipeline.stages.toolchain.profdata import do_profraw_merge
from sysforge.pipeline.stages.toolchain.profdata import extract_built_to_staging
from sysforge.pipeline.stages.toolchain.profdata import merge_profraw
from sysforge.pipeline.stages.toolchain.profdata import pgo_install
from sysforge.pipeline.stages.toolchain.profdata import pgo_stage_instrumented
from sysforge.pipeline.stages.toolchain.profdata import profile_runtime_ldflag
from sysforge.pipeline.stages.toolchain.profdata import profraw_merge_daemon
from sysforge.pipeline.stages.toolchain.profdata import remove_staging
from sysforge.pipeline.stages.toolchain.profdata import system_llvm_is_instrumented
from sysforge.pipeline.stages.toolchain.profdata import validate_pgo_environment
from sysforge.pipeline.stages.toolchain.stage import ToolchainStage
from unittest.mock import MagicMock
from unittest.mock import patch
import os
import pytest
import threading

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _fake_healthy_clang,
    _fake_subprocess_factory,
    _g2_opts,
    _make_old_profraw,
    _pgo_fake_run_factory,
    _pgo_setup,
    _run_pgo,
    _toolchain_gates_clean,
    fake_profdata_merge,
    fake_profdata_merge_fail,
    make_pkgbuild,
)


def test_extract_built_dry_run_skips(tmp_path):
    staging = tmp_path / "staging"
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    # dry_run=True: should not touch filesystem
    extract_built_to_staging(pkgbuild_map, staging, dry_run=True)
    assert not staging.exists()

def test_extract_built_no_pkg_raises(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").touch()
    staging = tmp_path / "staging"
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}
    with patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}):
        with pytest.raises(RuntimeError, match="No .pkg.tar"):
            extract_built_to_staging(pkgbuild_map, staging, dry_run=False)

def test_extract_built_calls_tar(tmp_path):
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
        extract_built_to_staging(pkgbuild_map, staging, dry_run=False)

    assert mock_run.called
    cmd = mock_run.call_args[0][0]
    assert "tar" in cmd
    assert str(fake_pkg) in cmd
    assert str(staging) in cmd

def test_extract_built_tar_failure_raises(tmp_path):
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
            extract_built_to_staging(pkgbuild_map, staging, dry_run=False)

def test_extract_built_split_pkgname_does_not_swallow_sibling(tmp_path):
    """Regression: name="llvm" must NOT match the split sibling "llvm-libs-…".

    The pgo_map for the split package has two keys (``llvm`` + ``llvm-libs``)
    pointing at the same PKGBUILD. A plain ``f"{name}-*"`` glob matches
    ``llvm-libs-…`` for name="llvm", and the mtime tiebreak then staged
    llvm-libs for BOTH keys — so the ``llvm`` (dev) package carrying
    LLVMConfig.cmake/headers never reached staging3 and Pass 4b's
    find_package(LLVM) silently fell back to the live /usr libLLVM (the Gate-3
    brick). The glob is version-anchored to prevent this; llvm-libs is made the
    newer artifact here so the old code would mis-select it.
    """
    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    pkg_llvm = pkgdest / "llvm-22.1.6-1-x86_64.pkg.tar"
    pkg_libs = pkgdest / "llvm-libs-22.1.6-1-x86_64.pkg.tar"
    pkg_llvm.touch()
    pkg_libs.touch()
    # Make llvm-libs strictly newer so a non-anchored glob's max(mtime) would
    # pick it for the "llvm" key — the exact failure being guarded against.
    os.utime(pkg_llvm, (1000, 1000))
    os.utime(pkg_libs, (2000, 2000))

    pb = tmp_path / "llvm" / "PKGBUILD"
    pb.parent.mkdir()
    pb.touch()
    # Same PKGBUILD for both split keys, mirroring the real pgo_map.
    pkgbuild_map = {"llvm": pb, "llvm-libs": pb}
    staging = tmp_path / "staging"

    extracted: list[str] = []

    def _fake_run(cmd, *a, **k):
        # Capture the .pkg.tar* path handed to tar for each extraction.
        for tok in cmd:
            if isinstance(tok, str) and ".pkg.tar" in tok:
                extracted.append(Path(tok).name)
        res = MagicMock()
        res.returncode = 0
        return res

    with patch("sysforge.primitives.config.parse_system_makepkg_conf",
               return_value={"PKGDEST": str(pkgdest)}), \
         patch("subprocess.run", side_effect=_fake_run):
        extract_built_to_staging(pkgbuild_map, staging, dry_run=False)

    # Each key must stage its own artifact: llvm → llvm-, llvm-libs → llvm-libs-.
    assert pkg_llvm.name in extracted, (
        f"the 'llvm' package was never staged (got {extracted}) — "
        "staging3 would lack LLVMConfig.cmake/headers"
    )
    assert pkg_libs.name in extracted
    assert extracted.count(pkg_libs.name) == 1, (
        f"llvm-libs staged more than once (got {extracted}) — "
        "the 'llvm' key swallowed the sibling artifact"
    )

def test_remove_staging_removes_dir(tmp_path):
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "file").touch()
    remove_staging(staging)
    assert not staging.exists()

def test_remove_staging_no_dir_noop(tmp_path):
    staging = tmp_path / "nonexistent"
    remove_staging(staging)  # should not raise

def test_do_profraw_merge_merges_and_deletes(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    _make_old_profraw(tmp_path / "b.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        count, n_batches = do_profraw_merge(tmp_path, "test")

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
        count, n_batches = do_profraw_merge(tmp_path, "test")
    assert count == 0
    assert n_batches == 0
    mock_run.assert_not_called()

def test_do_profraw_merge_includes_existing_profdata(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        do_profraw_merge(tmp_path, "test")

    cmd = mock_run.call_args[0][0]
    assert str(tmp_path / "clang.profdata") in cmd

def test_do_profraw_merge_failure_returns_zero(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge_fail):
        count, n_batches = do_profraw_merge(tmp_path, "test")

    assert count == 0
    assert n_batches == 0
    assert (tmp_path / "a.profraw").exists()   # not deleted on failure

def test_do_profraw_merge_batches_large_sets(tmp_path):
    """With N > PROFRAW_MERGE_BATCH_MAX settled files, llvm-profdata is called
    in multiple batches rather than once with all files."""
    n = PROFRAW_MERGE_BATCH_MAX + 3
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
        count, n_batches = do_profraw_merge(tmp_path, "test")

    assert count == n
    assert n_batches == 2
    assert len(call_counts) == 2
    assert call_counts[0] == PROFRAW_MERGE_BATCH_MAX
    assert call_counts[1] == 3
    assert all(c <= PROFRAW_MERGE_BATCH_MAX for c in call_counts)

def test_do_profraw_merge_gives_up_at_min_batch(tmp_path):
    """If merge keeps failing down to min batch size, returns partial count."""
    for i in range(PROFRAW_MERGE_BATCH_MIN):
        _make_old_profraw(tmp_path / f"p{i}.profraw")

    def always_fail(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "OOM"
        return result

    with patch("subprocess.run", side_effect=always_fail):
        count, n_batches = do_profraw_merge(tmp_path, "test")

    assert count == 0  # nothing merged, gives up at min batch
    assert n_batches == 0

def test_profraw_merge_daemon_stops_cleanly_with_no_files(tmp_path):
    """Daemon exits cleanly when stop_event fires with no profraw present."""
    stop_event = threading.Event()
    stop_event.set()  # fire immediately

    with patch("subprocess.run") as mock_run:
        t = threading.Thread(target=profraw_merge_daemon,
                             args=(tmp_path, stop_event), daemon=True)
        t.start()
        t.join(timeout=2)

    mock_run.assert_not_called()

def test_collect_pgo_packages_uses_makepkg_packagelist(tmp_path):
    """collect_pgo_packages calls 'makepkg --packagelist' and returns existing paths."""
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
        pkgs = collect_pgo_packages(pkgbuild_map)

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
        collect_pgo_packages(pkgbuild_map)

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
        pkgs = collect_pgo_packages(pkgbuild_map)

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
        pgo_install("test", pkgbuild_map, dry_run=False)

    assert any("pacman" in c and "-U" in c for c in pacman_calls)

def test_pgo_install_dry_run_skips_pacman(tmp_path):
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    with patch("subprocess.run") as mock_run:
        pgo_install("test", pkgbuild_map, dry_run=True)
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
            pgo_install("test", pkgbuild_map, dry_run=False)

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
            pgo_install("test", pkgbuild_map, dry_run=False)

def test_gate2_audit_refuses_hazardous_build(tmp_path, monkeypatch):
    """When scan_abi_hazards finds a leak, gate2_audit aborts before install.

    The ABI-hazard scan moved out of pgo_install into Gate 2, which runs
    *outside* the sentinel — so a hazardous build raises with nothing installed
    and no sentinel left behind. (Replaces the old in-install hazard check.)
    """
    from sysforge.pipeline.stages.toolchain import gate2_audit
    from sysforge.primitives import toolchain_safety as _ts

    pkg_dir = tmp_path / "clang"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "clang-22.1.5-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    built_map = {"clang": pkg_dir / "PKGBUILD"}

    hazard = [_ts.ToolchainFinding(
        "error", "abi_hazard",
        f"{fake_pkg.name}: libclang-cpp.so.22.1: _M_assign@LLVM_22.1",
        is_brick=True,
    )]
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: hazard)
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [fake_pkg],
    )

    with pytest.raises(RuntimeError, match="refusing to install"):
        gate2_audit(built_map, [], _g2_opts(), {}, dry_run=False)

def test_gate2_audit_clean_build_passes(tmp_path, monkeypatch):
    """No hazard → gate2_audit returns without raising."""
    from sysforge.pipeline.stages.toolchain import gate2_audit

    pkg_dir = tmp_path / "clang"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "clang-22.1.5-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [fake_pkg],
    )
    # scan_abi_hazards stubbed to [] by the autouse fixture.
    assert gate2_audit(
        {"clang": pkg_dir / "PKGBUILD"}, [], _g2_opts(), {}, dry_run=False,
    ) == []  # must not raise

def test_gate2_audit_refuses_graphics_consumer_brick(tmp_path, monkeypatch):
    """A freshly-built libLLVM that drops a target an installed mesa consumer
    imports aborts Gate 2 before install (outside the sentinel) — the
    bricked-desktop class. scan_abi_hazards is clean; the consumer check trips."""
    from sysforge.pipeline.stages.toolchain import gate2_audit
    from sysforge.primitives import toolchain_safety as _ts

    pkg_dir = tmp_path / "llvm-libs"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-libs-22.1.6-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [fake_pkg],
    )
    # scan_abi_hazards clean (autouse) — the consumer check is what aborts.
    brick = [_ts.ToolchainFinding(
        "error", "libllvm_consumer_symbols",
        "libgallium-26.so links libLLVM.so.22.1 but the freshly-built libLLVM "
        "does not export 6 symbol(s) — dropped LLVM target(s): AMDGPU",
        "Rebuild with AMDGPU kept.",
        is_brick=True,
    )]
    monkeypatch.setattr(_ts, "check_system_consumer_symbols", lambda pkgs: brick)

    with pytest.raises(RuntimeError, match="graphics consumer"):
        gate2_audit(
            {"llvm-libs": pkg_dir / "PKGBUILD"}, [], _g2_opts(), {}, dry_run=False,
        )

def test_gate2_audit_heals_stddrift_consumer(tmp_path, monkeypatch):
    """A healable std:: re-export drift does NOT abort: under mode=auto, Gate 2
    returns the libLLVM consumers to rebuild after Gate 3 (same machinery as a
    soname bump), and nothing is raised."""
    from sysforge.pipeline.stages.toolchain import gate2_audit
    from sysforge.primitives import toolchain_safety as _ts

    pkg_dir = tmp_path / "llvm-libs"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-libs-22.1.6-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [fake_pkg],
    )
    drift = [_ts.ToolchainFinding(
        "error", "libllvm_consumer_symbols",
        "libgallium-26.so links libLLVM.so.22.1 but the freshly-built libLLVM "
        "no longer re-exports 4 libstdc++ symbol(s)",
        "Rebuild the affected libLLVM consumer(s).",
        is_brick=True, healable=True,
    )]
    monkeypatch.setattr(_ts, "check_system_consumer_symbols", lambda pkgs: drift)
    monkeypatch.setattr(_ts, "libllvm_abi_consumers", lambda *, exclude: ["mesa"])

    out = gate2_audit(
        {"llvm-libs": pkg_dir / "PKGBUILD"}, [], _g2_opts("auto"), {},
        dry_run=False,
    )
    assert out == ["mesa"]

def test_gate2_audit_stddrift_prompt_noninteractive_aborts(tmp_path, monkeypatch):
    """Healable drift + mode=prompt + non-interactive → abort (never silently
    install a stranding libLLVM); points at --rebuild-soname-consumers."""
    from sysforge.pipeline.stages import toolchain as _tc
    from sysforge.primitives import prompt
    from sysforge.primitives import toolchain_safety as _ts

    pkg_dir = tmp_path / "llvm-libs"
    pkg_dir.mkdir()
    fake_pkg = pkg_dir / "llvm-libs-22.1.6-1-x86_64.pkg.tar.zst"
    fake_pkg.touch()
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [fake_pkg],
    )
    drift = [_ts.ToolchainFinding(
        "error", "libllvm_consumer_symbols", "std:: re-export drift",
        "Rebuild the consumer.", is_brick=True, healable=True,
    )]
    monkeypatch.setattr(_ts, "check_system_consumer_symbols", lambda pkgs: drift)
    monkeypatch.setattr(_ts, "libllvm_abi_consumers", lambda *, exclude: ["mesa"])
    monkeypatch.setattr(prompt, "is_interactive", lambda: False)

    with pytest.raises(RuntimeError, match="non-interactive"):
        _tc.gate2_audit(
            {"llvm-libs": pkg_dir / "PKGBUILD"}, [], _g2_opts("prompt"), {},
            dry_run=False,
        )

def test_assert_pass_links_shipped_libllvm_raises_on_dangling(tmp_path, monkeypatch):
    """A built consumer with a _ZNSt*@LLVM_* undefined ref means the pass linked
    the live /usr libLLVM (split defeated) → abort before install.
    """
    from sysforge.primitives import toolchain_safety as _ts
    pb = make_pkgbuild(tmp_path / "builds", "clang")
    hazard = [_ts.ToolchainFinding(
        _ts.SEV_ERROR, "abi_hazard",
        "clang-x.pkg.tar: libclang-cpp.so: _ZNSt7foo@LLVM_22.1 (should be GLIBCXX_*)",
        is_brick=True,
    )]
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [tmp_path / "clang.pkg.tar"],
    )
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: hazard)
    with pytest.raises(RuntimeError, match="staged libLLVM"):
        assert_pass_links_shipped_libllvm(
            {"clang": pb}, label="Pass 4b", dry_run=False,
        )

def test_assert_pass_links_shipped_libllvm_clean_passes(tmp_path, monkeypatch):
    """No dangling refs → no raise (the correctly-steered build)."""
    from sysforge.primitives import toolchain_safety as _ts
    pb = make_pkgbuild(tmp_path / "builds", "clang")
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: [tmp_path / "clang.pkg.tar"],
    )
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: [])
    assert_pass_links_shipped_libllvm({"clang": pb}, label="Pass 4b", dry_run=False)

def test_assert_pass_links_shipped_libllvm_noop_when_no_pkgs(tmp_path, monkeypatch):
    """No built packages resolved → no scan, no raise (degrade gracefully)."""
    from sysforge.primitives import toolchain_safety as _ts
    pb = make_pkgbuild(tmp_path / "builds", "clang")
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages", lambda m: [],
    )
    called = {"scan": False}

    def _spy(pkgs):
        called["scan"] = True
        return []
    monkeypatch.setattr(_ts, "scan_abi_hazards", _spy)
    assert_pass_links_shipped_libllvm({"clang": pb}, label="Pass 4b", dry_run=False)
    assert called["scan"] is False

def test_assert_pass_links_shipped_libllvm_dry_run_skips(tmp_path, monkeypatch):
    """dry_run → never touches the artifacts (nothing was built)."""
    from sysforge.primitives import toolchain_safety as _ts
    pb = make_pkgbuild(tmp_path / "builds", "clang")

    def _boom(m):
        raise AssertionError("must not resolve packages in dry-run")
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages", _boom,
    )
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: [])
    assert_pass_links_shipped_libllvm({"clang": pb}, label="Pass 4b", dry_run=True)

def test_assert_staging_has_llvm_cmake_raises_when_missing(tmp_path):
    """staging without LLVMConfig.cmake → fail fast (the root-cause guard)."""
    staging = tmp_path / "stage3"
    (staging / "usr/lib").mkdir(parents=True)  # libLLVM only, no cmake config
    with pytest.raises(RuntimeError, match="Pass-4 staging is incomplete"):
        assert_staging_has_llvm_cmake(staging)

def test_assert_staging_has_llvm_cmake_passes_when_present(tmp_path):
    """staging with LLVMConfig.cmake → no raise."""
    staging = tmp_path / "stage3"
    cfg = staging / "usr/lib/cmake/llvm"
    cfg.mkdir(parents=True)
    (cfg / "LLVMConfig.cmake").touch()
    assert_staging_has_llvm_cmake(staging)  # must not raise

def test_pgo_stage_instrumented_extracts_all_packages(tmp_path):
    """Phase 2: every Pass 1 package is staged into stage1 (including the
    cmake-config llvm pkg) so Pass 2's find_package(LLVM) finds stage1.
    Instrumented .a archives are tolerated and resolved via residual
    profile-runtime LDFLAGS in Pass 2 / Pass 3.  And no `pacman -U`
    is ever invoked — Pass 1 never touches the live root."""
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    llvm_pkg = pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst"
    llvm_libs_pkg = pkg_dir / "llvm-libs-18.0.0-1-x86_64.pkg.tar.zst"
    llvm_pkg.touch()
    llvm_libs_pkg.touch()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD", "llvm-libs": pkg_dir / "PKGBUILD"}
    staging1 = tmp_path / "stage1"

    extract_targets: list[str] = []
    pacman_called: list[bool] = []

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        result.stdout = ""
        if "makepkg" in cmd:
            result.stdout = f"{llvm_pkg}\n{llvm_libs_pkg}\n"
        elif "tar" in cmd and "-xf" in cmd:
            extract_targets.append(cmd[cmd.index("-xf") + 1])
        elif "pacman" in cmd:
            pacman_called.append(True)
        return result

    with patch("subprocess.run", side_effect=fake_run):
        pgo_stage_instrumented(pkgbuild_map, staging1, dry_run=False)

    assert str(llvm_libs_pkg) in extract_targets, "llvm-libs must stage to stage1"
    assert str(llvm_pkg) in extract_targets, \
        "Phase 2: cmake-config llvm pkg must also stage so find_package(LLVM) hits stage1"
    assert not pacman_called, "Pass 1 must not invoke pacman — stage instead of install"

def test_pgo_stage_instrumented_dry_run(tmp_path):
    pkgbuild_map = {"llvm": tmp_path / "llvm" / "PKGBUILD"}
    staging1 = tmp_path / "stage1"
    with patch("subprocess.run") as mock_run:
        pgo_stage_instrumented(pkgbuild_map, staging1, dry_run=True)
    mock_run.assert_not_called()

def test_pgo_stage_instrumented_raises_when_no_packages(tmp_path):
    pkg_dir = tmp_path / "llvm"
    pkg_dir.mkdir()
    pkgbuild_map = {"llvm": pkg_dir / "PKGBUILD"}
    staging1 = tmp_path / "stage1"

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = b""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="No built packages"):
            pgo_stage_instrumented(pkgbuild_map, staging1, dry_run=False)

def test_system_llvm_is_instrumented_true(tmp_path):
    """Returns True when nm output contains __llvm_profile_ symbols."""
    fake_lib = tmp_path / "libLLVMSupport.a"
    fake_lib.touch()
    nm_output = "0000 T __llvm_profile_instrument_target\n0000 T __llvm_profile_instrument_memop\n"
    with patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=nm_output, stderr="")
        result = system_llvm_is_instrumented()
    assert result is True

def test_system_llvm_is_instrumented_false(tmp_path):
    """Returns False when nm output has no profile symbols."""
    nm_output = "0000 T some_other_symbol\n"
    with patch("pathlib.Path.exists", return_value=True), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=nm_output, stderr="")
        result = system_llvm_is_instrumented()
    assert result is False

def test_system_llvm_is_instrumented_missing_lib():
    """Returns False when libLLVMSupport.a does not exist."""
    with patch("pathlib.Path.exists", return_value=False):
        result = system_llvm_is_instrumented()
    assert result is False

def test_profile_runtime_ldflag_returns_flag(tmp_path):
    """Returns a force-loaded (--whole-archive) full-path flag when the runtime
    lib exists. Force-load makes the runtime resolve regardless of link order, so
    a bfd link can't drop it ahead of the archives that reference __llvm_profile_*."""
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
        flag = profile_runtime_ldflag()

    assert flag is not None
    # Full archive path, force-loaded, scoped by push/pop-state.
    assert str(profile_lib) in flag
    assert "-Wl,--push-state,--whole-archive" in flag
    assert "-Wl,--pop-state" in flag

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
        flag = profile_runtime_ldflag()

    assert flag is None

def test_merge_profraw_merges_and_deletes_raws(tmp_path):
    _make_old_profraw(tmp_path / "a.profraw")
    _make_old_profraw(tmp_path / "b.profraw")

    with patch("subprocess.run", side_effect=fake_profdata_merge):
        result = merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    assert (tmp_path / "clang.profdata").exists()
    assert not (tmp_path / "a.profraw").exists()
    assert not (tmp_path / "b.profraw").exists()

def test_merge_profraw_no_files_no_profdata_raises(tmp_path):
    with pytest.raises(RuntimeError, match="No .profraw files and no profdata"):
        merge_profraw(tmp_path, dry_run=False)

def test_merge_profraw_existing_profdata_no_raws_returns_it(tmp_path):
    """Daemon already merged everything — final sweep returns existing profdata."""
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run") as mock_run:
        result = merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()

def test_merge_profraw_combines_raws_with_existing_profdata(tmp_path):
    """Final sweep merges remaining raws together with daemon's profdata."""
    _make_old_profraw(tmp_path / "late.profraw")
    (tmp_path / "clang.profdata").touch()

    with patch("subprocess.run", side_effect=fake_profdata_merge) as mock_run:
        result = merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    cmd = mock_run.call_args[0][0]
    # Both the existing profdata and the remaining profraw were inputs
    assert str(tmp_path / "clang.profdata") in cmd
    assert str(tmp_path / "late.profraw") in cmd

def test_merge_profraw_dry_run_skips_everything(tmp_path):
    with patch("subprocess.run") as mock_run:
        result = merge_profraw(tmp_path, dry_run=True)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()

def test_merge_profraw_fresh_raws_with_profdata_warns_and_returns(tmp_path):
    """Fresh profraw (< PROFRAW_SETTLE_SECS old) + existing profdata → warn, return profdata."""
    # Fresh profraw: use default touch() mtime (now)
    (tmp_path / "fresh.profraw").touch()
    (tmp_path / "clang.profdata").touch()

    # do_profraw_merge returns 0 because all profraw is too fresh to merge;
    # merge_profraw should warn and return the existing profdata rather than raising
    with patch("subprocess.run") as mock_run:
        result = merge_profraw(tmp_path, dry_run=False)

    assert result == tmp_path / "clang.profdata"
    mock_run.assert_not_called()  # llvm-profdata not invoked

def test_merge_profraw_fresh_raws_no_profdata_raises(tmp_path):
    """Fresh profraw + no profdata → error (no usable data at all)."""
    (tmp_path / "fresh.profraw").touch()

    with patch("subprocess.run"), pytest.raises(RuntimeError, match="too fresh"):
        merge_profraw(tmp_path, dry_run=False)

def test_pgo_profile_runtime_unavailable_no_injection(tmp_path):
    """If clang --print-runtime-dir returns nothing, profile_runtime_ldflag()
    yields None and no LDFLAGS injection happens for any pass."""
    call_log = _run_pgo(
        tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"],
        runtime_flag=None,
    )
    for call in call_log:
        assert call["lfe"] is None, \
            f"No linker flag when profile runtime is unavailable (cc={call['cc']})"

def test_pgo_stale_staging_purged_at_run_start(tmp_path):
    """
    A staging directory left by a prior failed run must be purged at the
    start of the next run, not accumulated on top of.  Stale Pass 3 binaries
    from an aborted build could otherwise shadow freshly extracted ones.
    """
    toml_path, pkgbuild_dir, staging, pgo_store, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs=["llvm"])

    # Simulate a stale staging dir left by a prior aborted Pass 3
    staging.mkdir(parents=True)
    stale_marker = staging / "stale_sentinel"
    stale_marker.touch()

    call_log = []
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=_pgo_fake_run_factory(pgo_store, call_log)), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_stage_instrumented"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_subprocess_factory()), \
         patch("sys.stdin.isatty", return_value=False), \
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=False):
        ToolchainStage().run(config, state, options)

    assert not stale_marker.exists(), \
        "Stale staging sentinel must be removed before Pass 1 starts"

def test_validate_pgo_environment_dry_run_skips_all_checks():
    """dry_run=True must skip all checks — no subprocess calls."""
    with patch("subprocess.run") as mock_run:
        validate_pgo_environment(dry_run=True)
    mock_run.assert_not_called()

def test_validate_pgo_environment_raises_if_clang_missing(tmp_path):
    """Raises RuntimeError immediately when /usr/bin/clang does not exist."""
    with patch("pathlib.Path.exists", return_value=False), \
         patch("shutil.which", return_value="/usr/bin/lld"):
        with pytest.raises(RuntimeError, match="clang not found"):
            validate_pgo_environment(dry_run=False)

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
            validate_pgo_environment(dry_run=False)

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
            validate_pgo_environment(dry_run=False)

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
            validate_pgo_environment(dry_run=False)

def test_validate_pgo_environment_clean_logs_info():
    """Logs a clean-environment info message when no instrumentation is detected."""
    info_calls = []
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=False), \
         patch("sysforge.log.info", side_effect=lambda *a: info_calls.append(a)):
        validate_pgo_environment(dry_run=False)

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
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=False), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        validate_pgo_environment(dry_run=False)   # must not raise

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
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        validate_pgo_environment(dry_run=False)   # must not raise

    assert warn_calls
    assert any("libLLVMSupport.a" in str(a) for a in warn_calls)

def test_validate_pgo_environment_instrumented_tty_decline_raises():
    """User declines the dirty-env prompt → RuntimeError before any build starts."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="n"), pytest.raises(RuntimeError, match="Aborted"):
        validate_pgo_environment(dry_run=False)

def test_validate_pgo_environment_instrumented_non_tty_raises():
    """In non-interactive mode, residual instrumentation is a hard failure —
    an unattended build must not silently proceed with a degraded environment."""
    with patch("pathlib.Path.exists", return_value=True), \
         patch("shutil.which", return_value="/usr/bin/lld"), \
         patch("pathlib.Path.glob", return_value=[]), \
         patch("subprocess.run", side_effect=_fake_healthy_clang), \
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=True), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Aborting unattended"):
            validate_pgo_environment(dry_run=False)
