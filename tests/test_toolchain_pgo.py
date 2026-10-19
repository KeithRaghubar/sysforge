# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/pgo.py — the four-pass PGO sequence.
"""
from pathlib import Path
from sysforge.pipeline.stages.toolchain.constants import PGO_PROFDATA_MIN_BYTES
from sysforge.pipeline.stages.toolchain.pgo import PGOAborted
from sysforge.pipeline.stages.toolchain.pgo import build_llvm_pgo_inner
from sysforge.pipeline.stages.toolchain.pgo import pgo_confirm
from unittest.mock import MagicMock
from unittest.mock import patch
import os
import pytest

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _bootstrap_calls,
    _build_calls,
    _confirm_options,
    _instrument_calls,
    _run_pgo,
    _single_pass_setup,
    _toolchain_gates_clean,
    _train_calls,
    make_options,
    make_pkgbuild,
)


def test_reuse_path_installs_every_pkgbuilds_repo_deps_before_building(
        tmp_path, monkeypatch):
    """3.5.0-B15: reusing profdata skips Pass 1, the only pass that installed
    missing repo build deps, so the merged llvm PKGBUILD's new makedepends
    (wasi-libc) were never installed and 4a died in the wasm libc++ build.
    One up-front install covers every PKGBUILD of the run, never a suite name."""
    from sysforge import build_core
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    spirv = make_pkgbuild(builds, "spirv-llvm-translator")
    lib32 = make_pkgbuild(builds, "lib32-llvm")
    pgo_map = {"llvm": llvm, "llvm-libs": llvm, "clang": llvm}
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = pgo_store / "clang.profdata"
    profdata.write_bytes(b"fake")
    order = []
    monkeypatch.setattr(
        build_core, "install_missing_repo_deps",
        lambda paths, **k: order.append(("deps", sorted(paths), k.get("exclude"))))

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "profdata.check_existing_profdata", return_value=("ready", str(profdata))), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "passes.build_pass",
               side_effect=lambda label, m, o, **kw: order.append(("build", label)) or {}), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(
            pgo_map, {"spirv-llvm-translator": spirv}, {"lib32-llvm": lib32},
            tmp_path / "s1", tmp_path / "s2", tmp_path / "s3", pgo_store,
            make_options(dry_run=False, rebuild_profdata=False,
                         state_dir=tmp_path / "state"),
        )

    assert order[0] == ("deps", sorted([llvm, spirv, lib32]), frozenset(
        {"llvm", "llvm-libs", "clang", "spirv-llvm-translator", "lib32-llvm"}))
    assert [k for k, *_ in order].count("deps") == 1
    assert order[1][0] == "build"


def test_build_non_pgo_links_against_staged_optimized_libllvm_reuse(tmp_path):
    """Regression: in the profdata-reuse fast path, the non-pgo suite (clang, …)
    must build against the freshly-built OPTIMIZED libLLVM staged in stage3, NOT
    the live /usr libLLVM. Otherwise libclang-cpp records `_ZNSt*@LLVM_<ver>`
    against a libLLVM that the -fprofile-use build no longer exports (it inlines
    the stdlib symbol away), bricking the live clang at the first symbol lookup —
    the exact failure this split exists to prevent.
    """
    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang")}
    staging1, staging, staging3 = (
        tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3"
    )
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = pgo_store / "clang.profdata"
    profdata.write_bytes(b"fake")

    options = make_options(
        dry_run=False, rebuild_profdata=False, state_dir=tmp_path / "state"
    )

    calls = []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        calls.append({
            "pkgs": set(pkgbuild_map.keys()),
            "cfe": kw.get("compiler_flags_extra"),
            "env": dict(kw.get("pgo_env") or {}),
            "owner_stage": kw.get("owner_stage"),
            "cc": kw.get("cc"),
            "cmake_llvm_dir": kw.get("cmake_llvm_dir"),
        })
        return {}  # real build_pass returns {pkgbase: fingerprint}

    extract_mock = MagicMock()
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "profdata.check_existing_profdata", return_value=("ready", str(profdata))), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch(T + "profdata.extract_built_to_staging", extract_mock), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(
            pgo_map, non_pgo_map, {},
            staging1, staging, staging3, pgo_store, options,
        )

    # Exactly two sub-passes: 4a (pgo) then 4b (non_pgo). No 4c (lib32 empty).
    assert len(calls) == 2
    pgo_call, non_pgo_call = calls[0], calls[1]
    assert pgo_call["pkgs"] == {"llvm"}
    assert non_pgo_call["pkgs"] == {"clang"}

    # 4a builds llvm itself — no find_package(LLVM) redirect on the reuse path
    # (system clang, no staged cc).
    assert "CMAKE_PREFIX_PATH" not in pgo_call["env"]

    # 4b — the fix: clang/lld link against the staged OPTIMIZED libLLVM.
    assert non_pgo_call["env"]["CMAKE_PREFIX_PATH"] == f"{staging3}/usr"
    assert "LD_LIBRARY_PATH" not in non_pgo_call["env"], (
        "host clang compiles the source; it must NOT be forced to LOAD the "
        "staged libLLVM (mirror Pass 2)"
    )
    assert non_pgo_call["env"]["LLVM_PROFILE_FILE"] == ""
    # 4b also forces -DLLVM_DIR at the staged cmake config so find_package(LLVM)
    # cannot fall back to /usr (env CMAKE_PREFIX_PATH alone was losing to /usr).
    assert non_pgo_call["cmake_llvm_dir"] == f"{staging3}/usr/lib/cmake/llvm"
    # 4a builds llvm itself — no LLVM_DIR steering (it IS the libLLVM).
    assert pgo_call["cmake_llvm_dir"] is None

    for c in calls:
        assert c["owner_stage"] == "toolchain"
        assert c["cfe"] == f"-fprofile-use={profdata}"

    # The optimized libLLVM was staged into stage3 *before* clang built.
    extract_mock.assert_any_call(pgo_map, staging3, False)

def test_build_non_pgo_links_against_staged_optimized_libllvm_full(tmp_path):
    """Same coherence guarantee on the full 4-pass path (rebuild_profdata=True):
    Pass 4b builds the non-pgo suite against stage3's optimized libLLVM, after a
    pgo sub-pass (4a) optimized llvm/llvm-libs with -fprofile-use.
    """
    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang")}
    staging1, staging, staging3 = (
        tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3"
    )
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    # merge_profraw returns this; kept OUTSIDE pgo_store (the fresh-build purge
    # wipes pgo_store) and sized past PGO_PROFDATA_MIN_BYTES so the
    # "suspicious profdata" prompt does not fire.
    profdata = tmp_path / "clang.profdata"
    profdata.write_bytes(b"x" * (PGO_PROFDATA_MIN_BYTES + 1))

    options = make_options(
        dry_run=False, rebuild_profdata=True, state_dir=tmp_path / "state"
    )

    calls = []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        calls.append({
            "pkgs": set(pkgbuild_map.keys()),
            "cfe": kw.get("compiler_flags_extra"),
            "env": dict(kw.get("pgo_env") or {}),
            "cmake_llvm_dir": kw.get("cmake_llvm_dir"),
            "cc": kw.get("cc"),
            "cxx": kw.get("cxx"),
        })
        return {}  # real build_pass returns {pkgbase: fingerprint}

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "profdata.pgo_stage_instrumented"), \
         patch(T + "profdata.profile_runtime_ldflag", return_value=None), \
         patch(T + "profdata.profraw_merge_daemon"), \
         patch(T + "profdata.merge_profraw", return_value=profdata), \
         patch(T + "profdata.write_profdata_version"), \
         patch(T + "pgo.fs_provision.ensure_writable_dir"), \
         patch(T + "pgo.fs_provision.empty_dir_contents"), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(
            pgo_map, non_pgo_map, {},
            staging1, staging, staging3, pgo_store, options,
        )

    # Pass 4b is the sub-pass steered at stage3.
    build_nonpgo = [
        c for c in calls
        if c["env"].get("CMAKE_PREFIX_PATH") == f"{staging3}/usr"
    ]
    assert len(build_nonpgo) == 1
    assert build_nonpgo[0]["pkgs"] == {"clang"}
    assert "LD_LIBRARY_PATH" not in build_nonpgo[0]["env"]
    assert build_nonpgo[0]["cfe"] == f"-fprofile-use={profdata}"
    assert build_nonpgo[0]["cmake_llvm_dir"] == f"{staging3}/usr/lib/cmake/llvm"

    # Pass 2 (bootstrap clang against stage1) is also LLVM_DIR-steered, at
    # stage1's cmake config — so the training clang links stage1's libLLVM too.
    # (Pass 3 shares CMAKE_PREFIX_PATH=stage1 via stage_env but is the mixed
    # pgo+non_pgo training pass and gets NO -DLLVM_DIR, so filter on it.)
    bootstrap = [
        c for c in calls
        if c["cmake_llvm_dir"] == f"{staging1}/usr/lib/cmake/llvm"
    ]
    assert len(bootstrap) == 1
    assert bootstrap[0]["pkgs"] == {"clang"}
    # The mixed Pass-3 training pass is NOT LLVM_DIR-steered.
    train = [c for c in calls if c["pkgs"] == {"clang", "llvm"}]
    assert train and all(c["cmake_llvm_dir"] is None for c in train)

    # A pgo sub-pass (4a) optimized llvm with -fprofile-use beforehand (Pass 1
    # also touches llvm but with -fprofile-generate, so filter on the flag).
    pgo_opt = [
        c for c in calls
        if c["pkgs"] == {"llvm"} and (c["cfe"] or "").startswith("-fprofile-use=")
    ]
    assert pgo_opt, "Pass 4a must optimize llvm/llvm-libs with -fprofile-use"

    # Pass 1 (instrument) MUST bootstrap on the live system clang, never the
    # resolved profile's CC=gcc — gcc + -fprofile-generate produces gcov
    # __gcov_* refs in stage1's .a archives that the clang profile runtime
    # cannot satisfy, bricking the Pass 2 link.
    instrument = [
        c for c in calls
        if c["pkgs"] == {"llvm"}
        and (c["cfe"] or "").startswith("-fprofile-generate=")
    ]
    assert len(instrument) == 1, "Pass 1 must instrument llvm with -fprofile-generate"
    assert instrument[0]["cc"] == "/usr/bin/clang"
    assert instrument[0]["cxx"] == "/usr/bin/clang++"

def test_train_corpus_enrichment_compiles_extras_into_profile(tmp_path):
    """A non-empty corpus_map compiles the extra targets (mesa) in Pass 3 with
    the SAME instrumented LLVM_PROFILE_FILE so their codegen profraw merges into
    clang.profdata — never installed, never -fprofile-use."""
    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang")}
    corpus_map = {"mesa": make_pkgbuild(builds, "mesa")}
    staging1, staging, staging3 = (
        tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3"
    )
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = tmp_path / "clang.profdata"
    profdata.write_bytes(b"x" * (PGO_PROFDATA_MIN_BYTES + 1))

    options = make_options(
        dry_run=False, rebuild_profdata=True, state_dir=tmp_path / "state"
    )

    calls = []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        calls.append({
            "label": label,
            "pkgs": set(pkgbuild_map.keys()),
            "env": dict(kw.get("pgo_env") or {}),
            "install": kw.get("install"),
            "cfe": kw.get("compiler_flags_extra"),
            "lfe": kw.get("linker_flags_extra"),
            "pgo_reuse": kw.get("pgo_reuse", True),
        })
        return {}

    rt_flag = "-Wl,--push-state,--whole-archive /rt/libclang_rt.profile-x86_64.a -Wl,--pop-state"
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "profdata.pgo_stage_instrumented"), \
         patch(T + "profdata.profile_runtime_ldflag", return_value=rt_flag), \
         patch(T + "profdata.profraw_merge_daemon"), \
         patch(T + "profdata.merge_profraw", return_value=profdata), \
         patch(T + "profdata.write_profdata_version"), \
         patch(T + "pgo.fs_provision.ensure_writable_dir"), \
         patch(T + "pgo.fs_provision.empty_dir_contents"), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(
            pgo_map, non_pgo_map, {},
            staging1, staging, staging3, pgo_store, options,
            corpus_map=corpus_map,
        )

    corpus_calls = [c for c in calls if c["pkgs"] == {"mesa"}]
    assert len(corpus_calls) == 1, "mesa corpus must be compiled exactly once"
    cc = corpus_calls[0]
    # Same instrumented profraw target as the Pass-3 LLVM training build, so it
    # merges into the one clang.profdata.
    assert cc["env"]["LLVM_PROFILE_FILE"] == f"{pgo_store}/default_%m_%p.profraw"
    assert cc["env"]["CCACHE_DISABLE"] == "1"
    # Never installed; never an -fprofile-use target (corpus, not consumer).
    assert cc["install"] is False
    assert cc["cfe"] is None
    # 3.2.0-B25: the static-archive profile-runtime force-load stays on the
    # LLVM passes only — meson doubles LDFLAGS, and a doubled --whole-archive
    # is a duplicate-symbol link failure (mesa "cannot compile programs").
    assert cc["lfe"] is None
    train = [c for c in calls if c["pkgs"] == {"llvm", "clang"}]
    assert train and train[0]["lfe"] == rt_flag
    # 3.2.0-B28: the target's own prior --pgo=use profile is not re-applied.
    assert cc["pgo_reuse"] is False

def test_train_corpus_enrichment_failure_is_non_fatal(tmp_path):
    """A corpus build that raises must NOT abort the PGO run — the toolchain
    proceeds to Pass 4 with whatever LLVM-only profraw was collected."""
    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang")}
    corpus_map = {"mesa": make_pkgbuild(builds, "mesa")}
    staging1, staging, staging3 = (
        tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3"
    )
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = tmp_path / "clang.profdata"
    profdata.write_bytes(b"x" * (PGO_PROFDATA_MIN_BYTES + 1))

    options = make_options(
        dry_run=False, rebuild_profdata=True, state_dir=tmp_path / "state"
    )

    seen = []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        seen.append(set(pkgbuild_map.keys()))
        if set(pkgbuild_map.keys()) == {"mesa"}:
            raise RuntimeError("missing makedepend under --nodeps")
        return {}

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "profdata.pgo_stage_instrumented"), \
         patch(T + "profdata.profile_runtime_ldflag", return_value=None), \
         patch(T + "profdata.profraw_merge_daemon"), \
         patch(T + "profdata.merge_profraw", return_value=profdata), \
         patch(T + "profdata.write_profdata_version"), \
         patch(T + "pgo.fs_provision.ensure_writable_dir"), \
         patch(T + "pgo.fs_provision.empty_dir_contents"), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        # Must not raise despite the mesa corpus build failing.
        build_llvm_pgo_inner(
            pgo_map, non_pgo_map, {},
            staging1, staging, staging3, pgo_store, options,
            corpus_map=corpus_map,
        )

    # Pass 4 still ran after the failed corpus build (llvm re-optimized).
    assert {"mesa"} in seen
    assert any(s == {"llvm"} for s in seen), "Pass 4 must proceed after corpus failure"

def test_train_corpus_user_abort_propagates(tmp_path):
    """3.2.0-B26: 'abort' at the corpus build's failure menu stops the run —
    it is not swallowed as a best-effort corpus failure, and Pass 4 never
    starts."""
    from sysforge.primitives.makepkg_invoke import BuildAborted

    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang")}
    corpus_map = {"mesa": make_pkgbuild(builds, "mesa")}
    staging1, staging, staging3 = (
        tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3"
    )
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = tmp_path / "clang.profdata"
    profdata.write_bytes(b"x" * (PGO_PROFDATA_MIN_BYTES + 1))

    options = make_options(
        dry_run=False, rebuild_profdata=True, state_dir=tmp_path / "state"
    )

    seen = []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        seen.append(set(pkgbuild_map.keys()))
        if set(pkgbuild_map.keys()) == {"mesa"}:
            raise BuildAborted("[build_failed] Aborted by user after build failure")
        return {}

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "profdata.pgo_stage_instrumented"), \
         patch(T + "profdata.profile_runtime_ldflag", return_value=None), \
         patch(T + "profdata.profraw_merge_daemon"), \
         patch(T + "profdata.merge_profraw", return_value=profdata), \
         patch(T + "profdata.write_profdata_version") as wpv, \
         patch(T + "pgo.fs_provision.ensure_writable_dir"), \
         patch(T + "pgo.fs_provision.empty_dir_contents"), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")), \
         pytest.raises(BuildAborted):
        build_llvm_pgo_inner(
            pgo_map, non_pgo_map, {},
            staging1, staging, staging3, pgo_store, options,
            corpus_map=corpus_map,
        )

    assert seen[-1] == {"mesa"}, "nothing may build after the user's abort"
    wpv.assert_not_called()


def test_build_pass_threads_pgo_reuse_to_build_options(tmp_path):
    """3.2.0-B28: build_pass(pgo_reuse=False) reaches BuildOptions.pgo_reuse,
    the gate on the wrapper's durable per-package profile reuse."""
    from sysforge.pipeline.stages.toolchain import passes

    pkgbuild = make_pkgbuild(tmp_path / "builds", "mesa")
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    captured = []
    with patch.object(passes, "makepkg_run",
                      side_effect=lambda p, options: captured.append(options)):
        passes.build_pass("corpus", {"mesa": pkgbuild}, options,
                          install=False, pgo_reuse=False)
        passes.build_pass("normal", {"mesa": pkgbuild}, options, install=False)
    assert [o.pgo_reuse for o in captured] == [False, True]


def test_pgo_lock_contention_raises_with_holder_pid(tmp_path):
    """Second acquirer fails fast with the holder's PID surfaced."""
    from sysforge.pipeline.stages.toolchain import pgo_lock

    lock_path = tmp_path / "sysforge-pgo.lock"

    with pgo_lock(lock_path):
        # Lock is held; a nested attempt on the same path must fail.
        with pytest.raises(RuntimeError, match="Another sysforge PGO build"):
            with pgo_lock(lock_path):
                pass
        # Lock file should name the holder PID.
        assert lock_path.read_text().strip() == str(os.getpid())

    # After release, a fresh acquirer succeeds.
    with pgo_lock(lock_path):
        pass

def test_pgo_train_disables_ccache_and_sccache(tmp_path):
    """
    Pass 3 must inject CCACHE_DISABLE=1 and SCCACHE_DISABLE=1 into the build
    environment.  If either cache tool intercepts a compilation it bypasses the
    instrumented compiler entirely, producing no profraw data and silently
    degrading the PGO profile.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"])
    p2 = _train_calls(call_log)
    assert p2, "Pass 3 must have run"
    for call in p2:
        assert call["env"].get("CCACHE_DISABLE") == "1", \
            "CCACHE_DISABLE=1 missing from Pass 3 env"
        assert call["env"].get("SCCACHE_DISABLE") == "1", \
            "SCCACHE_DISABLE=1 missing from Pass 3 env"

def test_pgo_instrument_and_build_do_not_disable_cache_tools(tmp_path):
    """
    CCACHE/SCCACHE_DISABLE must only be set in Pass 3 (the training run).
    Passes 1 and 4 use distinct compiler flags (-fprofile-generate /
    -fprofile-use) that already produce cache misses naturally; disabling
    cache tools there would throw away legitimate cache benefit on reruns.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"])
    for call in _instrument_calls(call_log) + _build_calls(call_log):
        assert "CCACHE_DISABLE" not in call["env"], \
            f"CCACHE_DISABLE must not be set in Pass {1 if call['cc'] is None else 4}"
        assert "SCCACHE_DISABLE" not in call["env"], \
            f"SCCACHE_DISABLE must not be set in Pass {1 if call['cc'] is None else 4}"

def test_pgo_train_includes_non_pgo_packages(tmp_path):
    """
    Pass 3 must build pgo + non_pgo packages (not just pgo) so the training
    run exercises additional clang code paths: OpenMP pragmas, compiler-rt
    intrinsics, Polly polyhedral analysis.
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"])

    p2_pkgbuilds = {c["pkgbuild"] for c in _train_calls(call_log)}
    # Both pgo and non_pgo packages must appear in Pass 3
    assert any("llvm" in pb          for pb in p2_pkgbuilds), "pgo pkg missing from Pass 3"
    assert any("compiler-rt" in pb   for pb in p2_pkgbuilds), "non_pgo pkg missing from Pass 3"

def test_pgo_instrument_does_not_include_non_pgo_packages(tmp_path):
    """
    Pass 1 builds only pgo packages with -fprofile-generate; non_pgo packages
    must not be included there (they don't need instrumentation, and building
    them against the instrumented static libs would fail at link time).
    """
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"])

    p1_pkgbuilds = {c["pkgbuild"] for c in _instrument_calls(call_log)}
    assert not any("compiler-rt" in pb for pb in p1_pkgbuilds), \
        "non_pgo package must not be built in Pass 1"

def test_pgo_build_includes_non_pgo_and_lib32(tmp_path):
    """Pass 4 must build all package groups: pgo, non_pgo, and lib32."""
    call_log = _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                        non_pgo_pkgs=["compiler-rt"], lib32_pkgs=["lib32-llvm"])

    p3_pkgbuilds = {c["pkgbuild"] for c in _build_calls(call_log)}
    assert any("llvm" in pb          for pb in p3_pkgbuilds)
    assert any("compiler-rt" in pb   for pb in p3_pkgbuilds)
    assert any("lib32-llvm" in pb    for pb in p3_pkgbuilds)

def test_pgo_profile_runtime_injected_into_bootstrap_and_train(tmp_path):
    """Phase 2: Pass 2 and Pass 3 build against stage1's instrumented .a
    archives via find_package(LLVM). Without the clang profile runtime in
    LDFLAGS, those builds fail to resolve __llvm_profile_* symbols. Pass 1
    (no external instrumented .a) and Pass 4 (built against stage2's
    non-instrumented LLVM) must NOT receive the flag."""
    fake_rt_flag = "-L/usr/lib/clang/18/lib/linux -lclang_rt.profile-x86_64"
    call_log = _run_pgo(
        tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"],
        runtime_flag=fake_rt_flag,
    )

    p1a = _instrument_calls(call_log)
    p1b = _bootstrap_calls(call_log)
    p2 = _train_calls(call_log)
    p3 = _build_calls(call_log)
    assert p1a, "Pass 1 must have run"
    assert p1b, "Pass 2 must have run (non_pgo present)"
    assert p2, "Pass 3 must have run"
    assert p3, "Pass 4 must have run"

    for call in p1a:
        assert call["lfe"] is None, \
            "Pass 1 builds llvm from scratch — no external instrumented .a to satisfy"
    for call in p1b:
        assert call["lfe"] == fake_rt_flag, \
            "Pass 2 links against stage1's instrumented .a → profile runtime required"
    for call in p2:
        assert call["lfe"] == fake_rt_flag, (
            "Pass 3 non_pgo find_package(LLVM) hits stage1's instrumented .a "
            "→ profile runtime required")
    for call in p3:
        assert call["lfe"] is None, \
            "Pass 4 uses stage2 (non-instrumented) — profile runtime must NOT leak through"

def test_pgo_instrumented_consumer_passes_select_lld(tmp_path):
    """Pass 2 and Pass 3 link against stage1's *instrumented* archives. They
    must select lld (toolchain_variant="pgo_llvm") so the [VARIANT_LD] guard in
    emit_makepkg_conf injects -fuse-ld=lld — otherwise they fall back to the
    CC=gcc profile's bfd, whose strict left-to-right archive resolution drops
    the force-loaded profile runtime and __llvm_profile_* dangles (the
    historical Pass 2 link failure). Regression guard for that omission."""
    call_log = _run_pgo(
        tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"],
    )
    p1b = _bootstrap_calls(call_log)
    p2 = _train_calls(call_log)
    assert p1b, "Pass 2 must have run (non_pgo present)"
    assert p2, "Pass 3 must have run"
    for call in p1b:
        assert call["variant"] == "pgo_llvm", \
            "Pass 2 must select lld via toolchain_variant=pgo_llvm"
    for call in p2:
        assert call["variant"] == "pgo_llvm", \
            "Pass 3 must select lld via toolchain_variant=pgo_llvm"

def test_pgo_intermediate_passes_record_their_own_variant(tmp_path):
    """3.5.0-B39: passes 1 to 3 build as pgo_llvm but record
    INTERMEDIATE_VARIANT, so a run that stops before Pass 4 cannot pass the
    --install-only / --bolt-only record check. Pass 4 records pgo_llvm."""
    from sysforge.pipeline.stages.toolchain import constants
    call_log = _run_pgo(
        tmp_path, pgo_pkgs=["llvm"], non_pgo_pkgs=["compiler-rt"],
    )
    intermediate = (_instrument_calls(call_log) + _bootstrap_calls(call_log)
                    + _train_calls(call_log))
    assert _instrument_calls(call_log) and _bootstrap_calls(call_log) and _train_calls(call_log)
    for call in intermediate:
        assert call["variant"] == "pgo_llvm"
        assert call["record_variant"] == constants.INTERMEDIATE_VARIANT
    final = _build_calls(call_log)
    assert final
    for call in final:
        assert call["record_variant"] is None


def test_pgo_warns_when_profdata_suspiciously_small(tmp_path):
    """
    A warn is emitted when merged profdata is smaller than PGO_PROFDATA_MIN_BYTES.
    This is the canary for cache bypass: if ccache/sccache slipped through, clang
    never ran, no profraw was generated, and the profdata will be tiny or empty.
    """
    warn_calls = []
    with patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                 profdata_size=PGO_PROFDATA_MIN_BYTES - 1)

    assert any("unexpectedly small" in str(a) for a in warn_calls), \
        "Expected a warning about suspiciously small profdata"

def test_pgo_no_size_warning_when_profdata_adequate(tmp_path):
    """No profdata size warning when profdata is large enough to represent real training."""
    warn_calls = []
    with patch("sysforge.log.warn", side_effect=lambda *a: warn_calls.append(a)):
        _run_pgo(tmp_path, pgo_pkgs=["llvm"],
                 profdata_size=PGO_PROFDATA_MIN_BYTES + 1)

    assert not any("unexpectedly small" in str(a) for a in warn_calls), \
        "Unexpected profdata size warning for adequate profdata"

def test_pgo_confirm_auto_pgo_skips_prompt():
    """--auto-pgo bypasses the prompt entirely (no TTY check, no input)."""
    with patch("sysforge.primitives.prompt.is_interactive") as is_int, \
         patch("sysforge.primitives.prompt.prompt_choice") as pc:
        result = pgo_confirm(
            "fake prompt",
            default="n", eof_default="n",
            options=_confirm_options(auto_pgo=True),
            abort_msg="should not abort",
        )
    assert result is True
    is_int.assert_not_called()
    pc.assert_not_called()

def test_pgo_confirm_non_tty_aborts_without_auto_pgo():
    """No TTY and no --auto-pgo → abort with 'requires --auto-pgo' message."""
    with patch("sysforge.primitives.prompt.is_interactive",
               return_value=False), \
         patch("sysforge.primitives.prompt.prompt_choice") as pc:
        with pytest.raises(PGOAborted, match="non-interactive PGO requires --auto-pgo"):
            pgo_confirm(
                "fake prompt",
                default="n", eof_default="n",
                options=_confirm_options(),
                abort_msg="declined",
            )
        pc.assert_not_called()

def test_pgo_confirm_tty_yes_returns_true():
    """User answers 'y' at TTY → returns True."""
    with patch("sysforge.primitives.prompt.is_interactive",
               return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice",
               return_value="y"):
        result = pgo_confirm(
            "fake prompt",
            default="n", eof_default="n",
            options=_confirm_options(),
            abort_msg="declined",
        )
    assert result is True

def test_pgo_confirm_tty_no_raises():
    """User answers 'n' at TTY → raises PGOAborted with the abort_msg."""
    with patch("sysforge.primitives.prompt.is_interactive",
               return_value=True), \
         patch("sysforge.primitives.prompt.prompt_choice",
               return_value="n"), pytest.raises(PGOAborted, match="user declined the build"):
        pgo_confirm(
            "fake prompt",
            default="n", eof_default="n",
            options=_confirm_options(),
            abort_msg="user declined the build",
        )

def test_single_pass_setup_isolates_build_paths(tmp_path):
    """Fixture hygiene: the toolchain config must keep the staging dirs, pgo_store,
    and thus the PGO build lock under tmp_path. Otherwise a test that drives
    ToolchainStage.run far enough grabs the real /var/tmp/sysforge-pgo.lock and
    collides with a concurrent live `sysforge run toolchain` (2.5.1-B2 follow-up)."""
    import tomllib

    from sysforge.pipeline.stages.toolchain import DEFAULT_STAGING_1, pgo_lock_path
    from sysforge.primitives.makepkg_pgo import resolve_pgo_store

    toml_path, *_ = _single_pass_setup(tmp_path)
    with toml_path.open("rb") as f:
        tcfg = tomllib.load(f)

    staging1 = Path(tcfg.get("pgo_staging1", DEFAULT_STAGING_1))
    assert str(staging1).startswith(str(tmp_path)), "staging1 must be tmp-scoped"
    assert str(pgo_lock_path(staging1)).startswith(str(tmp_path)), \
        "PGO build lock must be tmp-scoped, not /var/tmp"
    assert str(resolve_pgo_store(tcfg)).startswith(str(tmp_path)), \
        "pgo_store must be tmp-scoped"


def _record_trackers(monkeypatch):
    """Replace progress.tracker with a recorder: [(prefix, total, [labels])]."""
    import contextlib
    from sysforge.ui import progress
    opened = []

    @contextlib.contextmanager
    def fake_tracker(total, prefix):
        labels = []
        opened.append((prefix, total, labels))
        yield labels.append

    monkeypatch.setattr(progress, "tracker", fake_tracker)
    return opened


def test_split_pass4_counts_once_across_sub_passes(tmp_path, monkeypatch):
    """3.3.0-B12: Pass 4's three sub-passes (4a/4b/4c) share one counter."""
    builds = tmp_path / "builds"
    pgo_map = {"llvm": make_pkgbuild(builds, "llvm")}
    non_pgo_map = {"clang": make_pkgbuild(builds, "clang"),
                   "lld": make_pkgbuild(builds, "lld")}
    lib32_map = {"lib32-llvm": make_pkgbuild(builds, "lib32-llvm")}
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = pgo_store / "clang.profdata"
    profdata.write_bytes(b"fake")
    options = make_options(dry_run=False, rebuild_profdata=False,
                           state_dir=tmp_path / "state")
    opened = _record_trackers(monkeypatch)
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "profdata.check_existing_profdata", return_value=("ready", str(profdata))), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "passes.makepkg_run"), \
         patch(T + "profdata.extract_built_to_staging"), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.assert_pass_links_shipped_libllvm"), \
         patch(T + "profdata.remove_staging"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(pgo_map, non_pgo_map, lib32_map,
                             tmp_path / "s1", tmp_path / "s2", tmp_path / "s3",
                             pgo_store, options)
    assert opened == [("optimize", 4,
                       ["llvm", "clang", "lld", "lib32-llvm"])]


def test_full_pgo_run_one_tracker_per_logical_pass(tmp_path, monkeypatch):
    """3.3.0-B12: every counter runs 1..total exactly once, and Pass 4 (split
    across sub-passes) is a single tracker rather than one per call."""
    opened = _record_trackers(monkeypatch)
    _run_pgo(tmp_path, ["llvm", "llvm-libs"], ["clang", "lld"], ["lib32-llvm"])
    pass4 = [o for o in opened if o[0] == "optimize"]
    assert len(pass4) == 1 and pass4[0][1] == 5  # llvm + llvm-libs dirs, clang, lld, lib32
    train = [o for o in opened if o[0].startswith("train")]
    assert [p for p, *_ in train] == ["train"]
    for prefix, total, labels in opened:
        assert len(labels) == total, (prefix, total, labels)


def test_verified_llvm_build_hands_off_to_the_bench_step(tmp_path, monkeypatch):
    """3.4.0-F11, llvm path: a from-source build that passes Gate 3 calls
    measure.after_install once, with the stage's own config."""
    from sysforge.pipeline.stages.toolchain import measure

    calls = []
    monkeypatch.setattr(measure, "after_install",
                        lambda tcfg, options: calls.append(tcfg))
    _run_pgo(tmp_path, pgo_pkgs=["llvm"])
    assert len(calls) == 1 and calls[0].compiler == "llvm"


def test_bolt_runs_on_the_4a_tree_before_staging_and_repackages(tmp_path):
    """3.5.0-F3: BOLT rewrites the Pass-4a build tree and repackages it before
    anything is staged or installed from those packages. The repackage reruns
    package() only (--repackage), never re-syncing the source it just BOLTed."""
    from tests.toolchain_helpers import toolchain_cfg
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    profdata = pgo_store / "clang.profdata"
    profdata.write_bytes(b"fake")
    options = make_options(dry_run=False, rebuild_profdata=False,
                           state_dir=tmp_path / "state")
    events, repacks, roots = [], [], []

    def fake_build_pass(label, pkgbuild_map, options, **kw):
        events.append(("build", set(pkgbuild_map), kw.get("linker_flags_extra")))
        if kw.get("build_roots") is not None:
            kw["build_roots"][llvm.parent] = builds / "root-llvm"
        return {}

    def fake_optimize(pgo_map, tcfg, options, *, repackage, build_root=None):
        events.append(("bolt", set(pgo_map)))
        roots.append(build_root)
        repackage()
        return True

    def fake_build_pkg(name, path, opts, **kw):
        repacks.append((name, path, opts.no_update, kw.get("extra_flags")))

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "profdata.validate_pgo_environment"), \
         patch(T + "profdata.check_existing_profdata", return_value=("ready", str(profdata))), \
         patch(T + "pgo.pgo_confirm"), \
         patch(T + "passes.build_pass", side_effect=fake_build_pass), \
         patch(T + "passes.build_pkg", side_effect=fake_build_pkg), \
         patch(T + "bolt.optimize_in_build", side_effect=fake_optimize), \
         patch(T + "profdata.extract_built_to_staging",
               side_effect=lambda m, s, d: events.append(("stage", s.name))), \
         patch(T + "profdata.assert_staging_has_llvm_cmake"), \
         patch(T + "profdata.remove_staging"), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        build_llvm_pgo_inner(
            {"llvm": llvm, "clang": llvm}, {"spirv-llvm-translator": make_pkgbuild(
                builds, "spirv-llvm-translator")}, {},
            tmp_path / "stage1", tmp_path / "stage2", tmp_path / "stage3", pgo_store,
            options, tcfg=toolchain_cfg(bolt={"enabled": True}),
        )

    kinds = [e[0] for e in events]
    assert kinds.index("bolt") == kinds.index("build") + 1  # right after 4a
    assert kinds.index("bolt") < kinds.index("stage")
    assert events[0][2] == "-Wl,--emit-relocs"  # 4a keeps relocations
    assert repacks == [("llvm", llvm, True, ["--repackage", "--force"])]
    assert roots == [builds / "root-llvm"]  # 3.5.0-B22: 4a's own build root
    assert getattr(options, "no_update", False) is False


def test_bolt_only_builds_nothing_and_bolts_strictly(tmp_path, monkeypatch):
    """3.5.0-F7: --bolt-only checks the tree, then BOLTs it in strict mode with
    BOLT forced on (the flag is the request), repackages llvm only, builds
    nothing, and returns the whole suite for Gate 2 + install."""
    from sysforge.pipeline.stages.toolchain import pgo
    from tests.toolchain_helpers import toolchain_cfg
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    spirv = make_pkgbuild(builds, "spirv-llvm-translator")
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    (pgo_store / "clang.profdata").write_bytes(b"x")
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    seen, repacks = {}, []

    def fake_check(pgo_map, entry, *, libllvm):
        seen["checked"] = set(pgo_map)
        return builds / "root"

    def fake_optimize(pgo_map, tcfg, options, *, repackage, build_root=None, strict=False):
        seen.update(enabled=tcfg.bolt.enabled, root=build_root, strict=strict)
        repackage()
        return True

    def fake_build_pkg(name, path, opts, **kw):
        repacks.append((name, path, kw.get("extra_flags"), kw.get("compiler_flags_extra")))

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "bolt.check_bolt_only_tree", side_effect=fake_check), \
         patch(T + "bolt.optimize_in_build", side_effect=fake_optimize), \
         patch(T + "passes.build_pkg", side_effect=fake_build_pkg), \
         patch(T + "passes.build_pass", side_effect=AssertionError("built")):
        built, cc, cxx, ld, variant = pgo.bolt_only(
            {"llvm": llvm, "clang": llvm}, {"spirv-llvm-translator": spirv}, {},
            pgo_store, options, tcfg=toolchain_cfg(bolt={"enabled": False}))

    assert seen == {"checked": {"llvm", "clang"}, "enabled": True,
                    "root": builds / "root", "strict": True}
    assert repacks == [("llvm", llvm, ["--repackage", "--force"],
                        f"-fprofile-use={pgo_store / 'clang.profdata'}")]
    assert set(built) == {"llvm", "clang", "spirv-llvm-translator"}
    assert (cc, variant) == ("/usr/bin/clang", "pgo_llvm")


def test_bolt_only_holds_sudo_from_before_the_bolt(tmp_path, monkeypatch):
    """3.5.0-B30: BOLT takes minutes and the install's sudo follows it, so
    --bolt-only authenticates up front and keeps credentials warm across the
    BOLT, as the full build does, rather than prompting an unattended terminal."""
    from sysforge.pipeline.stages.toolchain import pgo
    from sysforge.primitives import sudo_session
    from tests.toolchain_helpers import toolchain_cfg
    import contextlib
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    events = []

    @contextlib.contextmanager
    def fake_keepalive(*, tag, enabled=True):
        events.append(("keepalive-on", enabled))
        yield
        events.append(("keepalive-off", enabled))

    monkeypatch.setattr(sudo_session, "authenticate", lambda: events.append("auth") or True)
    monkeypatch.setattr(sudo_session, "keepalive", fake_keepalive)
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "bolt.check_bolt_only_tree", return_value=None), \
         patch(T + "bolt.optimize_in_build",
               side_effect=lambda *a, **k: events.append("bolt") or True):
        pgo.bolt_only({"llvm": llvm, "clang": llvm}, {}, {}, tmp_path / "store",
                      options, tcfg=toolchain_cfg())
    assert events == ["auth", ("keepalive-on", True), "bolt", ("keepalive-off", True)]


def test_install_only_builds_nothing_and_returns_the_suite(tmp_path, monkeypatch):
    """3.5.0-F8: --install-only checks the last build record, finds each
    PKGBUILD's built packages, builds nothing and returns the whole suite."""
    from sysforge.pipeline.stages.toolchain import pgo
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    spirv = make_pkgbuild(builds, "spirv-llvm-translator")
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    seen = {}
    T = "sysforge.pipeline.stages.toolchain."

    def fake_check(pgo_map, entry, *, flag, tag):
        seen["flag"] = flag
        return llvm, "llvm", "23.1.3-3"

    with patch(T + "pkgbuilds.check_last_pgo_build", side_effect=fake_check), \
         patch(T + "profdata.collect_pgo_packages",
               side_effect=lambda m: [tmp_path / f"{next(iter(m))}.pkg.tar"]), \
         patch(T + "passes.build_pkg", side_effect=AssertionError("built")), \
         patch(T + "passes.build_pass", side_effect=AssertionError("built")):
        built, cc, cxx, ld, variant = pgo.install_only(
            {"llvm": llvm, "clang": llvm}, {"spirv-llvm-translator": spirv}, {}, options)
    assert seen["flag"] == "--install-only"
    assert set(built) == {"llvm", "clang", "spirv-llvm-translator"}
    assert (cc, ld, variant) == ("/usr/bin/clang", "lld", "pgo_llvm")


def test_install_only_refuses_a_pkgbuild_with_no_built_package(tmp_path):
    from sysforge.pipeline.stages.toolchain import pgo
    builds = tmp_path / "builds"
    llvm = make_pkgbuild(builds, "llvm")
    spirv = make_pkgbuild(builds, "spirv-llvm-translator")
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "pkgbuilds.check_last_pgo_build",
               return_value=(llvm, "llvm", "23.1.3-3")), \
         patch(T + "profdata.collect_pgo_packages",
               side_effect=lambda m: [] if "spirv-llvm-translator" in m else [tmp_path / "x"]), \
         pytest.raises(RuntimeError, match="spirv-llvm-translator"):
        pgo.install_only({"llvm": llvm}, {"spirv-llvm-translator": spirv}, {}, options)


# --- The stage's own steps on the bar (3.5.0-F6) ----------------------------

def _record_steps(monkeypatch):
    """Wrap progress.step: [(step, plan after it)] in the order run."""
    from sysforge.ui import progress
    seen = []
    real = progress.step

    def spy(name):
        real(name)
        steps = progress._bar.steps
        seen.append((name, list(steps.names) if steps else None))
    monkeypatch.setattr(progress, "step", spy)
    return seen


def test_full_pgo_run_counts_its_steps_and_records_their_durations(tmp_path, monkeypatch):
    from sysforge.pipeline.state import resolve_state_dir
    from sysforge.primitives import stage_timings
    seen = _record_steps(monkeypatch)
    _run_pgo(tmp_path, ["llvm", "llvm-libs"], ["spirv-llvm-translator"])
    full = ["preflight", "instrument", "bootstrap", "train", "optimize",
            "audit", "install", "verify"]
    assert [n for n, _ in seen] == full
    # The plan the bar showed matched the run from the first step on.
    assert all(plan == full for _, plan in seen)
    recorded = stage_timings.expected(resolve_state_dir(None)[0], "toolchain")
    assert set(recorded) == set(full)


def test_profdata_reuse_run_counts_only_the_steps_it_takes(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.toolchain import pgo, profdata
    seen = _record_steps(monkeypatch)
    monkeypatch.setattr(pgo, "pgo_confirm", lambda *a, **k: None)
    def ready(pgo_store, pgo_map):
        pgo_store.mkdir(parents=True, exist_ok=True)
        (pgo_store / "clang.profdata").write_bytes(b"fake")
        return ("ready", str(pgo_store / "clang.profdata"))
    monkeypatch.setattr(profdata, "check_existing_profdata", ready)
    _run_pgo(tmp_path, ["llvm"], ["spirv-llvm-translator"])
    reuse = ["preflight", "optimize", "audit", "install", "verify"]
    assert [n for n, _ in seen] == reuse
    assert seen[-1][1] == reuse


def test_dry_run_records_no_step_durations(tmp_path):
    from sysforge.pipeline.stages.toolchain.stage import step_recorder
    from sysforge.primitives import stage_timings
    assert step_recorder(tmp_path, make_options(dry_run=True)) is None
    step_recorder(tmp_path, make_options(dry_run=False))("audit", 42)
    assert stage_timings.expected(tmp_path, "toolchain") == {"audit": 42}


@pytest.mark.parametrize("no_build, pgo, kw, expected", [
    (None, True, {}, ["preflight", "instrument", "bootstrap", "train", "optimize",
                      "audit", "install", "verify"]),
    (None, True, {"bootstrap": False}, ["preflight", "instrument", "train", "optimize",
                                        "audit", "install", "verify"]),
    (None, True, {"reuse_profdata": True}, ["preflight", "optimize", "audit", "install",
                                            "verify"]),
    (None, False, {"consumers": True}, ["preflight", "build", "audit", "install", "verify",
                                        "consumers"]),
    ("--bolt-only", True, {}, ["preflight", "bolt", "audit", "install", "verify"]),
    ("--install-only", True, {}, ["preflight", "audit", "install", "verify"]),
])
def test_planned_steps_per_path(no_build, pgo, kw, expected):
    from sysforge.pipeline.stages.toolchain.stage import planned_steps
    from tests.toolchain_helpers import toolchain_cfg
    tcfg = toolchain_cfg(compiler="llvm", pgo=pgo)
    assert planned_steps(tcfg, make_options(dry_run=False), no_build, **kw) == expected


def test_planned_steps_end_with_bench_only_when_it_runs():
    from sysforge.pipeline.stages.toolchain.stage import planned_steps
    from tests.toolchain_helpers import toolchain_cfg
    tcfg = toolchain_cfg(compiler="llvm", pgo=False, bench={"after_build": True})
    assert planned_steps(tcfg, make_options(dry_run=False), None)[-1] == "bench"
    assert planned_steps(tcfg, make_options(dry_run=True), None)[-1] == "verify"
