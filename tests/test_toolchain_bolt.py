# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/bolt.py — BOLT Pass 5 gating.
"""
import pytest

from sysforge.pipeline.stages.toolchain.bolt import run_bolt
from sysforge.pipeline.stages.toolchain.config import bolt_config

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    toolchain_cfg,
    _bolt_opts,
    _toolchain_gates_clean,
)


def test_bolt_config_defaults():
    assert bolt_config({}) == {
        "enabled": False, "libllvm": False, "training_workload": "",
    }

def test_bolt_config_override():
    cfg = bolt_config({"bolt": {
        "enabled": True, "libllvm": True, "training_workload": "/t/w.cpp",
    }})
    assert cfg == {"enabled": True, "libllvm": True, "training_workload": "/t/w.cpp"}

def test_bolt_disabled_is_noop(monkeypatch):
    # [bolt] absent → returns before importing/calling any BOLT machinery.
    called = []
    monkeypatch.setattr(
        "sysforge.primitives.fs_provision._run_priv",
        lambda argv: called.append(argv),
    )
    run_bolt(toolchain_cfg(), {}, _bolt_opts(), "pgo_llvm")
    assert called == []

def test_bolt_dry_run_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(
        "sysforge.primitives.fs_provision._run_priv",
        lambda argv: called.append(argv),
    )
    run_bolt(toolchain_cfg(bolt={"enabled": True}), {}, _bolt_opts(dry_run=True), "pgo_llvm")
    assert called == []

def test_bolt_tool_build_fails_skips_rewrite(monkeypatch):
    # Enabled but the BOLT tools can't be built (Pass 5a fails) → no rewrite, the
    # verified PGO clang is left untouched (no privileged install).
    called = []
    # tools absent, and the build itself raises → build_bolt_tools returns False.
    monkeypatch.setattr(
        "sysforge.primitives.bolt.tools_available",
        lambda need_perf=False: (False, ["llvm-bolt"]),
    )
    monkeypatch.setattr(
        "sysforge.primitives.bolt.standalone_build_viable",
        lambda *a, **k: True,  # past the dylib-only BLOCKED guard
    )
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.verify.query_pacman_versions",
        lambda names: {"llvm": "22.1.8-4"},
    )
    monkeypatch.setattr(
        "sysforge.primitives.bolt.materialize_pkgbuild",
        lambda d, v: __import__("pathlib").Path(d) / "llvm-bolt" / "PKGBUILD",
    )
    def _boom(*a, **k):
        raise RuntimeError("build failed")
    monkeypatch.setattr("sysforge.pipeline.stages.toolchain.passes.build_pkg", _boom)
    monkeypatch.setattr(
        "sysforge.primitives.fs_provision._run_priv",
        lambda argv: called.append(argv),
    )
    cfg = {"paths": {"pkgbuild_src_dir": "/tmp"}}
    run_bolt(toolchain_cfg(bolt={"enabled": True}), cfg, _bolt_opts(), "pgo_llvm")
    assert called == []

def test_bolt_no_pkgbuild_src_dir_skips(monkeypatch):
    # Enabled, tools absent, but no pkgbuild_src_dir to materialize into → skip.
    monkeypatch.setattr(
        "sysforge.primitives.bolt.tools_available",
        lambda need_perf=False: (False, ["llvm-bolt"]),
    )
    monkeypatch.setattr(
        "sysforge.primitives.bolt.standalone_build_viable",
        lambda *a, **k: True,  # past the dylib-only BLOCKED guard
    )
    built = []
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.passes.build_pkg",
        lambda *a, **k: built.append(a),
    )
    run_bolt(toolchain_cfg(bolt={"enabled": True}), {"paths": {}}, _bolt_opts(), "pgo_llvm")
    assert built == []  # never attempted a build without a source dir

def test_bolt_store_resolves_from_toolchain_config(monkeypatch, tmp_path):
    # Pass 5b resolves its profile store from the parsed ToolchainConfig. The
    # resolver reads toolchain.toml keys (``profile_store``), so it must be handed
    # the raw table: the frozen dataclass has no ``.get`` and raised
    # AttributeError here once the tools and clang were present (3.2.0-F6).
    class _Stop(Exception):
        pass

    seen = []

    def _stop_at_store(path):
        seen.append(path)
        raise _Stop

    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.bolt.build_bolt_tools",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sysforge.primitives.bolt.tools_available",
        lambda need_perf=False: (True, []),
    )
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)
    monkeypatch.setattr(
        "sysforge.primitives.fs_provision.ensure_writable_dir", _stop_at_store,
    )
    tcfg = toolchain_cfg(bolt={"enabled": True}, profile_store=str(tmp_path))
    with pytest.raises(_Stop):
        run_bolt(tcfg, {}, _bolt_opts(), "pgo_llvm")
    assert seen == [tmp_path / "bolt"]

def test_bolt_dylib_only_llvm_is_blocked(monkeypatch):
    # Enabled, tools absent, but the host LLVM is dylib-only → the BLOCKED guard
    # short-circuits Pass 5 before materializing the PKGBUILD or building anything.
    monkeypatch.setattr(
        "sysforge.primitives.bolt.tools_available",
        lambda need_perf=False: (False, ["llvm-bolt"]),
    )
    monkeypatch.setattr(
        "sysforge.primitives.bolt.standalone_build_viable",
        lambda *a, **k: False,  # no per-component static archives on disk
    )
    materialized = []
    built = []
    monkeypatch.setattr(
        "sysforge.primitives.bolt.materialize_pkgbuild",
        lambda d, v: materialized.append((d, v)),
    )
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.passes.build_pkg",
        lambda *a, **k: built.append(a),
    )
    cfg = {"paths": {"pkgbuild_src_dir": "/tmp"}}
    run_bolt(toolchain_cfg(bolt={"enabled": True}), cfg, _bolt_opts(), "pgo_llvm")
    assert materialized == [] and built == []  # blocked before any work


def test_bolt_pass5a_installs_through_sysforge_not_makepkg(monkeypatch, tmp_path):
    """3.4.0-F1: makepkg builds llvm-bolt without --install; sysforge installs it."""
    from sysforge.pipeline.stages.toolchain import bolt as tc_bolt
    monkeypatch.setattr("sysforge.primitives.bolt.tools_available",
                        lambda need_perf=False: (False, ["llvm-bolt"]))
    monkeypatch.setattr("sysforge.primitives.bolt.standalone_build_viable", lambda *a, **k: True)
    monkeypatch.setattr("sysforge.primitives.bolt.materialize_pkgbuild",
                        lambda d, v: tmp_path / "llvm-bolt" / "PKGBUILD")
    monkeypatch.setattr(tc_bolt.verify, "query_pacman_versions", lambda names: {"llvm": "22.1.8-1"})
    events = []
    monkeypatch.setattr("sysforge.pipeline.stages.toolchain.passes.build_pkg",
                        lambda *a, **k: events.append(("build", k.get("extra_flags"))))
    monkeypatch.setattr("sysforge.pipeline.stages.toolchain.passes.install_built_packages",
                        lambda d, **k: events.append(("install", d, k)))
    tc_bolt.build_bolt_tools(toolchain_cfg(bolt={"enabled": True}),
                             {"paths": {"pkgbuild_src_dir": str(tmp_path)}},
                             _bolt_opts(), "pgo_llvm")
    assert events[0] == ("build", None)
    assert events[1] == ("install", tmp_path / "llvm-bolt", {"noconfirm": True})
