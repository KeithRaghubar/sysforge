# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/pkgbuilds.py — PKGBUILD resolution and source sync.
"""
from sysforge.pipeline.stages.toolchain.pkgbuilds import resolve_all_pkgbuilds
from unittest.mock import MagicMock
from unittest.mock import patch
import pytest

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _toolchain_gates_clean,
    make_pkgbuild,
)


def test_resolve_all_pkgbuilds_finds_local(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    result = resolve_all_pkgbuilds(["llvm", "clang"], config, update=False)
    assert "llvm" in result
    assert "clang" in result
    assert result["llvm"].name == "PKGBUILD"

def test_resolve_all_pkgbuilds_missing_raises(tmp_path):
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            resolve_all_pkgbuilds(["nonexistent-pkg"], config, update=False)

def test_resolve_all_pkgbuilds_partial_miss_reports_all(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve"):
            resolve_all_pkgbuilds(["llvm", "clang", "lld"], config, update=False)

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
        result = resolve_all_pkgbuilds(["gcc", "gcc-libs"], config, update=False)

    assert "gcc" in result
    assert "gcc-libs" in result
    # Both must resolve to the same PKGBUILD — not two separate clones
    assert result["gcc"].parent == result["gcc-libs"].parent

def test_resolve_all_pkgbuilds_calls_scheduler_when_update_true(tmp_path):
    """update=True must route every unique resolved dir through SourceSyncScheduler."""
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    fake_scheduler = MagicMock()
    fake_result = MagicMock()
    fake_result.status = "up_to_date"
    fake_result.error = None
    fake_scheduler.request.return_value = fake_result

    # Patch repo_packages to {} so both pkgbases are routed as AUR,
    # exercising the _ensure_rpc path (real pacman would classify
    # llvm/clang as repo and skip it).
    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.get_scheduler",
               return_value=fake_scheduler), \
         patch("sysforge.primitives.config.load_sysforge_toml",
               return_value={"git": {}, "aur": {}}), \
         patch("sysforge.primitives.aur.repo_packages", return_value=set()):
        result = resolve_all_pkgbuilds(["llvm", "clang"], config, update=True)

    assert "llvm" in result and "clang" in result
    # Scheduler called once per unique pkgbuild_dir (two: llvm, clang)
    assert fake_scheduler.request.call_count == 2
    fake_scheduler._ensure_rpc.assert_called_once()
    fake_scheduler.close.assert_called_once()

def test_resolve_all_pkgbuilds_skips_scheduler_when_update_false(tmp_path):
    """update=False (mapped from --no-update) must not invoke the scheduler."""
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.get_scheduler") as gs:
        result = resolve_all_pkgbuilds(["llvm"], config, update=False)

    assert "llvm" in result
    gs.assert_not_called()

def test_resolve_all_pkgbuilds_blocker_status_raises(tmp_path):
    """STATUS_FAILED / STATUS_RATE_LIMITED / STATUS_PURGE_REFUSED must abort."""
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    fake_scheduler = MagicMock()
    fake_result = MagicMock()
    fake_result.status = "failed"   # STATUS_FAILED literal
    fake_result.error = "git fetch timed out"
    fake_scheduler.request.return_value = fake_result

    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.get_scheduler",
               return_value=fake_scheduler), \
         patch("sysforge.primitives.config.load_sysforge_toml",
               return_value={"git": {}, "aur": {}}), \
         patch("sysforge.primitives.aur.repo_packages", return_value=set()), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.STATUS_FAILED", "failed"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.SYNC_BLOCKING_STATUSES",
               frozenset({"failed"})), pytest.raises(RuntimeError, match="PKGBUILD sync failed"):
        resolve_all_pkgbuilds(["llvm"], config, update=True)

def test_sync_pkgbuild_dirs_classifies_repo_vs_aur(tmp_path):
    """Each SyncRequest must carry source='repo' for [extra]/[core]/etc.
    packages and source='aur' for AUR-only packages.

    Regression: before this, all toolchain SyncRequests were source='aur',
    so a --cleansrc on clang/llvm/lld silently failed to re-clone (the
    purge succeeded, then aur_clone tried gitlab.aur.org/clang.git).
    """
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    make_pkgbuild(tmp_path, "cosmic-comp-git")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    captured: list = []
    fake_scheduler = MagicMock()
    fake_result = MagicMock()
    fake_result.status = "up_to_date"
    fake_result.error = None

    def _request(req):
        captured.append((req.pkgbase, req.source))
        return fake_result

    fake_scheduler.request.side_effect = _request

    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.get_scheduler",
               return_value=fake_scheduler), \
         patch("sysforge.primitives.config.load_sysforge_toml",
               return_value={"git": {}, "aur": {}}), \
         patch("sysforge.primitives.aur.repo_packages",
               return_value={"llvm", "clang"}):
        resolve_all_pkgbuilds(
            ["llvm", "clang", "cosmic-comp-git"], config, update=True,
        )

    by_pkgbase = dict(captured)
    assert by_pkgbase["llvm"] == "repo"
    assert by_pkgbase["clang"] == "repo"
    assert by_pkgbase["cosmic-comp-git"] == "aur"
    fake_scheduler._ensure_rpc.assert_called_once_with(["cosmic-comp-git"])

def test_sync_pkgbuild_dirs_skips_rpc_for_repo_only_set(tmp_path):
    """When every package is in pacman's sync DBs, _ensure_rpc must not run
    — repo packages have no AUR-RPC equivalent and priming the cache with
    their names just wastes a request.
    """
    make_pkgbuild(tmp_path, "llvm")
    make_pkgbuild(tmp_path, "clang")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}

    fake_scheduler = MagicMock()
    fake_result = MagicMock()
    fake_result.status = "up_to_date"
    fake_result.error = None
    fake_scheduler.request.return_value = fake_result

    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.get_scheduler",
               return_value=fake_scheduler), \
         patch("sysforge.primitives.config.load_sysforge_toml",
               return_value={"git": {}, "aur": {}}), \
         patch("sysforge.primitives.aur.repo_packages",
               return_value={"llvm", "clang"}):
        resolve_all_pkgbuilds(["llvm", "clang"], config, update=True)

    fake_scheduler._ensure_rpc.assert_not_called()
    assert fake_scheduler.request.call_count == 2
