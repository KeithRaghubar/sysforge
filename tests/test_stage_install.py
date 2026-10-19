# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

import pytest
from sysforge.pipeline.stages import install as inst


class _Opts:
    def __init__(self, dry_run=False): self.dry_run = dry_run


def _patch_cfg(monkeypatch):
    from sysforge.pipeline.stages._bootstrap import BootstrapConfig
    cfg = BootstrapConfig(target="/mnt", device="/dev/vda", hostname="h",
                          locale="en_US.UTF-8", timezone="UTC")
    monkeypatch.setattr(inst, "load_bootstrap", lambda: cfg)
    # Probe would hit a real /dev/vda; stub a plausible 40 GiB disk.
    monkeypatch.setattr(inst, "probe_disk_size_bytes", lambda _dev: 40 * 1024**3)
    monkeypatch.setattr(inst, "refuse_unsafe_device", lambda _dev: None)
    return cfg


def test_decline_confirmation_aborts_without_running(monkeypatch):
    _patch_cfg(monkeypatch)
    def deny(_cfg): raise RuntimeError("[PARTITION] Aborted by user.")
    monkeypatch.setattr(inst, "_confirm", deny)
    ran = []
    monkeypatch.setattr(inst, "run_archinstall", lambda *a, **k: ran.append(True))
    with pytest.raises(RuntimeError, match="Aborted"):
        inst.InstallStage().run(config={}, state=None, options=_Opts())
    assert ran == []


def test_dry_run_skips_confirm_and_passes_dry_run(monkeypatch):
    _patch_cfg(monkeypatch)
    confirmed = []
    monkeypatch.setattr(inst, "_confirm", lambda c: confirmed.append(True))
    seen = {}
    monkeypatch.setattr(inst, "run_archinstall",
                        lambda cfg_dict, *, dry_run: seen.update(dry_run=dry_run))
    inst.InstallStage().run(config={}, state=None, options=_Opts(dry_run=True))
    assert confirmed == []          # no prompt on dry-run
    assert seen["dry_run"] is True


def test_real_run_confirms_then_invokes(monkeypatch):
    _patch_cfg(monkeypatch)
    order = []
    monkeypatch.setattr(inst, "_confirm", lambda c: order.append("confirm"))
    monkeypatch.setattr(inst, "run_archinstall",
                        lambda cfg_dict, *, dry_run: order.append("run"))
    inst.InstallStage().run(config={}, state=None, options=_Opts(dry_run=False))
    assert order == ["confirm", "run"]


def test_unprobeable_disk_aborts_real_run(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(inst, "probe_disk_size_bytes", lambda _dev: None)
    monkeypatch.setattr(inst, "_confirm", lambda c: None)
    ran = []
    monkeypatch.setattr(inst, "run_archinstall", lambda *a, **k: ran.append(True))
    with pytest.raises(RuntimeError, match="could not determine the size"):
        inst.InstallStage().run(config={}, state=None, options=_Opts(dry_run=False))
    assert ran == []  # never partitioned with an unknown disk size


def test_unprobeable_disk_dry_run_uses_nominal_size(monkeypatch):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(inst, "probe_disk_size_bytes", lambda _dev: None)
    seen = {}
    monkeypatch.setattr(inst, "run_archinstall",
                        lambda cfg_dict, *, dry_run: seen.update(cfg_dict=cfg_dict))
    inst.InstallStage().run(config={}, state=None, options=_Opts(dry_run=True))
    root = next(p for p in seen["cfg_dict"]["disk_config"]["device_modifications"][0]["partitions"]
                if p["obj_id"] == "root")
    assert root["size"]["value"] > 0  # preview still renders a concrete layout



# ---------------------------------------------------------------------------
# 3.5.0-B96: the disk the live system runs from is refused before the prompt
# ---------------------------------------------------------------------------

def test_non_block_path_is_refused(tmp_path):
    from sysforge.pipeline.stages._partition_plan import refuse_unsafe_device
    f = tmp_path / "disk.img"
    f.write_bytes(b"")
    with pytest.raises(RuntimeError, match="not a block device"):
        refuse_unsafe_device(str(f))


def _fake_block(monkeypatch, lsblk_out):
    import stat
    from types import SimpleNamespace
    from sysforge.pipeline.stages import _partition_plan as pp
    monkeypatch.setattr(pp.Path, "stat", lambda self: SimpleNamespace(st_mode=stat.S_IFBLK))
    monkeypatch.setattr(pp.run, "probe", lambda argv: SimpleNamespace(
        returncode=0, stdout=lsblk_out))
    return pp


@pytest.mark.parametrize("out,what", [
    ("nvme0n1 \nnvme0n1p1 /boot\nnvme0n1p2 /\n", "nvme0n1p2 on /"),
    ("sdb \nsdb1 /run/archiso/bootmnt\n", "archiso"),
    ("sda \nsda2 [SWAP]\n", "SWAP"),
])
def test_mounted_device_is_refused(monkeypatch, out, what):
    pp = _fake_block(monkeypatch, out)
    with pytest.raises(RuntimeError, match="in use") as e:
        pp.refuse_unsafe_device("/dev/x")
    assert what in str(e.value)


def test_unmounted_block_device_passes(monkeypatch):
    pp = _fake_block(monkeypatch, "vda \nvda1 \n")
    pp.refuse_unsafe_device("/dev/vda")


def test_real_run_checks_the_device_before_the_prompt(monkeypatch):
    _patch_cfg(monkeypatch)
    order = []
    monkeypatch.setattr(inst, "refuse_unsafe_device", lambda d: order.append("check"))
    monkeypatch.setattr(inst, "_confirm", lambda c: order.append("confirm"))
    monkeypatch.setattr(inst, "run_archinstall", lambda *a, **k: None)
    inst.InstallStage().run({}, None, _Opts())
    assert order == ["check", "confirm"]
