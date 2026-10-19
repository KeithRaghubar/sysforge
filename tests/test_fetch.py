"""fetch.cmd_fetch — sync keyed on the PKGBUILD's pkgbase; blockers fail (3.5.0-B82)."""
import argparse

import pytest

from sysforge import fetch
from sysforge.primitives.source_sync import STATUS_FROZEN, STATUS_UP_TO_DATE, SyncResult


@pytest.fixture
def renamed_checkout(tmp_path, monkeypatch):
    d = tmp_path / "my-renamed-dir"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgbase=foo-git\npkgname=(foo-git foo-libs-git)\n"
                                "pkgver=1\npkgrel=1\n")
    monkeypatch.setattr(fetch, "load_config", lambda **k: {})
    monkeypatch.setattr(fetch, "load_sysforge_toml", lambda: {})
    monkeypatch.setattr(fetch, "find_pkgbuild", lambda pkg, cfg: d / "PKGBUILD")
    return d


def _scheduler(monkeypatch, status):
    seen = []

    class _Sched:
        def request(self, req):
            seen.append(req)
            return SyncResult(pkgbase=req.pkgbase, status=status, error="denied")
    monkeypatch.setattr(fetch, "get_scheduler", lambda **k: _Sched())
    return seen


def _args():
    return argparse.Namespace(pkgs=["foo-git"], no_update=False, no_llvm_preflight=True)


def test_renamed_checkout_syncs_under_its_pkgbase(renamed_checkout, monkeypatch):
    seen = _scheduler(monkeypatch, STATUS_UP_TO_DATE)
    assert fetch.cmd_fetch(_args()) == 0
    assert seen[0].pkgbase == "foo-git"


def test_frozen_result_exits_nonzero(renamed_checkout, monkeypatch):
    _scheduler(monkeypatch, STATUS_FROZEN)
    assert fetch.cmd_fetch(_args()) == 1
