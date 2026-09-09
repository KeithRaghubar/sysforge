# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/passes.py + reuse.py — one package, one pass, and Pass-4 reuse.
"""
from sysforge.pipeline.stages.toolchain.passes import build_pass
from sysforge.pipeline.stages.toolchain.pgo import build_llvm_single
from sysforge.pipeline.stages.toolchain.reuse import pkg_fingerprint
from sysforge.primitives import build_fingerprint as bf
from unittest.mock import patch

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _DEPS_AFTER_INSTALL,
    _DEPS_BEFORE_INSTALL,
    _clang_pkgbuild,
    _fake_clang,
    _make_artifact,
    _reuse_ctx,
    _toolchain_gates_clean,
    make_options,
    make_pkgbuild,
)


def test_build_llvm_single_stamps_owner_stage(tmp_path):
    """The non-PGO single-pass install path stamps owner_stage='toolchain' so
    `sysforge update` skips the LLVM suite by default."""
    pkgbuild_dir = tmp_path / "builds"
    pb = make_pkgbuild(pkgbuild_dir, "llvm")
    options = make_options(dry_run=False, makepkg_flags=[], state_dir=tmp_path / "state")

    captured = {}

    def fake_run(pkgbuild_path, options=None):
        captured["owner_stage"] = options.owner_stage if options else None
        captured["variant"] = options.toolchain_variant if options else None

    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run):
        build_llvm_single({"llvm": pb}, {}, {}, options)

    assert captured["owner_stage"] == "toolchain"
    assert captured["variant"] == "stock_llvm"

def test_build_pass_pgo_drops_disallowed_flags(tmp_path):
    """pgo_build=True: only -f/--force pass through from user makepkg_flags."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    pkgbuild_map = {"llvm": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append(list(options.extra_flags or []) if options else [])

    # Simulate user passing -m '-f --noextract --noprepare'
    options = make_options(dry_run=False,
                           makepkg_flags=["-f", "--noextract", "--noprepare"])

    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run):
        build_pass("test pass", pkgbuild_map, options,
                    install=False, pgo_build=True)

    # Only -f should survive; --noextract and --noprepare dropped
    flags = captured[0]
    assert "-f" in flags
    assert "--noextract" not in flags
    assert "--noprepare" not in flags

def test_build_pass_non_pgo_passes_all_flags(tmp_path):
    """pgo_build=False (default): all user flags pass through unchanged."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    pkgbuild_map = {"llvm": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append(list(options.extra_flags or []) if options else [])

    options = make_options(dry_run=False,
                           makepkg_flags=["-f", "--noextract"])

    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run):
        build_pass("test pass", pkgbuild_map, options, install=False)

    flags = captured[0]
    assert "-f" in flags
    assert "--noextract" in flags

def test_build_pass_staged_deps_adds_nodeps_and_strips_syncdeps(tmp_path):
    """staged_deps=True: --nodeps added; --syncdeps/-s stripped via strip_flags.

    Reproduces the failure mode that motivated F1: with --syncdeps in the
    profile flags, Pass 2's clang build asks pacman to install llvm=<ver>
    against repos that don't have it. The fix is to make Pass 2/3/4 set
    staged_deps=True so makepkg never consults pacman for the staged deps.
    """
    pkgbuild = make_pkgbuild(tmp_path, "clang")
    pkgbuild_map = {"clang": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append({
            "extra_flags": list(options.extra_flags or []) if options else [],
            "strip_flags": options.strip_flags if options else None,
        })

    options = make_options(dry_run=False,
                           makepkg_flags=["--syncdeps", "-s", "--noconfirm"])

    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run):
        build_pass(
            "PGO 2/4 · bootstrap clang/lld against stage1",
            pkgbuild_map, options,
            install=False, pgo_build=True, staged_deps=True,
        )

    rec = captured[0]
    assert "--nodeps" in rec["extra_flags"]
    assert rec["strip_flags"] is not None
    assert "--syncdeps" in rec["strip_flags"]
    assert "-s" in rec["strip_flags"]

def test_build_pass_default_keeps_syncdeps_for_pass1a(tmp_path):
    """staged_deps=False (default, Pass 1): no --nodeps, no strip_flags.

    Pass 1 builds against the live system, so --syncdeps must stay so
    that any missing build deps (cmake, ninja, python, z3, ...) get
    installed normally before the build starts.
    """
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    pkgbuild_map = {"llvm": pkgbuild}

    captured = []
    def fake_run(pb, options=None):
        captured.append({
            "extra_flags": list(options.extra_flags or []) if options else [],
            "strip_flags": options.strip_flags if options else None,
        })

    options = make_options(dry_run=False,
                           makepkg_flags=["--syncdeps", "--noconfirm"])

    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run):
        build_pass(
            "PGO 1/4 · instrument llvm",
            pkgbuild_map, options,
            install=False, pgo_build=True,  # staged_deps defaults False
        )

    rec = captured[0]
    assert "--nodeps" not in rec["extra_flags"]
    assert rec["strip_flags"] is None

def test_build_pass_without_reuse_returns_empty_and_builds(tmp_path):
    """No reuse ctx (passes 1/2/3, single-pass, gcc path): unchanged behavior."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    calls = []
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: calls.append(pb)):
        result = build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                             install=False, pgo_build=True)
    assert result == {}
    assert len(calls) == 1
    assert not (tmp_path / "build_cache.json").exists()  # no cache I/O

def test_build_pass_records_cache_without_consulting(tmp_path):
    """consult=False still populates the cache (so a later resume can use it)."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    _make_artifact(tmp_path / "llvm", "llvm")
    ctx = _reuse_ctx(tmp_path, consult=False)
    calls = []
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: calls.append(pb)):
        result = build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                             install=False, pgo_build=True, reuse_ctx=ctx)
    assert len(calls) == 1               # built
    assert "llvm" in result              # fingerprint returned
    cache = bf.load_cache(tmp_path / "build_cache.json")
    assert bf.cache_key("build-pgo", "llvm") in cache  # recorded for next run

def test_build_pass_skips_build_on_cache_hit(tmp_path):
    """consult=True with a matching, present artifact: makepkg is NOT invoked."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    _make_artifact(tmp_path / "llvm", "llvm")

    # First run populates the cache (not opted in).
    ctx1 = _reuse_ctx(tmp_path, consult=False)
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: None):
        build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                    install=False, pgo_build=True, reuse_ctx=ctx1)

    # Resume opted in: identical inputs → cache hit → no rebuild.
    ctx2 = _reuse_ctx(tmp_path, consult=True)
    calls = []
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: calls.append(pb)):
        result = build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                             install=False, pgo_build=True, reuse_ctx=ctx2)
    assert calls == []          # build skipped
    assert "llvm" in result     # fingerprint still reported (for Merkle chain)

def test_build_pass_rebuilds_when_input_changes(tmp_path):
    """consult=True but a changed input (config_digest) → fingerprint miss → build."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    _make_artifact(tmp_path / "llvm", "llvm")

    ctx1 = _reuse_ctx(tmp_path, consult=False, config_digest="OLD")
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: None):
        build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                    install=False, pgo_build=True, reuse_ctx=ctx1)

    ctx2 = _reuse_ctx(tmp_path, consult=True, config_digest="NEW")  # config changed
    calls = []
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: calls.append(pb)):
        build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=False),
                    install=False, pgo_build=True, reuse_ctx=ctx2)
    assert len(calls) == 1  # rebuilt — never reuses across a config change

def test_build_pass_dry_run_does_no_cache_io(tmp_path):
    """dry-run never consults or writes the cache (no artifacts to validate)."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    ctx = _reuse_ctx(tmp_path, consult=True)
    calls = []
    with patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda pb, options=None: calls.append(pb)):
        result = build_pass("p", {"llvm": pkgbuild}, make_options(dry_run=True),
                             install=False, pgo_build=True, reuse_ctx=ctx)
    assert calls == []
    assert result == {}
    assert not (tmp_path / "build_cache.json").exists()

def test_pkg_fingerprint_merkle_chain_changes(tmp_path):
    """A consumer's fingerprint shifts when a staged dep's fingerprint shifts."""
    pkgbuild = make_pkgbuild(tmp_path, "clang")
    ctx_v1 = _reuse_ctx(tmp_path, pass_id="build-nonpgo", staged_dep_fps=["fp-llvm-v1"])
    ctx_v2 = _reuse_ctx(tmp_path, pass_id="build-nonpgo", staged_dep_fps=["fp-llvm-v2"])
    _, fp1 = pkg_fingerprint(ctx_v1, "clang", pkgbuild, None, "-fp=/p", None, None, [])
    _, fp2 = pkg_fingerprint(ctx_v2, "clang", pkgbuild, None, "-fp=/p", None, None, [])
    assert fp1 != fp2

def test_pkg_fingerprint_stable_across_staged_vs_system_clang(tmp_path):
    """Pass-4 reuse must survive the staged→installed compiler swap (2.1.0-B14).

    A profgen run records Pass-4 fingerprints under the staged stage-2 clang; a
    profdata-reuse resume recomputes them under /usr/bin/clang. Both report the
    same compiler --version line, so the fingerprint must match — otherwise the
    resume never hits the cache the profgen run populated, defeating reuse.
    """
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    staged = _fake_clang(tmp_path / "stage2" / "clang", "clang version 19.1.0", "staged")
    system = _fake_clang(tmp_path / "usr" / "clang", "clang version 19.1.0",
                         "installed-bytes-differ")
    ctx = _reuse_ctx(tmp_path)
    _, fp_staged = pkg_fingerprint(ctx, "llvm", pkgbuild, staged, None, None, None, [])
    _, fp_system = pkg_fingerprint(ctx, "llvm", pkgbuild, system, None, None, None, [])
    assert fp_staged == fp_system

def test_pkg_fingerprint_changes_across_compiler_version(tmp_path):
    """The version line still guards a genuine compiler-version bump: a major
    version change must invalidate the cache even though the reuse dimension no
    longer folds in path/size/mtime."""
    pkgbuild = make_pkgbuild(tmp_path, "llvm")
    v19 = _fake_clang(tmp_path / "a" / "clang", "clang version 19.1.0")
    v20 = _fake_clang(tmp_path / "b" / "clang", "clang version 20.0.0")
    ctx = _reuse_ctx(tmp_path)
    _, fp19 = pkg_fingerprint(ctx, "llvm", pkgbuild, v19, None, None, None, [])
    _, fp20 = pkg_fingerprint(ctx, "llvm", pkgbuild, v20, None, None, None, [])
    assert fp19 != fp20

def test_dep_versions_excludes_build_set_members(monkeypatch):
    """2.5.1-B2: build-set members (llvm/llvm-libs/…) are satisfied from the
    staging prefix during Pass 4 (--nodeps), so their *installed* version is not
    a build input and must be excluded from makedep_versions. External build deps
    (cmake/…) come from the live /usr and stay."""
    from sysforge.pipeline.stages import toolchain

    monkeypatch.setattr(
        toolchain, "query_pacman_versions",
        lambda names: {n: "9.9-9" for n in names},
    )
    globals_ = {
        "makedepends": ["cmake", "llvm=1.0", "ninja"],
        "depends": ["llvm-libs"],
    }
    result = toolchain.reuse._dep_versions_from_globals(
        globals_, exclude={"llvm", "llvm-libs"},
    )
    assert set(result) == {"cmake", "ninja"}  # build-set members dropped

def test_pkg_fingerprint_stable_across_build_set_member_install(tmp_path, monkeypatch):
    """2.5.1-B2: a sanity re-run after installing the built suite must still hit
    the cache. Pass 4 links the *staged* libLLVM (captured by staged_dep_fps), so
    the bumped *installed* llvm-libs version must not move the fingerprint."""
    from sysforge.pipeline.stages import toolchain

    pb = _clang_pkgbuild(tmp_path)
    ctx = _reuse_ctx(tmp_path, pass_id="build-nonpgo", staged_dep_fps=["fp-llvm"])
    ctx.exclude_deps = frozenset({"llvm", "llvm-libs", "clang"})

    monkeypatch.setattr(toolchain.verify, "query_pacman_versions",
                        lambda names: {n: _DEPS_BEFORE_INSTALL[n] for n in names})
    _, fp_before = pkg_fingerprint(ctx, "clang", pb, None, None, None, None, [])
    monkeypatch.setattr(toolchain.verify, "query_pacman_versions",
                        lambda names: {n: _DEPS_AFTER_INSTALL[n] for n in names})
    _, fp_after = pkg_fingerprint(ctx, "clang", pb, None, None, None, None, [])
    assert fp_before == fp_after

def test_pkg_fingerprint_changes_when_external_dep_bumps(tmp_path, monkeypatch):
    """The exclusion is surgical: an external build dep (cmake) bumping still
    invalidates the fingerprint — only build-set members are exempted."""
    from sysforge.pipeline.stages import toolchain

    pb = _clang_pkgbuild(tmp_path)
    ctx = _reuse_ctx(tmp_path, pass_id="build-nonpgo", staged_dep_fps=["fp-llvm"])
    ctx.exclude_deps = frozenset({"llvm", "llvm-libs", "clang"})

    monkeypatch.setattr(toolchain.verify, "query_pacman_versions",
                        lambda names: {n: {"cmake": "3.0-1"}.get(n, "0") for n in names})
    _, fp_old = pkg_fingerprint(ctx, "clang", pb, None, None, None, None, [])
    monkeypatch.setattr(toolchain.verify, "query_pacman_versions",
                        lambda names: {n: {"cmake": "3.1-1"}.get(n, "0") for n in names})
    _, fp_new = pkg_fingerprint(ctx, "clang", pb, None, None, None, None, [])
    assert fp_old != fp_new

def test_reuse_cache_path_survives_pgo_store_purge(tmp_path):
    """The reuse cache must live outside pgo_store so a fresh 4-pass run's
    startup purge (empty_dir_contents) can't wipe a prior run's cache (2.1.0-B14)."""
    from sysforge.pipeline.stages.toolchain import reuse_cache_path
    from sysforge.primitives import fs_provision

    pgo_store = tmp_path / "llvm-pgo"
    pgo_store.mkdir()
    cache_path = reuse_cache_path(pgo_store)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("{}")

    fs_provision.empty_dir_contents(pgo_store)  # the fresh-run purge

    assert cache_path.exists(), "reuse cache must survive the pgo_store purge"
    assert pgo_store not in cache_path.parents
