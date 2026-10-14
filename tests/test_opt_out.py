"""
test_opt_out.py — the durable revert/forget decision (3.4.0-F4).

``revert`` and ``state forget`` record an opt-out keyed by the stock pkgbase;
``update`` subtracts it after every adoption source and reports what it
suppressed; only an explicit adoption (``build``, ``update <pkg>``,
``packages add``) clears it; ``uninstall`` writes none.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sysforge.primitives.build_state import BuildState
from sysforge.primitives.opt_out import (
    VIA_FORGET,
    VIA_REVERT,
    OptOutError,
    OptOutList,
    covering,
)
from sysforge.verbs.shared import forget_packages


def _make_args(**kwargs):
    defaults = dict(
        state_dir=None, dry_run=False, devel=False, offline=True,
        no_toolchain_preflight=True, no_pkg_log=True, persist_log=False,
        log_dir=None, profile_conf=None, cache_report=False, packages=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _seed(state_dir: Path, pkgname, pkgbase, **kw):
    state_dir.mkdir(parents=True, exist_ok=True)
    bs = BuildState(state_dir)
    bs.record(pkgname=pkgname, pkgver="1", pkgrel="1", epoch="0", pkgbase=pkgbase,
              pkgbuild_dir=state_dir, build_mode="source_built", source="repo", **kw)
    bs.save()


# ---------------------------------------------------------------------------
# OptOutList
# ---------------------------------------------------------------------------

def test_missing_file_is_empty(tmp_path):
    assert OptOutList(tmp_path).load() == {}


def test_add_round_trips_and_covers_every_member(tmp_path):
    OptOutList(tmp_path).add("mesa", ["mesa", "vulkan-radeon"], via=VIA_REVERT)
    got = OptOutList(tmp_path).load()
    assert got["mesa"]["via"] == "revert"
    assert got["mesa"]["pkgnames"] == ["mesa", "vulkan-radeon"]
    assert covering(got, "vulkan-radeon") == "mesa"
    assert covering(got, "htop") is None


def test_clear_by_one_member_clears_the_whole_pkgbase(tmp_path):
    lst = OptOutList(tmp_path)
    lst.add("mesa", ["mesa", "vulkan-radeon"], via=VIA_REVERT)
    lst.add("htop", ["htop"], via=VIA_FORGET)
    assert lst.clear(["vulkan-radeon"]) == ["mesa"]
    assert set(lst.load()) == {"htop"}


def test_clear_without_a_match_writes_nothing(tmp_path):
    assert OptOutList(tmp_path).clear(["htop"]) == []
    assert not (tmp_path / "opted-out.toml").exists()


def test_corrupt_file_raises_rather_than_reading_empty(tmp_path):
    (tmp_path / "opted-out.toml").write_text("not = [toml")
    with pytest.raises(OptOutError):
        OptOutList(tmp_path).load()


# ---------------------------------------------------------------------------
# The writer: forget_packages(opt_out=...)
# ---------------------------------------------------------------------------

def test_forget_with_opt_out_records_the_stock_pkgbase(tmp_path):
    # A conflict-renamed split set: recorded under the renamed names.
    _seed(tmp_path, "mesa-sysforge", "mesa-sysforge", origin_pkgbase="mesa")
    _seed(tmp_path, "vulkan-radeon-sysforge", "mesa-sysforge", origin_pkgbase="mesa")
    forget_packages(tmp_path, ["mesa-sysforge"], opt_out=VIA_REVERT)
    rec = OptOutList(tmp_path).load()
    assert set(rec) == {"mesa"}
    # Both spellings are covered: update sees the stock names after a revert.
    for name in ("mesa", "vulkan-radeon", "mesa-sysforge", "vulkan-radeon-sysforge"):
        assert covering(rec, name) == "mesa"


def test_forget_without_opt_out_records_nothing(tmp_path):
    # uninstall's call shape: a removed package is a fresh decision later.
    _seed(tmp_path, "htop", "htop")
    forget_packages(tmp_path, ["htop"])
    assert OptOutList(tmp_path).load() == {}


def test_state_forget_writes_the_record(tmp_path, capsys):
    from sysforge.state_cmd import cmd_state_forget
    _seed(tmp_path, "htop", "htop")
    cmd_state_forget(SimpleNamespace(state_dir=str(tmp_path), pkgnames=["htop"]))
    assert OptOutList(tmp_path).load()["htop"]["via"] == "forget"
    assert "will not re-adopt" in capsys.readouterr().out


def test_revert_records_a_revert_opt_out(tmp_path, monkeypatch):
    from sysforge import revert_cmd
    monkeypatch.setattr(revert_cmd, "resolve_state_dir", lambda *a, **k: (tmp_path, "test"))
    _seed(tmp_path, "htop", "htop")
    verb = revert_cmd.RevertToStockVerb()
    args = SimpleNamespace(packages=["htop"], dry_run=False, force=True, state_dir=None)
    with patch.object(revert_cmd.pacman, "reinstall_repo_pkgs"):
        pre = verb.pre_check(args)
        assert verb.execute(args, pre).exit_code == 0
    assert OptOutList(tmp_path).load()["htop"]["via"] == "revert"


def test_uninstall_writes_no_record(tmp_path, monkeypatch):
    from sysforge import uninstall_cmd
    from sysforge.verbs.base import PreCheckResult
    _seed(tmp_path, "htop", "htop")
    monkeypatch.setattr(uninstall_cmd.pacman, "uninstall_pkgs",
                        lambda names, extra_flags=None: None)
    monkeypatch.setattr(uninstall_cmd.install_reconcile, "external_install_targets",
                        lambda: set())
    bs = BuildState(tmp_path)
    args = SimpleNamespace(packages=["htop"], pacman_flags=[], state_dir=str(tmp_path))
    pre = PreCheckResult(ctx={"items": uninstall_cmd.plan_uninstall(bs, ["htop"]),
                              "state_dir": str(tmp_path)})
    assert uninstall_cmd.UninstallVerb().execute(args, pre).exit_code == 0
    assert BuildState(tmp_path).get("htop") is None  # forgotten...
    assert OptOutList(tmp_path).load() == {}           # ...but not opted out


# ---------------------------------------------------------------------------
# update: subtraction, reporting, explicit re-adoption
# ---------------------------------------------------------------------------

def _opt_out(update_scenario, name, via=VIA_REVERT):
    OptOutList(update_scenario.state_dir).add(name, [name], via=via)


def _newer_htop(update_scenario):
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")


def test_override_does_not_readopt_a_reverted_package(update_scenario, capsys):
    update_scenario.add_override("htop", source="repo", enable_build_from_source=True)
    _newer_htop(update_scenario)
    _opt_out(update_scenario, "htop")
    builds = update_scenario.run(_make_args(), installed={"htop": "3.3.0-1"})
    assert builds == []
    err = capsys.readouterr().err
    assert "htop: not re-adopted (packages.toml override)" in err
    assert err.count("not re-adopted") == 1


def test_repo_mode_build_from_source_does_not_readopt(update_scenario, capsys):
    update_scenario.set_repo_mode("build_from_source")
    _newer_htop(update_scenario)
    _opt_out(update_scenario, "htop")
    builds = update_scenario.run(_make_args(), installed={"htop": "3.3.0-1"})
    assert builds == []
    assert 'htop: not re-adopted (repo_mode = "build_from_source")' in capsys.readouterr().err


def test_forgotten_foreign_package_is_not_readopted(update_scenario, capsys):
    _newer_htop(update_scenario)
    _opt_out(update_scenario, "htop", via=VIA_FORGET)
    builds = update_scenario.run(_make_args(), installed={"htop": "3.3.0-1"},
                                 foreign={"htop": "3.3.0-1"})
    assert builds == []
    assert "foreign (AUR) package" in capsys.readouterr().err


def test_update_naming_the_package_clears_and_readopts(update_scenario, capsys):
    update_scenario.add_override("htop", source="repo", enable_build_from_source=True)
    _newer_htop(update_scenario)
    _opt_out(update_scenario, "htop")
    builds = update_scenario.run(_make_args(pkgnames=["htop"]),
                                 installed={"htop": "3.3.0-1"})
    assert len(builds) == 1
    assert OptOutList(update_scenario.state_dir).load() == {}
    assert "htop: maintained again" in "".join(capsys.readouterr())


def test_dry_run_naming_the_package_keeps_the_record(update_scenario):
    update_scenario.add_override("htop", source="repo", enable_build_from_source=True)
    _newer_htop(update_scenario)
    _opt_out(update_scenario, "htop")
    update_scenario.run(_make_args(pkgnames=["htop"], dry_run=True),
                        installed={"htop": "3.3.0-1"})
    assert "htop" in OptOutList(update_scenario.state_dir).load()


def test_split_set_opts_out_and_clears_as_one(update_scenario):
    update_scenario.set_repo_mode("build_from_source")
    OptOutList(update_scenario.state_dir).add(
        "mesa", ["mesa", "vulkan-radeon"], via=VIA_REVERT)
    builds = update_scenario.run(
        _make_args(), installed={"mesa": "1-1", "vulkan-radeon": "1-1"})
    assert builds == []
    # Naming one member re-adopts the whole pkgbase.
    assert OptOutList(update_scenario.state_dir).clear(["vulkan-radeon"]) == ["mesa"]
    assert OptOutList(update_scenario.state_dir).load() == {}


# ---------------------------------------------------------------------------
# packages add: explicit adoption
# ---------------------------------------------------------------------------

def test_packages_add_clears_the_record(tmp_path, monkeypatch, capsys):
    from sysforge.packages_cmd import cmd_packages_add
    state_dir = tmp_path / "state"
    OptOutList(state_dir).add("htop", ["htop"], via=VIA_REVERT)
    cmd_packages_add(SimpleNamespace(
        pkg="htop", enable_build_from_source=True, no_cache=False, reason=None,
        source=None, packages=str(tmp_path / "packages.toml"),
        state_dir=str(state_dir)))
    assert OptOutList(state_dir).load() == {}
    assert "maintained again" in capsys.readouterr().out
