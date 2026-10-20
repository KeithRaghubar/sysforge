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


# --- 3.5.0-DEV2: the failure paths (every one a non-fatal warn) ---

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


def _probe_result(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


class _Log:
    """Stands in for the module logger (``Logger`` is slotted, so its methods
    cannot be patched one at a time)."""

    def __init__(self):
        self.warned, self.shown = [], []

    def warn(self, msg):
        self.warned.append(msg)

    def ui(self, msg):
        self.shown.append(msg)

    def info(self, msg):
        pass


def _record_log(monkeypatch):
    log = _Log()
    monkeypatch.setattr(snapshot, "_log", log)
    return log


def test_root_is_btrfs_unreadable_mounts_is_false(tmp_path):
    assert snapshot.root_is_btrfs(str(tmp_path / "missing")) is False


def test_root_is_btrfs_without_a_root_entry_is_false(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda3 /home btrfs rw 0 0\n")
    assert snapshot.root_is_btrfs(str(mounts)) is False


def test_snapper_config_for_root_without_configs_dir(tmp_path):
    assert snapshot.snapper_config_for_root(str(tmp_path / "none")) is None


def test_snapper_config_for_root_skips_an_unreadable_entry(tmp_path):
    cfgdir = tmp_path / "configs"
    cfgdir.mkdir()
    (cfgdir / "a-dir").mkdir()  # read_text raises IsADirectoryError (an OSError)
    (cfgdir / "root").write_text('SUBVOLUME="/"\n')
    assert snapshot.snapper_config_for_root(str(cfgdir)) == "root"


def test_create_snapper_snapshot_without_snapper(monkeypatch):
    monkeypatch.setattr(snapshot.shutil, "which", lambda n: None)
    assert snapshot.create_snapper_snapshot("root", "d") is None


@pytest.mark.parametrize("probe", [
    lambda argv: _probe_result(1, err="no permission"),
    lambda argv: (_ for _ in ()).throw(OSError("exec failed")),
])
def test_create_snapper_snapshot_failure_warns_and_returns_none(monkeypatch, probe):
    monkeypatch.setattr(snapshot.shutil, "which", lambda n: "/usr/bin/snapper")
    monkeypatch.setattr(snapshot.run, "probe", probe)
    warned = _record_log(monkeypatch).warned
    assert snapshot.create_snapper_snapshot("root", "d") is None
    assert warned and "snapper snapshot failed" in warned[0]


def test_create_snapper_snapshot_returns_the_number(monkeypatch):
    monkeypatch.setattr(snapshot.shutil, "which", lambda n: "/usr/bin/snapper")
    seen = []
    monkeypatch.setattr(snapshot.run, "probe",
                        lambda argv: seen.append(argv) or _probe_result(0, out="42\n"))
    assert snapshot.create_snapper_snapshot("root", "sysforge pre-build") == "42"
    assert seen[0] == ["snapper", "-c", "root", "create", "-d", "sysforge pre-build", "-p"]


def test_create_raw_snapshot_without_btrfs_warns(monkeypatch):
    monkeypatch.setattr(snapshot.shutil, "which", lambda n: None)
    warned = _record_log(monkeypatch).warned
    assert snapshot.create_raw_snapshot() is None
    assert "btrfs command not found" in warned[0]


def _raw_env(monkeypatch, *, authorised=True, privileged=None):
    """The raw path with btrfs present; sudo and the privilege seam faked."""
    monkeypatch.setattr(snapshot.shutil, "which", lambda n: "/usr/bin/btrfs")
    monkeypatch.setattr(snapshot.sudo_session, "authenticate", lambda: authorised)
    seen = []

    def _run_privileged(argv, *, tag, **_k):
        seen.append((argv, tag))
        if privileged is not None:
            privileged(argv)

    monkeypatch.setattr(snapshot.privilege, "run_privileged", _run_privileged)
    return seen


def _refuse(argv):
    raise RuntimeError("[SNAPSHOT] btrfs failed: Permission denied")


def _exec_failed(argv):
    raise OSError("exec failed")


@pytest.mark.parametrize("privileged", [_refuse, _exec_failed])
def test_create_raw_snapshot_failure_warns_and_returns_none(monkeypatch, privileged):
    _raw_env(monkeypatch, privileged=privileged)
    warned = _record_log(monkeypatch).warned
    assert snapshot.create_raw_snapshot() is None
    assert "raw btrfs snapshot failed" in warned[0]


def test_create_raw_snapshot_runs_mkdir_and_btrfs_through_the_privilege_seam(monkeypatch):
    """3.5.0-B117: both need root, so as the build user they always failed."""
    seen = _raw_env(monkeypatch)
    monkeypatch.setattr(snapshot.run, "probe",
                        lambda *a, **k: pytest.fail("ran unprivileged"))
    dest = snapshot.create_raw_snapshot()
    assert dest is not None and dest.parent == Path("/.snapshots")
    assert dest.name.startswith("sysforge-pre-build-")
    assert seen == [
        (["mkdir", "-p", "/.snapshots"], "SNAPSHOT"),
        (["btrfs", "subvolume", "snapshot", "-r", "/", str(dest)], "SNAPSHOT"),
    ]


def test_create_raw_snapshot_declined_sudo_warns_and_runs_nothing(monkeypatch):
    seen = _raw_env(monkeypatch, authorised=False)
    warned = _record_log(monkeypatch).warned
    assert snapshot.create_raw_snapshot() is None
    assert seen == []
    assert "sudo was not authorised" in warned[0]


def _enabled(monkeypatch, *, snapper_cfg):
    snapshot.reset_guard()
    monkeypatch.setattr(snapshot, "root_is_btrfs", lambda *a: True)
    monkeypatch.setattr(snapshot, "snapper_config_for_root", lambda *a: snapper_cfg)
    return {"build": {"pre_build_snapshot": True}}, _record_log(monkeypatch).shown


def test_ensure_interactive_decline_takes_nothing(monkeypatch):
    cfg, shown = _enabled(monkeypatch, snapper_cfg=None)
    calls = []
    monkeypatch.setattr(snapshot, "create_raw_snapshot", lambda: calls.append("raw"))
    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "n")
    snapshot.ensure_pre_build_snapshot(cfg, interactive=True)
    assert calls == [] and "skipped by user" in shown[-1]


def test_ensure_interactive_accept_takes_the_snapshot(monkeypatch):
    cfg, shown = _enabled(monkeypatch, snapper_cfg=None)
    monkeypatch.setattr(snapshot, "create_raw_snapshot", lambda: Path("/.snapshots/s"))
    monkeypatch.setattr("sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "y")
    snapshot.ensure_pre_build_snapshot(cfg, interactive=True)
    assert "/.snapshots/s" in shown[-1] and "you own its cleanup" in shown[-1]


def test_ensure_prefers_snapper_and_reports_its_number(monkeypatch):
    cfg, shown = _enabled(monkeypatch, snapper_cfg="root")
    monkeypatch.setattr(snapshot, "create_raw_snapshot",
                        lambda: pytest.fail("raw snapshot taken with snapper configured"))
    monkeypatch.setattr(snapshot, "create_snapper_snapshot", lambda c, d: "7")
    snapshot.ensure_pre_build_snapshot(cfg)
    assert shown == ["  Pre-build snapshot created (snapper #7)"]


def test_ensure_failed_snapshot_reports_no_success(monkeypatch):
    cfg, shown = _enabled(monkeypatch, snapper_cfg="root")
    monkeypatch.setattr(snapshot, "create_snapper_snapshot", lambda c, d: None)
    snapshot.ensure_pre_build_snapshot(cfg)
    assert shown == []


def test_ensure_dry_run_names_the_plan(monkeypatch):
    cfg, shown = _enabled(monkeypatch, snapper_cfg="root")
    snapshot.ensure_pre_build_snapshot(cfg, dry_run=True)
    assert shown == ["  [dry-run] would take a pre-build snapper snapshot (config 'root')"]
