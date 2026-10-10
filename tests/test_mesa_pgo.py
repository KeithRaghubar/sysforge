# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/mesa_pgo.py — mesa instrumentation-PGO orchestration."""
import subprocess
from pathlib import Path

import pytest

from sysforge.primitives import mesa_pgo
from sysforge.primitives import run as run_seam


# ---------------------------------------------------------------------------
# Store resolution + flag values
# ---------------------------------------------------------------------------

def test_store_is_pgo_mesa_method_subdir(monkeypatch):
    # Default root, no override: <profile_store_root>/pgo-mesa. Opts out of
    # conftest's store isolation (path only, no I/O).
    monkeypatch.delenv("SYSFORGE_PROFILE_STORE", raising=False)
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


def test_merge_no_profraw_reuses_existing_profdata(tmp_path, monkeypatch):
    # A re-run of `--pgo=use` after the raw was already merged+pruned (e.g. the
    # consuming build failed downstream) must NOT hard-abort: the merged
    # profdata is the durable, consumable artifact. Return it as-is without
    # invoking llvm-profdata.
    out = tmp_path / mesa_pgo.PROFDATA_NAME
    out.write_text("merged-already")

    def explode(*a, **kw):  # llvm-profdata must not be touched
        raise AssertionError("merge invoked despite an existing profdata + no raw")

    monkeypatch.setattr(run_seam.subprocess, "run", explode)
    assert mesa_pgo.merge_profraw(tmp_path) == out
    assert out.read_text() == "merged-already"


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

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)
    out = mesa_pgo.merge_profraw(tmp_path)
    assert out == tmp_path / mesa_pgo.PROFDATA_NAME
    assert out.read_text() == "merged"
    assert calls["argv"][0] == "llvm-profdata"
    assert "merge" in calls["argv"]
    # Both profraw inputs passed.
    assert sum(a.endswith(".profraw") for a in calls["argv"]) == 2


def test_merge_prunes_profraw_after_success(tmp_path, monkeypatch):
    # Storage-leak guard (Q5): a successful merge must delete the raw .profraw
    # it consumed, mirroring the toolchain stage's delete-on-merge. Otherwise
    # every record→use cycle leaves its raw behind and the store grows forever.
    raws = [tmp_path / "a.profraw", tmp_path / "b.profraw"]
    for r in raws:
        r.write_text("raw")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")

    def fake_run(argv, **kw):
        out_idx = argv.index("--output") + 1
        Path(argv[out_idx]).write_text("merged")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)
    out = mesa_pgo.merge_profraw(tmp_path)
    assert out.read_text() == "merged"
    # Raw inputs gone; only the merged profdata remains.
    assert mesa_pgo.list_profraw(tmp_path) == []
    for r in raws:
        assert not r.exists()


def test_merge_folds_existing_profdata(tmp_path, monkeypatch):
    # Pruning the raw is only safe if the accumulated signal lives in the
    # profdata — so a re-merge must pass the existing profdata as an input
    # (cumulative, like the toolchain), not start from scratch.
    out = tmp_path / mesa_pgo.PROFDATA_NAME
    out.write_text("prior")
    (tmp_path / "new.profraw").write_text("raw")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")

    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        out_idx = argv.index("--output") + 1
        Path(argv[out_idx]).write_text("remerged")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)
    mesa_pgo.merge_profraw(tmp_path)
    # The prior profdata path is among the merge *inputs* (not just the
    # --output target), so accumulated signal survives the raw being pruned.
    argv = calls["argv"]
    out_idx = argv.index("--output") + 1
    inputs = [a for i, a in enumerate(argv) if i not in (0, out_idx - 1, out_idx)]
    assert any(a.endswith(mesa_pgo.PROFDATA_NAME) for a in inputs)
    assert out.read_text() == "remerged"


def test_merge_tool_failure_raises_with_stderr(tmp_path, monkeypatch):
    (tmp_path / "a.profraw").write_text("x")
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")
    monkeypatch.setattr(
        run_seam.subprocess,
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

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)
    out = mesa_pgo.merge_profraw(store, pkgbase="foo")
    assert out == store / "foo.profdata"


# ---------------------------------------------------------------------------
# 3.2.0-B16 — collected-version sidecar next to the merged profile
# ---------------------------------------------------------------------------

def _fake_merge(monkeypatch):
    monkeypatch.setattr(mesa_pgo.shutil, "which", lambda _t: "/usr/bin/llvm-profdata")

    def fake_run(argv, **kw):
        Path(argv[argv.index("--output") + 1]).write_text("merged")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_seam.subprocess, "run", fake_run)


def test_merge_records_the_collected_version_sidecar(tmp_path, monkeypatch):
    (tmp_path / "a.profraw").write_text("x")
    _fake_merge(monkeypatch)
    out = mesa_pgo.merge_profraw(tmp_path, collected_version="1:26.1.3-2")
    assert mesa_pgo.version_sidecar(out).read_text().strip() == "1:26.1.3-2"


def test_profile_verdict_current_when_sidecar_matches_target(tmp_path):
    pd = tmp_path / "mesa.profdata"
    pd.write_text("merged")
    mesa_pgo.version_sidecar(pd).write_text("1:26.1.7-1\n")
    status, msg = mesa_pgo.profile_version_verdict(pd, "1:26.1.7-1")
    assert (status, msg) == ("current", None)


def test_profile_verdict_older_names_both_versions(tmp_path):
    pd = tmp_path / "mesa.profdata"
    pd.write_text("merged")
    mesa_pgo.version_sidecar(pd).write_text("1:26.1.3-2\n")
    status, msg = mesa_pgo.profile_version_verdict(pd, "1:26.1.7-1")
    assert status == "older"
    assert "1:26.1.3-2" in msg and "1:26.1.7-1" in msg
    assert "--pgo=record" in msg


def test_profile_verdict_unknown_age_without_sidecar(tmp_path):
    """A profile collected before the sidecar existed is kept and reused —
    never discarded, never presented as current."""
    pd = tmp_path / "mesa.profdata"
    pd.write_text("merged")
    status, msg = mesa_pgo.profile_version_verdict(pd, "1:26.1.7-1")
    assert status == "unknown"
    assert "unknown" in msg and "--pgo=record" in msg


def test_reuse_notice_is_plain_when_profile_is_current(tmp_path):
    pd = tmp_path / "mesa.profdata"
    pd.write_text("merged")
    mesa_pgo.version_sidecar(pd).write_text("1:26.1.7-1\n")
    line = mesa_pgo.reuse_notice(pd, "mesa", "1:26.1.7-1")
    assert "re-applying" in line and "collected against" not in line


def test_reuse_notice_names_the_staleness(tmp_path):
    pd = tmp_path / "mesa.profdata"
    pd.write_text("merged")
    mesa_pgo.version_sidecar(pd).write_text("1:26.1.3-2\n")
    line = mesa_pgo.reuse_notice(pd, "mesa", "1:26.1.7-1")
    assert "re-applying" in line
    assert "collected against 1:26.1.3-2, building 1:26.1.7-1" in line


# Verbatim clang 23.1.1 output for a stale IR-PGO function.
_MISMATCH = ("warning: src/gallium/drivers/llvmpipe/lp_rast.c: function control flow "
             "change detected (hash mismatch) lp_rast_triangle Hash = 382993475055910911 "
             "up to 0 count discarded [-Wbackend-plugin]")


@pytest.fixture(autouse=True)
def _clean_skew_state():
    mesa_pgo.reset_skew_session()
    yield
    mesa_pgo.reset_skew_session()


def test_use_flags_has_no_inert_suppressions(tmp_path):
    assert mesa_pgo.use_flags(tmp_path / "mesa.profdata") == \
        f"-fprofile-use={tmp_path / 'mesa.profdata'}"


def test_observe_counts_unique_file_function_pairs():
    mesa_pgo.arm_skew_count()
    mesa_pgo.observe_line(_MISMATCH)
    mesa_pgo.observe_line(_MISMATCH)  # same TU compiled twice / ccache replay
    mesa_pgo.observe_line(_MISMATCH.replace("lp_rast_triangle", "lp_rast_clear"))
    mesa_pgo.observe_line("error: " + _MISMATCH.split("warning: ", 1)[1])
    mesa_pgo.observe_line("[42/900] Compiling C object foo.o")
    assert mesa_pgo.take_skew_count() == {
        ("src/gallium/drivers/llvmpipe/lp_rast.c", "lp_rast_triangle"),
        ("src/gallium/drivers/llvmpipe/lp_rast.c", "lp_rast_clear"),
    }


def test_observe_is_noop_unarmed():
    mesa_pgo.observe_line(_MISMATCH)
    assert mesa_pgo.take_skew_count() is None


def test_take_disarms():
    mesa_pgo.arm_skew_count()
    assert mesa_pgo.take_skew_count() == set()
    mesa_pgo.observe_line(_MISMATCH)
    assert mesa_pgo.take_skew_count() is None


def test_profile_function_total_parses(monkeypatch, tmp_path):
    out = ("Instrumentation level: IR  entry_first = 0\n"
           "Total functions: 18210\nMaximum function count: 9\n")
    monkeypatch.setattr(mesa_pgo.run, "capture",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out))
    assert mesa_pgo.profile_function_total(tmp_path / "m.profdata") == 18210


@pytest.mark.parametrize("result", [
    None,
    subprocess.CompletedProcess([], 1, stdout=""),
    subprocess.CompletedProcess([], 0, stdout="garbage\n"),
])
def test_profile_function_total_none_when_unavailable(monkeypatch, tmp_path, result):
    monkeypatch.setattr(mesa_pgo.run, "capture", lambda cmd, **kw: result)
    assert mesa_pgo.profile_function_total(tmp_path / "m.profdata") is None


def test_staleness_line_below_threshold():
    r = mesa_pgo.SkewReport("mesa", 412, 18210)
    assert mesa_pgo.staleness_line(r, 0.5) == \
        "PGO profile mesa: 412 of 18,210 profiled functions no longer match the source (2.3%)"
    assert not mesa_pgo.is_stale(r, 0.5)


def test_staleness_line_at_threshold_points_at_record():
    r = mesa_pgo.SkewReport("mesa", 50, 100)
    line = mesa_pgo.staleness_line(r, 0.5)
    assert mesa_pgo.is_stale(r, 0.5)
    assert line.endswith("profile is stale; refresh with `sysforge build mesa --pgo=record`")


def test_staleness_line_without_total_is_never_stale():
    r = mesa_pgo.SkewReport("mesa", 412, None)
    assert mesa_pgo.staleness_line(r, 0.5) == \
        "PGO profile mesa: 412 profiled functions no longer match the source"
    assert not mesa_pgo.is_stale(r, 0.5)


def test_skew_reports_drain():
    mesa_pgo.record_skew_report(mesa_pgo.SkewReport("mesa", 1, 2))
    assert [r.pkgbase for r in mesa_pgo.take_skew_reports()] == ["mesa"]
    assert mesa_pgo.take_skew_reports() == []
