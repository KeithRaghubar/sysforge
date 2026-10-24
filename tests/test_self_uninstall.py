# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`sysforge self-uninstall` (3.5.0-F19): revert to stock, then remove sysforge."""
import pytest

from sysforge import self_uninstall_cmd as su
from sysforge.primitives import paths
from sysforge.primitives.build_state import BuildState
from sysforge.setup_cmd import _patch_conf_text, _unpatch_conf_text
from tests.cli_args import cli_args


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry
    return bs


REPO = {"tree", "mesa", "vulkan-radeon", "linux"}


def _plan(tmp_path, entries, **kw):
    kw.setdefault("owner", "sysforge")
    kw.setdefault("pacman_conf", tmp_path / "missing.conf")
    kw.setdefault("installed", set(entries))
    return su.plan_self_uninstall(_bs(tmp_path, entries), in_repo=REPO.__contains__, **kw)


# --- planning -------------------------------------------------------------

def test_a_repo_package_is_reverted_and_an_aur_one_kept(tmp_path):
    plan = _plan(tmp_path, {
        "tree": {"build_mode": "source_built", "pkgbase": "tree"},
        "cbonsai": {"build_mode": "source_built", "pkgbase": "cbonsai"},
        "nano": {"build_mode": "pacman"},
    })
    assert [rp.target for rp in plan.reverts] == ["tree"]
    assert [n for n, _ in plan.kept] == ["cbonsai"]


def test_a_split_set_is_planned_once(tmp_path):
    entries = {n: {"build_mode": "source_built", "pkgbase": "mesa"}
               for n in ("mesa", "vulkan-radeon")}
    plan = _plan(tmp_path, entries, )
    assert len(plan.reverts) == 1
    assert sorted(n for n, _ in plan.reverts[0].members) == ["mesa", "vulkan-radeon"]


def test_a_renamed_build_without_a_stock_package_is_kept(tmp_path):
    plan = _plan(tmp_path, {
        "linux-sysforge-fdo": {"build_mode": "autofdo_kernel", "pkgbase": "linux-sysforge-fdo",
                               "origin_pkgbase": "linux-sysforge"},
    })
    assert plan.reverts == []
    assert "linux-sysforge" in plan.kept[0][1]


def test_only_existing_dirs_are_planned(tmp_path):
    there = tmp_path / "there"
    there.mkdir()
    plan = _plan(tmp_path, {}, system_dirs=[there, tmp_path / "gone"],
                 user_dirs=[tmp_path / "also-gone"])
    assert plan.system_dirs == [there] and plan.user_dirs == []


def test_the_ignore_group_and_chroot_block_are_found(tmp_path):
    conf = tmp_path / "pacman.conf"
    conf.write_text(_patch_conf_text("[options]\n"))
    chroot = tmp_path / "chroot.conf"
    from sysforge.primitives import local_repo
    chroot.write_text(local_repo._with_section("[core]\n", local_repo.section_text(tmp_path)))
    plan = _plan(tmp_path, {}, pacman_conf=conf, chroot_conf=chroot)
    assert plan.ignore_group and plan.chroot_conf == chroot


def test_package_dirs_never_follow_an_env_override():
    assert (paths.Path("/etc/sysforge"),
                                  paths.Path("/var/lib/sysforge"),
                                  paths.Path("/var/cache/sysforge")) == paths.PACKAGE_DIRS


# --- the removal script ---------------------------------------------------

def test_removal_script_removes_the_package_then_the_data():
    plan = su.SelfUninstallPlan(package="sysforge-git", group=True,
                                system_dirs=[paths.Path("/var/lib/sysforge")])
    assert su.removal_script(plan, keep_data=False) == (
        "pacman -Rns --noconfirm -- sysforge-git && rm -rf -- /var/lib/sysforge"
        " && groupdel sysforge")


def test_keep_data_keeps_config_and_data():
    plan = su.SelfUninstallPlan(package="sysforge", group=True,
                                system_dirs=[paths.Path("/var/lib/sysforge")])
    assert su.removal_script(plan, keep_data=True) == "pacman -Rs --noconfirm -- sysforge"


def test_a_checkout_removes_only_data():
    plan = su.SelfUninstallPlan(package=None, system_dirs=[paths.Path("/etc/sysforge")])
    assert su.removal_script(plan, keep_data=False) == "rm -rf -- /etc/sysforge"
    assert su.removal_script(su.SelfUninstallPlan(), keep_data=True) is None


def test_ignore_group_removal_restores_the_file():
    base = "[options]\nHoldPkg = pacman\n\n[core]\n"
    assert _unpatch_conf_text(_patch_conf_text(base)) == base
    assert _unpatch_conf_text("IgnoreGroup = a sf-build b\n") == "IgnoreGroup = a b\n"


# --- execute --------------------------------------------------------------

@pytest.fixture
def run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(su.prompt, "is_interactive", lambda: False)
    monkeypatch.setattr(su.sudo_session, "authenticate", lambda: True)
    def _revert(plans, state_dir):
        calls.append(("revert", [p.target for p in plans]))
        return 0

    monkeypatch.setattr(su, "apply_reverts", _revert)
    monkeypatch.setattr(su, "remove_ignore_group", lambda path: calls.append(("ignore",)))

    class _Done:
        returncode = 0

    monkeypatch.setattr(su.subprocess, "run",
                        lambda argv, **kw: calls.append(("sh", argv[-1])) or _Done())
    user_dir = tmp_path / "user-cache"
    user_dir.mkdir()

    def go(*flags, reverts=True):
        entries = {"tree": {"build_mode": "source_built", "pkgbase": "tree"}} if reverts else {}
        plan = _plan(tmp_path, entries, user_dirs=[user_dir])
        plan.ignore_group = True
        args = cli_args("self-uninstall", "--state-dir", str(tmp_path), *flags)
        pre = su.PreCheckResult(ctx={"plan": plan, "state_dir": tmp_path})
        return su.SelfUninstallVerb().execute(args, pre).exit_code

    return go, calls, user_dir


def test_steps_run_in_order(run):
    go, calls, user_dir = run
    assert go("--yes") == 0
    assert [c[0] for c in calls] == ["revert", "ignore", "sh"]
    assert calls[2][1].startswith("pacman -Rns --noconfirm -- sysforge")
    assert not user_dir.exists()


def test_dry_run_changes_nothing(run):
    go, calls, user_dir = run
    assert go("--dry-run") == 0
    assert calls == [] and user_dir.exists()


def test_without_yes_or_a_terminal_it_refuses(run):
    go, calls, _ = run
    assert go() == 2
    assert calls == []


def test_a_failed_revert_stops_before_removal(run, monkeypatch):
    go, calls, user_dir = run
    monkeypatch.setattr(su, "apply_reverts", lambda plans, state_dir: 1)
    assert go("--yes") == 1
    assert calls == [] and user_dir.exists()


def test_keep_data_leaves_user_dirs(run):
    go, calls, user_dir = run
    assert go("--yes", "--keep-data") == 0
    assert calls[-1] == ("sh", "pacman -Rs --noconfirm -- sysforge")
    assert user_dir.exists()
