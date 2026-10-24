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

def _merged_llvm_tree(root):
    """An llvm pkgbase that now builds clang, plus a stale standalone clang/."""
    llvm = root / "llvm"
    llvm.mkdir()
    (llvm / "PKGBUILD").write_text(
        "pkgbase=llvm\npkgname=('llvm' 'llvm-libs' 'clang')\npkgver=23.1.3\npkgrel=2\n"
    )
    make_pkgbuild(root, "clang")  # frozen pre-merge checkout


def test_resolve_prefers_sync_db_pkgbase_over_stale_checkout(tmp_path, monkeypatch):
    """3.5.0-B13: clang now comes from the llvm pkgbase; the old clang/ checkout
    must not win just because it exists on disk."""
    from sysforge.primitives import pacman
    _merged_llvm_tree(tmp_path)
    bases = {"llvm": "llvm", "clang": "llvm"}
    monkeypatch.setattr(pacman, "get_sync_pkgbase", bases.get)
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    result = resolve_all_pkgbuilds(["llvm", "clang"], config, update=False)
    assert result["clang"] == (tmp_path / "llvm" / "PKGBUILD").resolve()


def test_resolve_warns_once_about_the_stale_checkout(tmp_path, monkeypatch, capsys):
    from sysforge import log
    from sysforge.primitives import pacman
    monkeypatch.setattr(log, "_VERBOSITY", 1)  # warn() is a -v narration level
    _merged_llvm_tree(tmp_path)
    monkeypatch.setattr(pacman, "get_sync_pkgbase", {"clang": "llvm"}.get)
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    resolve_all_pkgbuilds(["llvm", "clang"], config, update=False)
    out, err = capsys.readouterr()
    assert (out + err).count(str(tmp_path / "clang")) == 1


def test_resolve_without_sync_db_entry_keeps_local_order(tmp_path):
    """No repo entry (AUR, or the autouse stub): the local dir still wins."""
    _merged_llvm_tree(tmp_path)
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    result = resolve_all_pkgbuilds(["llvm", "clang"], config, update=False)
    assert result["clang"] == (tmp_path / "clang" / "PKGBUILD").resolve()


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


# ── show_version_changes: pre-build installed → PKGBUILD versions ──────────


def _write_pkgbuild(tmp_path, name, body):
    d = tmp_path / name
    d.mkdir()
    (d / "PKGBUILD").write_text(body)
    return d / "PKGBUILD"


def test_show_version_changes_lists_installed_to_pkgbuild(tmp_path, capsys):
    from sysforge.pipeline.stages.toolchain import pkgbuilds

    pkgbuild_map = {
        "llvm": _write_pkgbuild(tmp_path, "llvm", "pkgver=21.1.8\npkgrel=1\n"),
        "clang": _write_pkgbuild(tmp_path, "clang", "pkgver=21.1.6\npkgrel=1\n"),
        "lld": _write_pkgbuild(tmp_path, "lld", "pkgver=21.1.8\npkgrel=1\nepoch=1\n"),
    }
    installed = {"llvm": "21.1.6-1", "clang": "21.1.6-1", "lld": None}
    with patch.object(
        pkgbuilds.pacman, "get_installed_version", side_effect=installed.get
    ):
        pkgbuilds.show_version_changes(pkgbuild_map)

    out = capsys.readouterr()
    text = out.out + out.err
    assert "2 of 3 changing" in text
    assert "21.1.6-1" in text and "21.1.8-1" in text
    assert "21.1.6-1 (=)" in text  # clang unchanged
    assert "1:21.1.8-1" in text  # epoch carried; not installed renders a dash


def test_show_version_changes_tolerates_unreadable_pkgbuild(tmp_path, capsys):
    from sysforge.pipeline.stages.toolchain import pkgbuilds

    with patch.object(
        pkgbuilds.pacman, "get_installed_version", return_value="21.1.6-1"
    ):
        pkgbuilds.show_version_changes({"llvm": tmp_path / "missing" / "PKGBUILD"})

    out = capsys.readouterr()
    assert "1 of 1 changing" in out.out + out.err


# ---------------------------------------------------------------------------
# 3.5.0-F2: upstream's single llvm pkgbase
# ---------------------------------------------------------------------------

from sysforge.pipeline.stages.toolchain.pkgbuilds import (  # noqa: E402
    install_names,
    partition_by_pkgbuild,
)


def test_partition_moves_names_built_by_a_pgo_pkgbuild_into_pgo(tmp_path):
    """clang/lld come out of the llvm PKGBUILD now: building them in Pass 2 and
    4b would rebuild the whole llvm tree twice more."""
    llvm, spirv = tmp_path / "llvm/PKGBUILD", tmp_path / "spirv-llvm-translator/PKGBUILD"
    pgo, non_pgo, lib32 = partition_by_pkgbuild(
        {"llvm": llvm, "llvm-libs": llvm},
        {"clang": llvm, "lld": llvm, "spirv-llvm-translator": spirv},
        {},
    )
    assert pgo == {"llvm": llvm, "llvm-libs": llvm, "clang": llvm, "lld": llvm}
    assert non_pgo == {"spirv-llvm-translator": spirv}
    assert lib32 == {}


def test_partition_leaves_the_split_layout_alone(tmp_path):
    llvm, clang = tmp_path / "llvm/PKGBUILD", tmp_path / "clang/PKGBUILD"
    pgo, non_pgo, _ = partition_by_pkgbuild({"llvm": llvm}, {"clang": clang}, {})
    assert pgo == {"llvm": llvm} and non_pgo == {"clang": clang}


def test_install_names_adds_installed_members_only(tmp_path, monkeypatch):
    """lldb is installed and comes from the llvm pkgbase: install the new
    build of it. wasi-libc++ is built but was never installed: leave it."""
    from sysforge.primitives import pacman
    d = tmp_path / "llvm"
    d.mkdir()
    (d / "PKGBUILD").write_text(
        "pkgbase=llvm\npkgname=('llvm' 'llvm-libs' 'clang' 'lldb' 'wasi-libc++')\n"
        "pkgver=23.1.3\npkgrel=4\n"
    )
    monkeypatch.setattr(pacman, "get_all_installed_packages",
                        lambda: {"llvm": "1", "llvm-libs": "1", "lldb": "1", "zlib": "1"})
    names = install_names({"llvm": d / "PKGBUILD", "clang": d / "PKGBUILD"})
    assert names == {"llvm", "llvm-libs", "clang", "lldb"}


# --- 3.5.0-DEV2: the LLVM pre-flight gate and resolution edge paths ---

from types import SimpleNamespace  # noqa: E402

from sysforge.pipeline.stages.toolchain import pkgbuilds as pb  # noqa: E402


# --- run_llvm_preflight: the strict gate ---

@pytest.fixture
def preflight(monkeypatch):
    """A report with one state; strict on; blockers set per test."""
    world = SimpleNamespace(states=["llvm"], mismatch=False, strict=True,
                            blockers=["llvm: dirty tree"], interactive=False, answer="n")
    monkeypatch.setattr(pb, "collect_llvm_state", lambda names, cfg, probe_fetch: SimpleNamespace(
        states=world.states, has_pgo_profdata_mismatch=world.mismatch))
    monkeypatch.setattr(pb, "render_preflight", lambda report, verbose: "table")
    monkeypatch.setattr(pb, "_llvm_strict_enabled", lambda: world.strict)
    monkeypatch.setattr(pb, "evaluate_strict", lambda report, allow_dirty: world.blockers)
    monkeypatch.setattr(pb.prompt, "is_interactive", lambda: world.interactive)
    monkeypatch.setattr(pb.prompt, "prompt_choice", lambda *a, **k: world.answer)
    return world


def _run(**opts):
    pb.run_llvm_preflight(["llvm"], {}, SimpleNamespace(**opts))


def test_preflight_with_no_llvm_in_scope_does_nothing(preflight):
    preflight.states = []
    preflight.blockers = ["would refuse"]
    _run()


def test_preflight_strict_off_lets_dirty_trees_through(preflight):
    preflight.strict = False
    _run()


def test_preflight_strict_off_still_refuses_a_profdata_mismatch(preflight):
    preflight.strict = False
    preflight.mismatch = True
    with pytest.raises(RuntimeError, match="pre-flight refused"):
        _run()


def test_preflight_non_interactive_refuses_with_the_bypass_hint(preflight):
    with pytest.raises(RuntimeError, match="--allow-dirty-llvm") as exc:
        _run()
    assert "llvm: dirty tree" in str(exc.value)


def test_preflight_interactive_decline_aborts(preflight):
    preflight.interactive = True
    with pytest.raises(RuntimeError, match="aborted by user"):
        _run()


def test_preflight_interactive_accept_proceeds(preflight):
    preflight.interactive = True
    preflight.answer = "yes"
    _run()


def test_preflight_without_blockers_proceeds(preflight):
    preflight.blockers = []
    _run()


# --- resolve_all_pkgbuilds: the clone pass ---

def _pkgbuild(d, body):
    d.mkdir(parents=True, exist_ok=True)
    p = d / "PKGBUILD"
    p.write_text(body)
    return p


@pytest.fixture
def no_sync_pkgbase(monkeypatch):
    monkeypatch.setattr(pb.pacman, "get_sync_pkgbase", lambda name: None)






def test_split_scan_survives_an_unparsable_pkgbuild(tmp_path, monkeypatch, no_sync_pkgbase):
    src = tmp_path / "src"
    llvm = _pkgbuild(src / "llvm", "pkgname=llvm\n")
    monkeypatch.setattr(pb.pkgbuild_meta, "parse_pkgbuild", lambda p: 1 / 0)
    monkeypatch.setattr("sysforge.primitives.pkgbuild_meta.parse_pkgbuild", lambda p: 1 / 0)
    other = _pkgbuild(tmp_path / "clone" / "clang", "pkgname=clang\n")
    monkeypatch.setattr(pb.prim_config, "find_pkgbuild", lambda name, cfg: other)
    got = pb.resolve_all_pkgbuilds(["llvm", "clang"],
                                   {"paths": {"pkgbuild_src_dir": str(src)}}, update=False)
    assert got == {"llvm": llvm.resolve(), "clang": other}


def test_resolve_syncs_the_trees_when_asked(tmp_path, monkeypatch, no_sync_pkgbase):
    src = tmp_path / "src"
    _pkgbuild(src / "llvm", "pkgname=llvm\n")
    seen = []
    monkeypatch.setattr(pb, "sync_pkgbuild_dirs", lambda resolved, **kw: seen.append(kw))
    pb.resolve_all_pkgbuilds(["llvm"], {"paths": {"pkgbuild_src_dir": str(src)}},
                             update=False, cleansrc=True)
    assert seen == [{"cleansrc": True, "cleansrc_force": False}]


# 3.5.0-B141: a dry run reads only what is on disk; a missing checkout is
# named, never cloned, synced or purged.

def test_dry_run_resolves_what_is_on_disk_without_syncing(tmp_path):
    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs") as sync:
        result = resolve_all_pkgbuilds(["llvm"], config, update=True, cleansrc=True, dry_run=True)
    assert result["llvm"].name == "PKGBUILD"
    sync.assert_not_called()


def test_dry_run_names_missing_checkouts_instead_of_cloning(tmp_path):
    from sysforge.pipeline.stages.toolchain.pkgbuilds import CheckoutsMissing

    make_pkgbuild(tmp_path, "llvm")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path)}}
    with patch("sysforge.primitives.config.find_pkgbuild") as find:
        with pytest.raises(CheckoutsMissing) as exc:
            resolve_all_pkgbuilds(["llvm", "spirv-llvm-translator"], config, dry_run=True)
    find.assert_not_called()
    assert exc.value.pkgbases == ["spirv-llvm-translator"]
