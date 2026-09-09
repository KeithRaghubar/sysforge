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
        })
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
