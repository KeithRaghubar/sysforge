# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_removed_failures.py — a removed package's build failure is cleared
(3.5.0-B120), whoever removed it, while a never-installed failure and a split
pkgbase with a member still installed keep theirs.
"""

import pytest

from cli_args import cli_args
from sysforge import state_cmd, uninstall_cmd
from sysforge.primitives import install_reconcile as ir
from sysforge.primitives import pacman, state_probe
from sysforge.primitives.build_state import BuildState
from sysforge.verbs.base import PreCheckResult
from sysforge.verbs.shared import forget_packages


@pytest.fixture
def local_db(tmp_path, monkeypatch):
    """A local pacman DB tree; ``local_db(name, base=None)`` installs into it."""
    root = tmp_path / "local"
    root.mkdir()
    monkeypatch.setattr(pacman, "_LOCAL_DB_ROOT", root)
    # No sync repo carries these, so the demotion half has nothing to demote.
    monkeypatch.setattr(pacman, "get_pacman_sync_version", lambda name: None)

    def install(name, base=None):
        entry = root / f"{name}-1-1"
        entry.mkdir()
        desc = f"%NAME%\n{name}\n\n" + (f"%BASE%\n{base}\n\n" if base else "")
        (entry / "desc").write_text(desc)

    # Never empty: an unreadable DB is the one case the pass refuses to act on.
    install("glibc")
    return install


def _window(*names):
    """The buildstate hook's record of one transaction touching *names*."""
    ir.SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    (ir.SENTINEL_DIR / "buildstate").write_text(
        "2026-10-20T10:00:00Z\n" + "".join(f"{n}\n" for n in names) + "\n")


def _state(tmp_path, entries=None, failures=()):
    bs = BuildState(tmp_path)
    for name, entry in (entries or {}).items():
        bs._data[name] = entry
    for base in failures:
        bs.record_failure(base, error="boom")
    bs.save()
    return BuildState(tmp_path)


def _src(pkgbase, **kw):
    return {"build_mode": "source_built", "pkgver": "1", "pkgrel": "1",
            "pkgbase": pkgbase, **kw}


def test_pacman_r_recorded_only_by_the_hook_clears_the_failure(tmp_path, local_db):
    bs = _state(tmp_path, {"foo": _src("foo")}, failures=["foo"])
    _window("foo")  # a plain `pacman -R foo`; foo is gone from the local DB

    result = ir.consume_external_installs(bs)

    assert result.cleared_failures == ["foo"] and result.changed
    assert bs.all_failures() == {}
    assert not (ir.SENTINEL_DIR / "buildstate").exists()


def test_a_never_installed_failure_is_kept(tmp_path, local_db):
    bs = _state(tmp_path, failures=["newpkg"])
    _window("htop")  # an unrelated transaction

    assert ir.consume_external_installs(bs).cleared_failures == []
    assert "newpkg" in bs.all_failures()


def test_an_upgraded_target_keeps_its_failure(tmp_path, local_db):
    local_db("foo")
    bs = _state(tmp_path, {"foo": _src("foo")}, failures=["foo"])
    _window("foo")  # `pacman -Syu` upgraded foo: still installed

    assert ir.consume_external_installs(bs).cleared_failures == []
    assert "foo" in bs.all_failures()


def test_split_member_removal_keeps_the_failure_while_a_sibling_is_installed(
        tmp_path, local_db):
    local_db("libfoo", base="foo")
    entries = {"libfoo": _src("foo"), "foo-docs": _src("foo")}
    bs = _state(tmp_path, entries, failures=["foo"])
    _window("foo-docs")

    assert ir.consume_external_installs(bs).cleared_failures == []
    assert "foo" in bs.all_failures()


def test_split_sibling_seen_only_in_the_local_db_keeps_the_failure(tmp_path, local_db):
    # build_state's plain pacman marker carries no pkgbase; %BASE% does.
    local_db("libfoo", base="foo")
    bs = _state(tmp_path, {"foo": _src("foo"), "libfoo": {"build_mode": "pacman"}},
                failures=["foo"])
    _window("foo")

    assert ir.consume_external_installs(bs).cleared_failures == []


def test_last_split_member_removed_clears_the_failure(tmp_path, local_db):
    entries = {"libfoo": _src("foo"), "foo-docs": _src("foo")}
    bs = _state(tmp_path, entries, failures=["foo"])
    _window("libfoo", "foo-docs")

    assert ir.consume_external_installs(bs).cleared_failures == ["foo"]


def test_renamed_build_removal_clears_the_stock_keyed_failure(tmp_path, local_db):
    entries = {"mesa-sysforge": _src("mesa-sysforge", origin_pkgbase="mesa")}
    bs = _state(tmp_path, entries, failures=["mesa"])
    _window("mesa-sysforge")

    assert ir.consume_external_installs(bs).cleared_failures == ["mesa"]


def test_renamed_member_still_installed_keeps_the_stock_keyed_failure(tmp_path, local_db):
    local_db("mesa-sysforge", base="mesa-sysforge")
    bs = _state(tmp_path, {"vulkan-radeon-sysforge": _src("mesa-sysforge",
                                                          origin_pkgbase="mesa")},
                failures=["mesa"])
    _window("vulkan-radeon-sysforge")

    assert ir.consume_external_installs(bs).cleared_failures == []


def test_an_unreadable_local_db_clears_nothing(tmp_path):
    # conftest points _LOCAL_DB_ROOT at an empty tree: every target looks gone.
    bs = _state(tmp_path, {"foo": _src("foo")}, failures=["foo"])

    assert ir.clear_removed_failures(bs, {"foo"}) == []


def test_read_only_clears_in_memory_and_keeps_the_window(tmp_path, local_db):
    bs = _state(tmp_path, {"foo": _src("foo")}, failures=["foo"])
    _window("foo")

    assert ir.consume_external_installs(bs, read_only=True).cleared_failures == ["foo"]
    assert (ir.SENTINEL_DIR / "buildstate").exists()
    assert "foo" in BuildState(tmp_path).all_failures()


# --- uninstall ---------------------------------------------------------------

def _uninstall(tmp_path, monkeypatch, targets):
    monkeypatch.setattr(uninstall_cmd.pacman, "uninstall_pkgs",
                        lambda names, extra_flags=None: None)
    bs = BuildState(tmp_path)
    args = cli_args("uninstall", *targets, "--yes", "--state-dir", str(tmp_path))
    pre = PreCheckResult(ctx={"items": uninstall_cmd.plan_uninstall(bs, targets),
                              "state_dir": str(tmp_path)})
    assert uninstall_cmd.UninstallVerb().execute(args, pre).exit_code == 0
    return BuildState(tmp_path)


def test_uninstall_of_a_tracked_package_clears_its_failure(tmp_path, monkeypatch, local_db):
    _state(tmp_path, {"mesa-sysforge": _src("mesa-sysforge", origin_pkgbase="mesa")},
           failures=["mesa"])

    after = _uninstall(tmp_path, monkeypatch, ["mesa"])  # no hook window at all

    assert after.all_failures() == {} and after.get("mesa-sysforge") is None


def test_uninstall_of_an_untracked_package_clears_its_failure(tmp_path, monkeypatch, local_db):
    _state(tmp_path, failures=["foo"])

    assert _uninstall(tmp_path, monkeypatch, ["foo"]).all_failures() == {}


def test_uninstall_of_one_split_member_keeps_the_failure(tmp_path, monkeypatch, local_db):
    local_db("libfoo", base="foo")
    _state(tmp_path, {"libfoo": _src("foo"), "foo-docs": _src("foo")}, failures=["foo"])

    assert "foo" in _uninstall(tmp_path, monkeypatch, ["foo-docs"]).all_failures()


# --- forget / revert ---------------------------------------------------------

@pytest.mark.parametrize("opt_out", [None, "revert"])
def test_forget_clears_the_forgotten_pkgbase_s_failure(tmp_path, opt_out):
    _state(tmp_path, {"mesa-sysforge": _src("mesa-sysforge", origin_pkgbase="mesa"),
                      "mesa-docs-sysforge": _src("mesa-sysforge", origin_pkgbase="mesa")},
           failures=["mesa"])

    forget_packages(tmp_path, ["mesa-sysforge"], opt_out=opt_out)

    assert BuildState(tmp_path).all_failures() == {}


def test_forget_of_a_failure_with_no_record_clears_it(tmp_path):
    _state(tmp_path, failures=["newpkg"])

    forgotten, missing = forget_packages(tmp_path, ["newpkg"])

    assert (forgotten, missing) == ([], ["newpkg"])
    assert BuildState(tmp_path).all_failures() == {}


def test_forget_of_one_member_keeps_a_still_tracked_pkgbase_s_failure(tmp_path):
    _state(tmp_path, {"libfoo": _src("foo"), "foo-docs": _src("foo")}, failures=["foo"])

    forget_packages(tmp_path, ["foo-docs"])

    assert "foo" in BuildState(tmp_path).all_failures()


# --- read-only reports -------------------------------------------------------

def test_state_failed_omits_a_removal_without_saving(tmp_path, local_db, capsys):
    _state(tmp_path, {"foo": _src("foo")}, failures=["foo", "bar"])
    _window("foo")

    state_cmd.cmd_state_failed(cli_args("state", "failed", "--names", "--no-pager",
                                        state_dir=str(tmp_path)))

    assert capsys.readouterr().out.split() == ["bar"]
    assert set(BuildState(tmp_path).all_failures()) == {"foo", "bar"}
    assert (ir.SENTINEL_DIR / "buildstate").exists()


def test_doctor_does_not_report_a_removed_package_s_failure(tmp_path, local_db):
    _state(tmp_path, {"foo": _src("foo")}, failures=["foo", "bar"])
    _window("foo")

    findings = state_probe.collect_state_findings(tmp_path, installed={})

    ids = {f.check_id for f in findings if f.check_id.startswith("build_failure:")}
    assert ids == {"build_failure:bar"}
    assert set(BuildState(tmp_path).all_failures()) == {"foo", "bar"}


# --- a tracked entry no longer installed (4.0.0-B1) ---------------------------
# The window is an event and can be lost before a reader sees it; the tracked
# entry of a package that is gone is state, still there until update's sync
# prunes it, so it clears the failure without any window.

def test_tracked_removal_with_no_window_clears_the_failure(tmp_path, local_db):
    bs = _state(tmp_path, {"ethtool-git": _src("ethtool-git")}, failures=["ethtool-git"])

    result = ir.consume_external_installs(bs)  # no hook record at all

    assert result.cleared_failures == ["ethtool-git"]
    assert bs.all_failures() == {}


def test_tracked_removal_keeps_the_failure_while_a_sibling_is_in_the_local_db(
        tmp_path, local_db):
    local_db("libfoo", base="foo")
    bs = _state(tmp_path, {"foo": _src("foo"), "libfoo": {"build_mode": "pacman"}},
                failures=["foo"])

    assert ir.consume_external_installs(bs).cleared_failures == []


def test_build_failed_skips_a_removed_package_without_saving(tmp_path, local_db):
    import sysforge.build_cmd as build_cmd

    _state(tmp_path, {"ethtool-git": _src("ethtool-git")},
           failures=["ethtool-git", "newpkg"])
    args = cli_args("build", "--failed", "--state-dir", str(tmp_path))

    build_cmd._add_recorded_failures(args)

    assert args.pkgbuilds == ["newpkg"]
    assert set(BuildState(tmp_path).all_failures()) == {"ethtool-git", "newpkg"}


def test_build_failed_skips_a_window_removal(tmp_path, local_db):
    import sysforge.build_cmd as build_cmd

    _state(tmp_path, failures=["foo", "bar"])
    _window("foo")
    args = cli_args("build", "--failed", "--state-dir", str(tmp_path))

    build_cmd._add_recorded_failures(args)

    assert args.pkgbuilds == ["bar"]
    assert (ir.SENTINEL_DIR / "buildstate").exists()


def test_state_failed_omits_a_tracked_removal(tmp_path, local_db, capsys):
    _state(tmp_path, {"foo": _src("foo")}, failures=["foo", "bar"])

    state_cmd.cmd_state_failed(cli_args("state", "failed", "--names", "--no-pager",
                                        state_dir=str(tmp_path)))

    assert capsys.readouterr().out.split() == ["bar"]
