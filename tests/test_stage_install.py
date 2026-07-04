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
