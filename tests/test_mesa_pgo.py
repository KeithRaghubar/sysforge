# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/mesa_pgo.py — mesa instrumentation-PGO orchestration."""
import subprocess
from pathlib import Path

import pytest

from sysforge.primitives import mesa_pgo


# ---------------------------------------------------------------------------
# Store resolution + flag values
# ---------------------------------------------------------------------------

def test_store_is_pgo_mesa_method_subdir():
    # Default root, no override: <profile_store_root>/pgo-mesa
    store = mesa_pgo.resolve_store({})
    assert store.name == "pgo-mesa"
    assert store.parent.name == "sysforge"


def test_store_honours_profile_store_override(tmp_path):
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)})
    assert store == tmp_path / "pgo-mesa"


def test_profdata_path_under_store():
    assert mesa_pgo.profdata_path({}).name == mesa_pgo.PROFDATA_NAME
    assert mesa_pgo.profdata_path({}).parent == mesa_pgo.resolve_store({})


def test_generate_flag_bakes_store_path(tmp_path):
    assert mesa_pgo.generate_flag(tmp_path) == f"-fprofile-generate={tmp_path}"


def test_use_flags_consume_profile_and_demote_skew_warnings(tmp_path):
    pd = tmp_path / "mesa.profdata"
    flags = mesa_pgo.use_flags(pd)
    assert f"-fprofile-use={pd}" in flags
    # The instrumentation-vs-source skew warnings must be demoted so a -Werror
    # mesa doesn't fail on an expected slightly-stale profile.
    assert "-Wno-profile-instr-out-of-date" in flags
    assert "-Wno-profile-instr-unprofiled" in flags


def test_build_mode_is_optimized():
    from sysforge.primitives.profile import is_optimized_build_mode

    assert mesa_pgo.BUILD_MODE == "pgo_mesa"
    assert is_optimized_build_mode(mesa_pgo.BUILD_MODE)


# ---------------------------------------------------------------------------
# list_profraw
# ---------------------------------------------------------------------------

def test_list_profraw_empty_when_dir_absent(tmp_path):
    assert mesa_pgo.list_profraw(tmp_path / "nope") == []


def test_list_profraw_finds_nested(tmp_path):
    (tmp_path / "a.profraw").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.profraw").write_text("y")
    (tmp_path / "ignore.txt").write_text("z")
    found = {p.name for p in mesa_pgo.list_profraw(tmp_path)}
    assert found == {"a.profraw", "b.profraw"}


# ---------------------------------------------------------------------------
# merge_profraw
# ---------------------------------------------------------------------------

def test_merge_no_profraw_raises_actionable(tmp_path):
    with pytest.raises(mesa_pgo.MesaPgoError) as exc:
        mesa_pgo.merge_profraw(tmp_path)
    assert "--pgo=record" in str(exc.value)


def test_merge_missing_tool_raises(tmp_path, monkeypatch):
    (tmp_path / "a.profraw").write_text("x")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: None)
    with pytest.raises(mesa_pgo.MesaPgoError) as exc:
        mesa_pgo.merge_profraw(tmp_path)
    assert "llvm-profdata" in str(exc.value)


def test_merge_success_invokes_llvm_profdata(tmp_path, monkeypatch):
    (tmp_path / "a.profraw").write_text("x")
    (tmp_path / "b.profraw").write_text("y")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")

    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        # Simulate llvm-profdata writing the output file.
        out_idx = argv.index("--output") + 1
        Path(argv[out_idx]).write_text("merged")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mesa_pgo.subprocess, "run", fake_run)
    out = mesa_pgo.merge_profraw(tmp_path)
    assert out == tmp_path / mesa_pgo.PROFDATA_NAME
    assert out.read_text() == "merged"
    assert calls["argv"][0] == "llvm-profdata"
    assert "merge" in calls["argv"]
    # Both profraw inputs passed.
    assert sum(a.endswith(".profraw") for a in calls["argv"]) == 2


def test_merge_tool_failure_raises_with_stderr(tmp_path, monkeypatch):
    (tmp_path / "a.profraw").write_text("x")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")
    monkeypatch.setattr(
        mesa_pgo.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "corrupt profile"),
    )
    with pytest.raises(mesa_pgo.MesaPgoError) as exc:
        mesa_pgo.merge_profraw(tmp_path)
    assert "corrupt profile" in str(exc.value)


# ---------------------------------------------------------------------------
# reuse_profdata — durability across plain `build mesa` / `update` rebuilds
# ---------------------------------------------------------------------------

def test_reuse_profdata_none_when_never_collected(tmp_path):
    # No prior --pgo=use ⇒ no merged profile ⇒ a plain rebuild stays stock.
    assert mesa_pgo.reuse_profdata({"profile_store": str(tmp_path)}) is None


def test_reuse_profdata_returns_existing_merged_profile(tmp_path):
    # A prior `build mesa --pgo=use` left a merged mesa.profdata in the store;
    # a subsequent source rebuild reuses it instead of producing stock mesa.
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)})
    store.mkdir(parents=True)
    pd = store / mesa_pgo.PROFDATA_NAME
    pd.write_text("merged")
    assert mesa_pgo.reuse_profdata({"profile_store": str(tmp_path)}) == pd


def test_reuse_profdata_ignores_bare_profraw(tmp_path):
    # record-only (profraw present, never merged) must NOT auto-reuse — there is
    # no consumable profdata yet, so the rebuild falls back to a normal build.
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)})
    store.mkdir(parents=True)
    (store / "a.profraw").write_text("raw")
    assert mesa_pgo.reuse_profdata({"profile_store": str(tmp_path)}) is None


# ---------------------------------------------------------------------------
# Generalization beyond mesa (F5): per-package stores + build_mode
# ---------------------------------------------------------------------------

def test_store_is_pgo_mesa_for_mesa_family():
    # mesa-family keeps the back-compat <root>/pgo-mesa store (no target subdir),
    # so existing collected mesa profiles are never orphaned.
    for pkgbase in ("mesa", "mesa-git", "lib32-mesa"):
        store = mesa_pgo.resolve_store({}, pkgbase=pkgbase)
        assert store.name == "pgo-mesa", pkgbase


def test_store_is_per_package_for_non_mesa(tmp_path):
    # A non-mesa target gets its own namespaced store under the generic `pgo`
    # method: <root>/pgo/<pkgbase>.
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)}, pkgbase="foo")
    assert store == tmp_path / "pgo" / "foo"


def test_profdata_name_tracks_pkgbase(tmp_path):
    # mesa keeps mesa.profdata (== the pkgbase pattern); a generic package gets
    # <pkgbase>.profdata inside its own store.
    assert mesa_pgo.profdata_path({}, pkgbase="mesa").name == "mesa.profdata"
    pd = mesa_pgo.profdata_path({"profile_store": str(tmp_path)}, pkgbase="foo")
    assert pd == tmp_path / "pgo" / "foo" / "foo.profdata"


def test_build_mode_for_mesa_vs_generic():
    from sysforge.primitives.profile import is_optimized_build_mode

    assert mesa_pgo.build_mode_for("mesa") == "pgo_mesa"
    assert mesa_pgo.build_mode_for("foo") == "pgo"
    # Both earn the -sysforge rename.
    assert is_optimized_build_mode(mesa_pgo.build_mode_for("mesa"))
    assert is_optimized_build_mode(mesa_pgo.build_mode_for("foo"))


def test_reuse_profdata_per_package(tmp_path):
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)}, pkgbase="foo")
    store.mkdir(parents=True)
    pd = store / "foo.profdata"
    pd.write_text("merged")
    assert mesa_pgo.reuse_profdata(
        {"profile_store": str(tmp_path)}, pkgbase="foo"
    ) == pd
    # A different package with no profile is unaffected.
    assert mesa_pgo.reuse_profdata(
        {"profile_store": str(tmp_path)}, pkgbase="bar"
    ) is None


def test_merge_profraw_names_output_per_package(tmp_path, monkeypatch):
    store = mesa_pgo.resolve_store({"profile_store": str(tmp_path)}, pkgbase="foo")
    store.mkdir(parents=True)
    (store / "a.profraw").write_text("x")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")

    def fake_run(argv, **kw):
        out_idx = argv.index("--output") + 1
        Path(argv[out_idx]).write_text("merged")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mesa_pgo.subprocess, "run", fake_run)
    out = mesa_pgo.merge_profraw(store, pkgbase="foo")
    assert out == store / "foo.profdata"
