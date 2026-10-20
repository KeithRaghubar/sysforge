"""[safety] llvm_preflight suppresses the LLVM pre-flight table (3.5.0-B110).

Shipped and documented, but no verb read it: only ``--no-llvm-preflight`` did.
"""
import argparse
import types

import pytest

from sysforge import build_cmd, fetch, update
from sysforge.build_cmd import BuildVerb
from sysforge.primitives import llvm_state


@pytest.mark.parametrize("flag, key, suppressed", [
    (False, None, False),   # unset: the table shows
    (False, True, False),
    (False, False, True),   # the key turns it off
    (True, True, True),     # the flag wins over the key
])
def test_preflight_suppressed_precedence(flag, key, suppressed):
    safety = {} if key is None else {"llvm_preflight": key}
    args = argparse.Namespace(no_llvm_preflight=flag)
    assert llvm_state.preflight_suppressed(args, {"safety": safety}) is suppressed


@pytest.fixture
def preflight_off(monkeypatch):
    """``sysforge.toml`` with ``[safety] llvm_preflight = false``."""
    monkeypatch.setattr("sysforge.primitives.config.load_sysforge_toml",
                        lambda: {"safety": {"llvm_preflight": False}})


def test_fetch_honours_the_key(preflight_off, monkeypatch, tmp_path):
    d = tmp_path / "foo-git"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=foo-git\npkgver=1\npkgrel=1\n")
    monkeypatch.setattr(fetch, "load_config", lambda **k: {})
    monkeypatch.setattr(fetch, "load_sysforge_toml", lambda: {})
    monkeypatch.setattr(fetch, "find_pkgbuild", lambda pkg, cfg: d / "PKGBUILD")
    from sysforge.primitives.source_sync import STATUS_UP_TO_DATE, SyncResult

    class _Sched:
        def request(self, req):
            return SyncResult(pkgbase=req.pkgbase, status=STATUS_UP_TO_DATE)
    monkeypatch.setattr(fetch, "get_scheduler", lambda **k: _Sched())
    called = []
    monkeypatch.setattr(fetch, "collect_llvm_state", lambda *a: called.append(a))
    fetch.cmd_fetch(argparse.Namespace(pkgs=["foo-git"], no_update=False,
                                       no_llvm_preflight=False))
    assert called == []


def test_build_honours_the_key(preflight_off, monkeypatch):
    monkeypatch.setattr(build_cmd, "load_config_with_overrides",
                        lambda args: {"defaults": {"toolchain": "gcc"}})
    called = []
    monkeypatch.setattr(build_cmd, "_render_llvm_preflight", lambda *a: called.append(a))
    args = types.SimpleNamespace(pgo_mode=None, no_pkg_log=False, log_dir=None,
                                 dry_run=False, no_llvm_preflight=False,
                                 pkgbuilds=["mesa"])
    BuildVerb().pre_check(args)
    assert called == []


def test_update_honours_the_key(preflight_off, monkeypatch):
    called = []
    monkeypatch.setattr(update, "collect_llvm_state", lambda *a: called.append(a))
    sched = types.SimpleNamespace(cache=types.SimpleNamespace(all=lambda: {}))
    monkeypatch.setattr(update, "get_scheduler", lambda: sched)
    run = types.SimpleNamespace(args=argparse.Namespace(no_llvm_preflight=False),
                                pkgbase_map={"llvm": None}, config={})
    update._phase_llvm_preflight(run)
    assert called == []
