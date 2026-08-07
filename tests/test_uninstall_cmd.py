# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the uninstall verb."""
from types import SimpleNamespace

from sysforge.primitives.build_state import BuildState
from sysforge import uninstall_cmd
from sysforge.verbs.base import PreCheckResult


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry
    return bs


def test_requires_sentinel():
    assert uninstall_cmd.UninstallVerb.requires_sentinel is True


def test_plan_resolves_renamed_and_flags_tracked(tmp_path):
    bs = _bs(tmp_path, {
        "mesa-sysforge": {"build_mode": "fdo", "pkgbase": "mesa-sysforge",
                          "origin_pkgbase": "mesa"},
    })
    (item,) = uninstall_cmd.plan_uninstall(bs, ["mesa"])
    assert item.installed_name == "mesa-sysforge"
    assert item.tracked is True


def test_plan_untracked_passes_through(tmp_path):
    bs = _bs(tmp_path, {})
    (item,) = uninstall_cmd.plan_uninstall(bs, ["nano"])
    assert item.installed_name == "nano"
    assert item.tracked is False


def test_execute_removes_then_forgets_and_reconciles(tmp_path, monkeypatch):
    bs = _bs(tmp_path, {
        "mesa-sysforge": {"build_mode": "fdo", "pkgbase": "mesa-sysforge",
                          "origin_pkgbase": "mesa"},
    })
    order = []
    monkeypatch.setattr(uninstall_cmd.pacman, "uninstall_pkgs",
                        lambda names, extra_flags=None: order.append(
                            ("remove", names, extra_flags)))
    monkeypatch.setattr(uninstall_cmd, "cmd_state_forget",
                        lambda args: order.append(("forget", list(args.pkgnames))))
    monkeypatch.setattr(uninstall_cmd.install_reconcile, "external_install_targets",
                        lambda: set())

    verb = uninstall_cmd.UninstallVerb()
    args = SimpleNamespace(packages=["mesa"], pacman_flags=[], state_dir=str(tmp_path))
    pre = PreCheckResult(ctx={"items": uninstall_cmd.plan_uninstall(bs, ["mesa"]),
                              "state_dir": str(tmp_path)})
    res = verb.execute(args, pre)

    assert res.exit_code == 0
    assert order[0] == ("remove", ["mesa-sysforge"], [])
    assert order[1] == ("forget", ["mesa-sysforge"])  # forget runs after removal
