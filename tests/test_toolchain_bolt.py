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
import shutil
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
    monkeypatch.setattr(_bolt, "is_bolted", lambda p: p.read_bytes() == b"BOLTED")
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


def test_build_root_from_the_4a_build_finds_the_tree(tmp_path, monkeypatch):
    """3.5.0-B22: the system BUILDDIR points elsewhere, the build's own root
    holds the tree; BOLT runs on it instead of skipping."""
    build = _tree(tmp_path, monkeypatch)
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "tmp-makepkg")
    _fake_cycle(monkeypatch)
    calls = []
    assert tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack(calls), build_root=build.parents[2])
    assert calls == [1]


# ---------------------------------------------------------------------------
# 3.5.0-F7: originals kept, restored before a rerun; strict mode for --bolt-only
# ---------------------------------------------------------------------------

def test_success_keeps_the_original_beside_the_tree(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack([]))
    saved = build / "lib" / tc_bolt.PREBOLT_DIR / "libclang-cpp.so.23.1"
    assert saved.read_bytes() == b"ORIGINAL" and saved.stat().st_mtime == MTIME


def test_a_rerun_starts_from_the_original(tmp_path, monkeypatch):
    """BOLT must never rewrite its own output: the second run restores the
    saved original first, so it succeeds instead of refusing."""
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    inputs = []
    real = _bolt.instrument_cmd

    def spy(llvm_bolt, lib, *a, **k):
        inputs.append(lib.read_bytes())
        return real(llvm_bolt, lib, *a, **k)

    monkeypatch.setattr(_bolt, "instrument_cmd", spy)
    calls = []
    for _ in range(2):
        assert tc_bolt.optimize_in_build(
            _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
            repackage=_repack(calls))
    assert inputs == [b"ORIGINAL", b"ORIGINAL"]
    assert calls == [1, 1]
    lib = build / "lib/libclang-cpp.so.23.1"
    assert lib.read_bytes() == b"BOLTED" and lib.stat().st_mtime == MTIME


def test_a_bolted_library_without_its_original_is_refused(tmp_path, monkeypatch):
    build = _tree(tmp_path, monkeypatch)
    lib = build / "lib/libclang-cpp.so.23.1"
    lib.write_bytes(b"BOLTED")
    _fake_cycle(monkeypatch)
    calls = []
    assert not tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
        repackage=_repack(calls))
    assert lib.read_bytes() == b"BOLTED" and calls == []


def test_strict_mode_raises_instead_of_skipping(tmp_path, monkeypatch):
    """--bolt-only has nothing to install without BOLT, so a skip stops it."""
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "nothing")
    with pytest.raises(RuntimeError, match="no llvm build tree"):
        tc_bolt.optimize_in_build(
            _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
            repackage=_repack([]), strict=True)
    build = _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch, fail_step="smoke")
    with pytest.raises(RuntimeError, match="smoke"):
        tc_bolt.optimize_in_build(
            _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), _bolt_opts(),
            repackage=_repack([]), strict=True)
    assert (build / "lib/libclang-cpp.so.23.1").read_bytes() == b"ORIGINAL"


# ---------------------------------------------------------------------------
# 3.5.0-F7: --bolt-only refuses a tree that is not the last Pass-4a build
# ---------------------------------------------------------------------------

def _bolt_only_setup(tmp_path, monkeypatch, *, artifact_mtime=MTIME + 10, artifacts=True):
    build = _tree(tmp_path, monkeypatch)
    from sysforge.primitives import pacman
    monkeypatch.setattr(pacman, "get_builddir", lambda: tmp_path / "tmp-makepkg")
    pb = tmp_path / "src/llvm/PKGBUILD"
    pb.parent.mkdir(parents=True)
    pb.write_text("pkgbase=llvm\npkgname=('llvm' 'clang')\npkgver=23.1.3\npkgrel=3\n"
                  "arch=('x86_64')\n")
    arts = []
    if artifacts:
        art = tmp_path / "pkgdest/llvm-23.1.3-3-x86_64.pkg.tar.zst"
        art.parent.mkdir()
        art.write_text("")
        os.utime(art, (artifact_mtime, artifact_mtime))
        arts.append(art)
    from sysforge.build import makepkg_wrapper
    monkeypatch.setattr(makepkg_wrapper, "_artifacts_for_pkgbuild", lambda d: arts)
    entry = {"pkgver": "23.1.3", "pkgrel": "3", "epoch": "0",
             "toolchain_variant": "pgo_llvm",
             "flags_string": f"CC=clang\nBUILDDIR={tmp_path / 'builds'}"}
    return build, {"llvm": pb, "clang": pb}, entry


def test_bolt_only_tree_found_from_the_recorded_builddir(tmp_path, monkeypatch):
    build, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    assert tc_bolt.check_bolt_only_tree(pgo_map, entry, libllvm=False) == build.parents[2]


@pytest.mark.parametrize("change, match", [
    ({"toolchain_variant": "stock_llvm"}, "pgo_llvm"),
    ({"pkgver": "23.1.1"}, "23.1.1"),
    (None, "no record"),
])
def test_bolt_only_refuses_a_mismatched_record(tmp_path, monkeypatch, change, match):
    _, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    entry = None if change is None else {**entry, **change}
    with pytest.raises(RuntimeError, match=match):
        tc_bolt.check_bolt_only_tree(pgo_map, entry, libllvm=False)


def test_bolt_only_refuses_a_tree_without_relocations(tmp_path, monkeypatch):
    """Only Pass 4 links with --emit-relocs: a Pass 1-3 tree has none."""
    _, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(_bolt, "has_relocations", lambda p: False)
    with pytest.raises(RuntimeError, match="Pass-4a"):
        tc_bolt.check_bolt_only_tree(pgo_map, entry, libllvm=False)


@pytest.mark.parametrize("kw, match", [
    ({"artifact_mtime": MTIME - 10}, "older than"),
    ({"artifacts": False}, "no built llvm package"),
])
def test_bolt_only_refuses_artifacts_not_packaged_from_this_tree(
        tmp_path, monkeypatch, kw, match):
    _, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch, **kw)
    with pytest.raises(RuntimeError, match=match):
        tc_bolt.check_bolt_only_tree(pgo_map, entry, libllvm=False)


def test_bolt_only_refuses_a_missing_tree(tmp_path, monkeypatch):
    build, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    shutil.rmtree(build)
    with pytest.raises(RuntimeError, match="no llvm build tree"):
        tc_bolt.check_bolt_only_tree(pgo_map, entry, libllvm=False)


def test_last_pgo_build_check_names_the_flag_asking(tmp_path, monkeypatch):
    """3.5.0-F8: --install-only shares --bolt-only's record check; its refusal
    names the flag that asked."""
    from sysforge.pipeline.stages.toolchain import pkgbuilds
    _, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="--install-only: no record"):
        pkgbuilds.check_last_pgo_build(pgo_map, None, flag="--install-only", tag="TOOLCHAIN")
    assert pkgbuilds.check_last_pgo_build(
        pgo_map, entry, flag="--install-only", tag="TOOLCHAIN")[1:] == ("llvm", "23.1.3-3")


@pytest.mark.parametrize("flag", ["--install-only", "--bolt-only"])
def test_last_pgo_build_check_refuses_an_intermediate_pass(tmp_path, monkeypatch, flag):
    """3.5.0-B39: a record left by PGO pass 1, 2 or 3 (same version, built as
    pgo_llvm, recorded as INTERMEDIATE_VARIANT) is refused; Pass 4's passes."""
    from sysforge.pipeline.stages.toolchain import constants, pkgbuilds
    _, pgo_map, entry = _bolt_only_setup(tmp_path, monkeypatch)
    stopped = {**entry, "toolchain_variant": constants.INTERMEDIATE_VARIANT}
    with pytest.raises(RuntimeError, match="stopped before the final PGO pass"):
        pkgbuilds.check_last_pgo_build(pgo_map, stopped, flag=flag, tag="TOOLCHAIN")
    assert pkgbuilds.check_last_pgo_build(pgo_map, entry, flag=flag, tag="TOOLCHAIN")


# ---------------------------------------------------------------------------
# 3.5.0-B31: a BOLTed package is recorded as build_mode bolt_llvm
# ---------------------------------------------------------------------------

def _stamp_setup(tmp_path, monkeypatch):
    from sysforge.build import makepkg_wrapper
    from sysforge.primitives.build_state import BuildState
    arts = {tmp_path / "clang-23.1.3-3-x86_64.pkg.tar":
                ("clang", ["usr/", "usr/lib/libclang-cpp.so.23.1", "usr/bin/clang"]),
            tmp_path / "llvm-libs-23.1.3-3-x86_64.pkg.tar":
                ("llvm-libs", ["usr/lib/libLLVM.so.23.1"])}
    monkeypatch.setattr(makepkg_wrapper, "_artifacts_for_pkgbuild", lambda d: list(arts))
    monkeypatch.setattr(tc_bolt, "_package_contents", lambda a: arts[a])
    state = tmp_path / "state"
    bs = BuildState(state)
    for name in ("clang", "llvm-libs"):
        bs.record(pkgname=name, pkgver="23.1.3", pkgrel="3", epoch="0", pkgbase="llvm",
                  pkgbuild_dir=tmp_path / "src/llvm", build_mode="source_built")
    bs.save()
    opts = _bolt_opts()
    opts.state_dir = state
    return state, opts


def test_success_records_bolt_llvm_on_the_packages_it_rewrote(tmp_path, monkeypatch):
    from sysforge.primitives.build_state import BuildState
    _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    state, opts = _stamp_setup(tmp_path, monkeypatch)
    assert tc_bolt.optimize_in_build(
        _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), opts,
        repackage=_repack([]))
    bs = BuildState(state)
    assert bs.get("clang")["build_mode"] == _bolt.BUILD_MODE
    assert bs.get("llvm-libs")["build_mode"] == "source_built"  # libLLVM untouched


def test_failed_repackage_records_nothing(tmp_path, monkeypatch):
    from sysforge.primitives.build_state import BuildState
    _tree(tmp_path, monkeypatch)
    _fake_cycle(monkeypatch)
    state, opts = _stamp_setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="repackag"):
        tc_bolt.optimize_in_build(
            _pgo_map(tmp_path), toolchain_cfg(bolt={"enabled": True}), opts,
            repackage=_repack([], raises=RuntimeError("makepkg failed")))
    assert BuildState(state).get("clang")["build_mode"] == "source_built"
