# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/bolt.py — BOLT inside the llvm build (3.5.0-F3).

The subprocess seams (``primitives.bolt.run_step``, the training runner) are
faked; what is pinned here is the orchestration: when the step runs at all,
what it refuses, that a success replaces the build-tree libraries keeping their
mtime and repackages once, and that any failure leaves the tree as built.
"""
import os
from pathlib import Path

import pytest

from sysforge.pipeline.stages.toolchain import bolt as tc_bolt
from sysforge.primitives import bolt as _bolt

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _bolt_opts,
    _toolchain_gates_clean,
    toolchain_cfg,
)

MTIME = 1_000_000


def _tree(tmp_path, monkeypatch, *, libs=("libclang-cpp.so.23.1",)):
    """A Pass-4a build tree with the in-tree BOLT tools and the target libs."""
    build = tmp_path / "builds/llvm/src/llvm-project-23.1.3.src/build"
    for d in ("bin", "lib"):
        (build / d).mkdir(parents=True)
    (build / "build.ninja").write_text("")
    for tool in ("llvm-bolt", "merge-fdata", "clang++"):
        (build / "bin" / tool).write_text("")
    (build / "lib" / "libbolt_rt_instr.a").write_text("")
    for name in libs:
        lib = build / "lib" / name
        lib.write_bytes(b"ORIGINAL")
        os.utime(lib, (MTIME, MTIME))
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "builds")
    monkeypatch.setattr(_bolt, "has_relocations", lambda p: True)
    monkeypatch.setattr(_bolt, "runtime_linkable", lambda p: True)
    obj = str(tmp_path / "x.o")  # the fake smoke step writes its -o; keep it out of cwd
    monkeypatch.setattr(_bolt, "training_commands",
                        lambda *a, **k: [["clang++", "-c", "x.cpp", "-o", obj]] * 4)
    monkeypatch.setattr(tc_bolt, "_store", lambda tcfg: tmp_path / "store")
    return build


def _fake_cycle(monkeypatch, *, train_ok=None, fail_step=None):
    """Fake llvm-bolt/merge-fdata/smoke: each writes its -o file; training
    drops one profile per instrumented library."""
    prefixes = []

    def run_step(argv, *, what, env=None, cwd=None):
        if fail_step and fail_step in what:
            raise _bolt.BoltError(f"{what} failed")
        if "-instrument" in argv:
            prefixes.append(next(a.split("=", 1)[1] for a in argv
                                 if a.startswith("--instrumentation-file=")))
        if "-o" in argv:
            out = Path(argv[argv.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"BOLTED" if "-data" in argv else b"x")

    def run_training(cmds, *, env, cwd):
        for p in prefixes:
            Path(f"{p}.4242.fdata").write_text("profile")
        prefixes.clear()  # one training round per optimize_in_build call
        return len(cmds) if train_ok is None else train_ok

    monkeypatch.setattr(_bolt, "run_step", run_step)
    monkeypatch.setattr(tc_bolt, "_run_training", run_training)


def _pgo_map(tmp_path):
    pb = tmp_path / "src/llvm/PKGBUILD"
    return {"llvm": pb, "llvm-libs": pb, "clang": pb}


def _repack(calls, *, raises=None):
    def repackage():
        calls.append(1)
        if raises:
            raise raises
    return repackage


def test_disabled_is_a_noop(tmp_path):
    calls = []
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(), _bolt_opts(), repackage=_repack(calls))
    assert calls == []


def test_dry_run_is_a_noop(tmp_path):
    calls = []
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}),
        _bolt_opts(dry_run=True), repackage=_repack(calls))
    assert calls == []


def test_needs_clang_from_the_pgo_pkgbuild(tmp_path, monkeypatch, capsys):
    """The old per-package layout builds clang outside the pgo passes."""
    from sysforge import log
    monkeypatch.setattr(log, "_VERBOSITY", 1)
    pb = tmp_path / "src/llvm/PKGBUILD"
    assert not tc_bolt.optimize_in_build(
        {"llvm": pb}, toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack([]))
    out, err = capsys.readouterr()
    assert "clang" in out + err


def test_missing_build_tree_skips(tmp_path, monkeypatch):
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "nothing")
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack([]))


def test_unlinkable_runtime_skips_and_leaves_the_tree(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(_bolt, "runtime_linkable", lambda p: False)
    _fake_cycle(monkeypatch)
    calls = []
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack(calls))
    assert (build / "lib/libclang-cpp.so.23.1").read_bytes() == b"ORIGINAL"
    assert calls == []


def test_library_without_relocations_skips(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch)
    monkeypatch.setattr(_bolt, "has_relocations", lambda p: False)
    _fake_cycle(monkeypatch)
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack([]))


def test_success_replaces_keeping_mtime_and_repackages_once(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    calls = []
    assert tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack(calls))
    lib = build / "lib/libclang-cpp.so.23.1"
    assert lib.read_bytes() == b"BOLTED"
    assert lib.stat().st_mtime == MTIME  # ninja must not relink it in package()
    assert calls == [1]


def test_libllvm_only_when_opted_in(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch, libs=("libclang-cpp.so.23.1", "libLLVM.so.23.1"))
    _fake_cycle(monkeypatch)
    tc_bolt.optimize_in_build(_pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}),
                              _bolt_opts(), repackage=_repack([]))
    assert (build / "lib/libLLVM.so.23.1").read_bytes() == b"ORIGINAL"
    tc_bolt.optimize_in_build(_pgo_map(tmp_path),
                              toolchain_cfg(bolt={"enabled": True, "libllvm": True}),
                              _bolt_opts(), repackage=_repack([]))
    assert (build / "lib/libLLVM.so.23.1").read_bytes() == b"BOLTED"


def test_mostly_failed_training_skips(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch, train_ok=1)  # 1 of 4 compiles succeeded
    calls = []
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack(calls))
    assert (build / "lib/libclang-cpp.so.23.1").read_bytes() == b"ORIGINAL"
    assert calls == []


def test_failed_smoke_test_leaves_the_tree(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch, fail_step="smoke")
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack([]))
    assert (build / "lib/libclang-cpp.so.23.1").read_bytes() == b"ORIGINAL"


def test_failed_repackage_restores_the_tree_and_stops_the_stage(tmp_path, monkeypatch):
    """The 4a packages may be half-rewritten: nothing is installed yet, so stop
    before install rather than ship them."""
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    with pytest.raises(RuntimeError, match="repackag"):
        tc_bolt.optimize_in_build(
            _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
            repackage=_repack([], raises=RuntimeError("makepkg -R failed")))
    lib = build / "lib/libclang-cpp.so.23.1"
    assert lib.read_bytes() == b"ORIGINAL" and lib.stat().st_mtime == MTIME


def test_bolt_config_defaults():
    b = toolchain_cfg().bolt
    assert (b.enabled, b.libllvm, b.training_workload) == (False, False, "")
