import pytest
from pathlib import Path
from types import SimpleNamespace

from sysforge.primitives import restart_probe


def _mkproc(root: Path, pid: int, maps: str, cgroup: str = "0::/", comm: str = "proc"):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "maps").write_text(maps)
    (d / "cgroup").write_text(cgroup)
    (d / "comm").write_text(comm + "\n")
    return d


_MAP = "7f3c1a000000-7f3c1a021000 r-xp 00000000 00:1b 12345  {path}{suffix}\n"


def test_scan_keeps_package_prefix_deleted(tmp_path):
    _mkproc(tmp_path, 100, _MAP.format(path="/usr/lib/libXau.so.6.0.0", suffix=" (deleted)"))
    paths, unreadable = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {"/usr/lib/libXau.so.6.0.0": [100]}
    assert unreadable == 0


def test_scan_drops_dev_shm_noise(tmp_path):
    # The real-world case: 72/72 deleted mappings on this box were Steam scratch.
    _mkproc(tmp_path, 101, _MAP.format(path="/dev/shm/.com.valvesoftware.Steam.0I37ns",
                                       suffix=" (deleted)"))
    paths, _ = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}


@pytest.mark.parametrize("noise", [
    "/tmp/scratch.so", "/dev/shm/x", "/memfd:jit", "[heap]", "",
])
def test_scan_drops_non_package_prefixes(tmp_path, noise):
    _mkproc(tmp_path, 102, _MAP.format(path=noise, suffix=" (deleted)"))
    paths, _ = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}


def test_scan_ignores_non_deleted_mappings(tmp_path):
    _mkproc(tmp_path, 103, _MAP.format(path="/usr/lib/libc.so.6", suffix=""))
    paths, _ = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}


def test_scan_groups_multiple_pids_per_path(tmp_path):
    line = _MAP.format(path="/usr/lib/libXau.so.6.0.0", suffix=" (deleted)")
    _mkproc(tmp_path, 200, line)
    _mkproc(tmp_path, 201, line)
    paths, _ = restart_probe._scan_deleted_mappings(tmp_path)
    assert sorted(paths["/usr/lib/libXau.so.6.0.0"]) == [200, 201]


def test_scan_counts_unreadable_pids(tmp_path):
    d = tmp_path / "300"
    d.mkdir()
    (d / "maps").write_text("x")
    (d / "maps").chmod(0o000)
    paths, unreadable = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}
    assert unreadable == 1


def test_scan_ignores_vanished_pid(tmp_path):
    # A PID directory with no maps file at all — the process exited mid-scan.
    (tmp_path / "400").mkdir()
    paths, unreadable = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}
    assert unreadable == 0


def test_scan_ignores_non_pid_entries(tmp_path):
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("x")
    paths, unreadable = restart_probe._scan_deleted_mappings(tmp_path)
    assert paths == {}
    assert unreadable == 0


def test_tier_system_service():
    tier, unit, is_user = restart_probe._classify_cgroup(
        "0::/system.slice/NetworkManager.service", pid=500)
    assert (tier, unit, is_user) == (restart_probe.TIER_RESTART_UNIT,
                                     "NetworkManager.service", False)


def test_tier_user_service_is_restartable_not_relogin():
    # Guards the suffix-not-slice rule: this sits under user@1000.service but is
    # a .service leaf, so `systemctl --user restart` suffices.
    tier, unit, is_user = restart_probe._classify_cgroup(
        "0::/user.slice/user-1000.slice/user@1000.service/session.slice/wireplumber.service",
        pid=501)
    assert (tier, unit, is_user) == (restart_probe.TIER_RESTART_UNIT,
                                     "wireplumber.service", True)


def test_tier_session_scope_is_relogin():
    tier, unit, is_user = restart_probe._classify_cgroup(
        "0::/user.slice/user-1000.slice/session-3.scope", pid=502)
    assert tier == restart_probe.TIER_RELOGIN


def test_tier_app_scope_is_relogin():
    # A .scope under app.slice is NOT restartable, despite living beside
    # restartable .service units.
    tier, unit, is_user = restart_probe._classify_cgroup(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/cosmic-launcher.scope",
        pid=503)
    assert tier == restart_probe.TIER_RELOGIN


def test_tier_pid1_is_reboot():
    tier, _, _ = restart_probe._classify_cgroup("0::/init.scope", pid=1)
    assert tier == restart_probe.TIER_REBOOT


def test_tier_unrecognized_cgroup_has_no_tier():
    tier, unit, is_user = restart_probe._classify_cgroup("0::/", pid=504)
    assert (tier, unit, is_user) == (None, None, False)


def test_tier_rank_order():
    assert (restart_probe.tier_rank(restart_probe.TIER_REBOOT)
            > restart_probe.tier_rank(restart_probe.TIER_RELOGIN)
            > restart_probe.tier_rank(restart_probe.TIER_RESTART_UNIT))


def test_report_builds_entries_with_owners(tmp_path, monkeypatch):
    _mkproc(tmp_path, 100,
            _MAP.format(path="/usr/lib/libXau.so.6.0.0", suffix=" (deleted)"),
            cgroup="0::/system.slice/NetworkManager.service", comm="NetworkManager")
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths",
                        lambda paths: {"/usr/lib/libXau.so.6.0.0": "libxau"})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert len(rep.entries) == 1
    e = rep.entries[0]
    assert (e.pid, e.package, e.tier, e.unit) == (
        100, "libxau", restart_probe.TIER_RESTART_UNIT, "NetworkManager.service")
    assert e.comm == "NetworkManager"
    assert rep.highest_tier == restart_probe.TIER_RESTART_UNIT
    assert rep.partial is False


def test_report_unowned_path_keeps_entry_with_none_package(tmp_path, monkeypatch):
    _mkproc(tmp_path, 101,
            _MAP.format(path="/usr/local/lib/custom.so", suffix=" (deleted)"),
            cgroup="0::/system.slice/foo.service")
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths", lambda paths: {})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert len(rep.entries) == 1
    assert rep.entries[0].package is None


def test_report_highest_tier_is_worst_present(tmp_path, monkeypatch):
    _mkproc(tmp_path, 110, _MAP.format(path="/usr/lib/a.so", suffix=" (deleted)"),
            cgroup="0::/system.slice/foo.service")
    _mkproc(tmp_path, 111, _MAP.format(path="/usr/lib/b.so", suffix=" (deleted)"),
            cgroup="0::/user.slice/user-1000.slice/session-3.scope")
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths",
                        lambda paths: {"/usr/lib/a.so": "pa", "/usr/lib/b.so": "pb"})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert rep.highest_tier == restart_probe.TIER_RELOGIN


def test_report_sets_partial_on_unreadable(tmp_path, monkeypatch):
    d = tmp_path / "120"
    d.mkdir()
    (d / "maps").write_text("x")
    (d / "maps").chmod(0o000)
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths", lambda paths: {})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert rep.partial is True


def test_report_empty_has_no_highest_tier(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths", lambda paths: {})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert rep.entries == []
    assert rep.highest_tier is None


def test_kernel_moddir_missing_yields_reboot_entry(tmp_path, monkeypatch):
    # The running kernel's module dir is gone => its package was replaced
    # out from under it (upgraded to a newer kernel, not yet rebooted into).
    monkeypatch.setattr(restart_probe.os, "uname",
                        lambda: SimpleNamespace(release="6.19.1-arch1-1"))

    e = restart_probe._kernel_reboot_entry(modules_root=tmp_path)
    assert e is not None
    assert e.tier == restart_probe.TIER_REBOOT
    assert e.package is None
    assert e.path == str(tmp_path / "6.19.1-arch1-1")


def test_kernel_moddir_present_yields_no_entry(tmp_path, monkeypatch):
    # The running kernel's module dir still exists => it's still installed.
    (tmp_path / "6.19.1-arch1-1").mkdir()
    monkeypatch.setattr(restart_probe.os, "uname",
                        lambda: SimpleNamespace(release="6.19.1-arch1-1"))

    assert restart_probe._kernel_reboot_entry(modules_root=tmp_path) is None


def test_untiered_entry_never_wins_highest_tier_reduction(tmp_path, monkeypatch):
    # tier_rank(None) == -1 is the entire safety net keeping an untiered
    # entry (non-systemd host, or an unreadable cgroup degrading to "") from
    # outranking a real tier in the "worst wins" reduction.
    _mkproc(tmp_path, 200, _MAP.format(path="/usr/lib/a.so", suffix=" (deleted)"),
            cgroup="0::/system.slice/foo.service")
    _mkproc(tmp_path, 201, _MAP.format(path="/usr/lib/b.so", suffix=" (deleted)"),
            cgroup="0::/")  # unrecognized leaf => no tier
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths",
                        lambda paths: {"/usr/lib/a.so": "pa", "/usr/lib/b.so": "pb"})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert {e.tier for e in rep.entries} == {restart_probe.TIER_RESTART_UNIT, None}
    assert rep.highest_tier == restart_probe.TIER_RESTART_UNIT


def test_report_of_only_untiered_entries_has_no_highest_tier(tmp_path, monkeypatch):
    _mkproc(tmp_path, 202, _MAP.format(path="/usr/lib/c.so", suffix=" (deleted)"),
            cgroup="0::/")  # unrecognized leaf => no tier
    monkeypatch.setattr(restart_probe.pacman, "owners_of_paths",
                        lambda paths: {"/usr/lib/c.so": "pc"})
    monkeypatch.setattr(restart_probe, "_kernel_reboot_entry", lambda: None)

    rep = restart_probe.scan_stale_processes(proc_root=tmp_path)
    assert len(rep.entries) == 1
    assert rep.entries[0].tier is None
    assert rep.highest_tier is None


def test_is_kernel_entry_discriminates_pid1_reboot():
    kernel = restart_probe.StaleEntry(
        pid=0, comm="kernel", tier=restart_probe.TIER_REBOOT,
        package=None, path="/usr/lib/modules/6.19.1-arch1-1")
    systemd = restart_probe.StaleEntry(
        pid=1, comm="systemd", tier=restart_probe.TIER_REBOOT,
        package="systemd", path="/usr/lib/systemd/systemd")

    assert restart_probe.is_kernel_entry(kernel) is True
    assert restart_probe.is_kernel_entry(systemd) is False
