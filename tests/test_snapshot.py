# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
from sysforge.primitives import snapshot


def test_root_is_btrfs_true(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda2 / btrfs rw,relatime 0 0\n")
    assert snapshot.root_is_btrfs(str(mounts)) is True


def test_root_is_btrfs_false(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda2 / ext4 rw,relatime 0 0\n")
    assert snapshot.root_is_btrfs(str(mounts)) is False


def test_snapper_config_for_root_found(tmp_path):
    cfgdir = tmp_path / "configs"
    cfgdir.mkdir()
    (cfgdir / "root").write_text('SUBVOLUME="/"\nFSTYPE="btrfs"\n')
    (cfgdir / "home").write_text('SUBVOLUME="/home"\n')
    assert snapshot.snapper_config_for_root(str(cfgdir)) == "root"


def test_snapper_config_for_root_absent(tmp_path):
    cfgdir = tmp_path / "configs"
    cfgdir.mkdir()
    assert snapshot.snapper_config_for_root(str(cfgdir)) is None


def test_ensure_disabled_by_default_is_noop(monkeypatch):
    snapshot.reset_guard()
    calls = []
    monkeypatch.setattr(snapshot, "create_raw_snapshot", lambda: calls.append("raw"))
    monkeypatch.setattr(snapshot, "root_is_btrfs", lambda *a: True)
    snapshot.ensure_pre_build_snapshot({"build": {}})  # key absent → off
    assert calls == []


def test_ensure_dry_run_takes_no_action(monkeypatch):
    snapshot.reset_guard()
    calls = []
    monkeypatch.setattr(snapshot, "root_is_btrfs", lambda *a: True)
    monkeypatch.setattr(snapshot, "snapper_config_for_root", lambda *a: None)
    monkeypatch.setattr(snapshot, "create_raw_snapshot", lambda: calls.append("raw"))
    snapshot.ensure_pre_build_snapshot(
        {"build": {"pre_build_snapshot": True}}, dry_run=True)
    assert calls == []


def test_ensure_once_guard(monkeypatch):
    snapshot.reset_guard()
    calls = []
    monkeypatch.setattr(snapshot, "root_is_btrfs", lambda *a: True)
    monkeypatch.setattr(snapshot, "snapper_config_for_root", lambda *a: None)
    monkeypatch.setattr(snapshot, "create_raw_snapshot",
                        lambda: (calls.append("raw"), __import__("pathlib").Path("/x"))[1])
    cfg = {"build": {"pre_build_snapshot": True}}
    snapshot.ensure_pre_build_snapshot(cfg)
    snapshot.ensure_pre_build_snapshot(cfg)  # second call: guard blocks
    assert calls == ["raw"]


def test_ensure_non_btrfs_skips(monkeypatch):
    snapshot.reset_guard()
    calls = []
    monkeypatch.setattr(snapshot, "root_is_btrfs", lambda *a: False)
    monkeypatch.setattr(snapshot, "create_raw_snapshot", lambda: calls.append("raw"))
    snapshot.ensure_pre_build_snapshot({"build": {"pre_build_snapshot": True}})
    assert calls == []
