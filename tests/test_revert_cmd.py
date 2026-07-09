# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""test_revert_cmd.py — tests for plan_revert planner."""
from sysforge.primitives.build_state import BuildState
from sysforge import revert_cmd


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry  # direct seed for the test fixture
    return bs


def test_plan_plain_source_built_reinstalls_stock_name(tmp_path):
    bs = _bs(tmp_path, {"mesa": {"build_mode": "source_built", "pkgbase": "mesa"}})
    (plan,) = revert_cmd.plan_revert(bs, ["mesa"])
    assert plan.action == "reinstall"
    assert plan.stock_pkg == "mesa"
    assert plan.pkgname == "mesa"


def test_plan_optimized_conflict_mode_is_replace(tmp_path):
    # `pgo` is a conflict-mode optimization: the -sysforge build provides/
    # conflicts the stock name, so revert is a single `pacman -S <stock>`.
    bs = _bs(tmp_path, {
        "llvm-sysforge": {"build_mode": "pgo", "pkgbase": "llvm-sysforge",
                          "origin_pkgbase": "llvm"},
    })
    # user names the renamed package
    (p1,) = revert_cmd.plan_revert(bs, ["llvm-sysforge"])
    assert p1.action == "replace"
    assert p1.pkgname == "llvm-sysforge"
    assert p1.stock_pkg == "llvm"
    # user names the stock base — resolves via origin_pkgbase
    (p2,) = revert_cmd.plan_revert(bs, ["llvm"])
    assert p2.action == "replace"
    assert p2.pkgname == "llvm-sysforge"
    assert p2.stock_pkg == "llvm"


def test_plan_optimized_coexist_mode_is_derename(tmp_path):
    # kernel FDO (`autofdo_kernel`) coexists with stock — revert must remove the
    # renamed build then reinstall stock.
    bs = _bs(tmp_path, {
        "linux-sysforge": {"build_mode": "autofdo_kernel",
                           "pkgbase": "linux-sysforge",
                           "origin_pkgbase": "linux"},
    })
    (p1,) = revert_cmd.plan_revert(bs, ["linux-sysforge"])
    assert p1.action == "derename"
    assert p1.pkgname == "linux-sysforge"
    assert p1.stock_pkg == "linux"
    (p2,) = revert_cmd.plan_revert(bs, ["linux"])
    assert p2.action == "derename"
    assert p2.pkgname == "linux-sysforge"
    assert p2.stock_pkg == "linux"


def test_plan_untracked_is_skip(tmp_path):
    bs = _bs(tmp_path, {})
    (plan,) = revert_cmd.plan_revert(bs, ["nano"])
    assert plan.action == "skip"


def test_plan_pacman_marker_is_skip(tmp_path):
    bs = _bs(tmp_path, {"nano": {"build_mode": "pacman", "pkgbase": "nano"}})
    (plan,) = revert_cmd.plan_revert(bs, ["nano"])
    assert plan.action == "skip"


import argparse
from unittest.mock import patch


def _args(**kw):
    ns = argparse.Namespace(packages=["mesa"], force=True, dry_run=False)
    ns.__dict__.update(kw)
    return ns


def test_execute_reinstall_forgets_and_reconciles(tmp_path, monkeypatch):
    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["mesa"] = {"build_mode": "source_built", "pkgbase": "mesa"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall, \
         patch.object(revert_cmd.pacman, "remove_pkgs") as remove, \
         patch.object(revert_cmd, "cmd_state_forget") as forget:
        pre = verb.pre_check(_args())
        res = verb.execute(_args(), pre)
    reinstall.assert_called_once_with(["mesa"])
    remove.assert_not_called()
    forget.assert_called_once()
    assert res.exit_code == 0


def test_execute_conflict_replace_reinstalls_only(tmp_path, monkeypatch):
    # conflict-mode (`pgo`): a single `pacman -S <stock>` swaps the -sysforge
    # build atomically; NO explicit remove (would break reverse deps).
    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["llvm-sysforge"] = {"build_mode": "pgo", "pkgbase": "llvm-sysforge",
                                 "origin_pkgbase": "llvm"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall, \
         patch.object(revert_cmd.pacman, "remove_pkgs") as remove, \
         patch.object(revert_cmd, "cmd_state_forget"):
        pre = verb.pre_check(_args(packages=["llvm-sysforge"]))
        verb.execute(_args(packages=["llvm-sysforge"]), pre)
    reinstall.assert_called_once_with(["llvm"])
    remove.assert_not_called()


def test_execute_coexist_derename_removes_then_reinstalls(tmp_path, monkeypatch):
    # coexist-mode (kernel FDO): remove the renamed build, THEN reinstall stock.
    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["linux-sysforge"] = {"build_mode": "autofdo_kernel",
                                  "pkgbase": "linux-sysforge",
                                  "origin_pkgbase": "linux"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall, \
         patch.object(revert_cmd.pacman, "remove_pkgs") as remove, \
         patch.object(revert_cmd, "cmd_state_forget"):
        pre = verb.pre_check(_args(packages=["linux-sysforge"]))
        verb.execute(_args(packages=["linux-sysforge"]), pre)
    remove.assert_called_once_with(["linux-sysforge"])
    reinstall.assert_called_once_with(["linux"])


def test_execute_noninteractive_without_force_aborts(tmp_path, monkeypatch):
    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    monkeypatch.setattr(revert_cmd.prompt, "is_interactive", lambda: False)
    bs = BuildState(tmp_path)
    bs._data["mesa"] = {"build_mode": "source_built", "pkgbase": "mesa"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall:
        pre = verb.pre_check(_args(force=False))
        res = verb.execute(_args(force=False), pre)
    reinstall.assert_not_called()
    assert res.exit_code == 2


def test_execute_derename_reinstall_failure_stops_and_skips_forget(tmp_path, monkeypatch):
    # coexist derename: remove succeeds, reinstall FAILS → "left without" warning.
    import subprocess

    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["linux-sysforge"] = {"build_mode": "autofdo_kernel",
                                  "pkgbase": "linux-sysforge",
                                  "origin_pkgbase": "linux"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs",
                       side_effect=subprocess.CalledProcessError(1, ["pacman"])), \
         patch.object(revert_cmd.pacman, "remove_pkgs") as remove, \
         patch.object(revert_cmd, "cmd_state_forget") as forget, \
         patch.object(revert_cmd, "_log") as mock_log:
        pre = verb.pre_check(_args(packages=["linux-sysforge"]))
        res = verb.execute(_args(packages=["linux-sysforge"]), pre)
    remove.assert_called_once_with(["linux-sysforge"])
    forget.assert_not_called()
    assert res.exit_code == 1
    assert any("left without" in str(c) for c in mock_log.error.call_args_list)


def test_execute_derename_remove_failure_reports_nothing_changed(tmp_path, monkeypatch):
    # coexist derename: the REMOVE step fails → system intact, no "left without".
    import subprocess

    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["linux-sysforge"] = {"build_mode": "autofdo_kernel",
                                  "pkgbase": "linux-sysforge",
                                  "origin_pkgbase": "linux"}
    bs.save()
    with patch.object(revert_cmd.pacman, "remove_pkgs",
                       side_effect=subprocess.CalledProcessError(1, ["pacman"])), \
         patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall, \
         patch.object(revert_cmd, "cmd_state_forget") as forget, \
         patch.object(revert_cmd, "_log") as mock_log:
        pre = verb.pre_check(_args(packages=["linux-sysforge"]))
        res = verb.execute(_args(packages=["linux-sysforge"]), pre)
    reinstall.assert_not_called()
    forget.assert_not_called()
    assert res.exit_code == 1
    assert any("nothing changed" in str(c) for c in mock_log.error.call_args_list)


def test_execute_dry_run_mutates_nothing(tmp_path, monkeypatch):
    verb = revert_cmd.RevertToStockVerb()
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    bs = BuildState(tmp_path)
    bs._data["mesa"] = {"build_mode": "source_built", "pkgbase": "mesa"}
    bs.save()
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs") as reinstall, \
         patch.object(revert_cmd, "cmd_state_forget") as forget:
        pre = verb.pre_check(_args(dry_run=True))
        verb.execute(_args(dry_run=True), pre)
    reinstall.assert_not_called()
    forget.assert_not_called()
