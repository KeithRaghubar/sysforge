# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain_helpers.py — shared builders and fixtures for the toolchain tests.

The toolchain stage became a package in 3.2.0-F2 and its 4945-line test file
split along the same seams. These helpers were shared across those sections,
so they live here rather than being copied into each file — a duplicated
fixture is how two test files quietly stop testing the same thing.

``_toolchain_gates_clean`` is autouse: import it into a test module and it
applies there. Every toolchain test module needs it, because the gates hit the
real system otherwise.
"""
from pathlib import Path
from sysforge.pipeline.stages.toolchain.config import ToolchainConfig
from sysforge.pipeline.stages.base import RunOptions
from sysforge.pipeline.stages.toolchain.reuse import ReuseCtx
from sysforge.pipeline.stages.toolchain.stage import ToolchainStage
from sysforge.pipeline.state import PipelineState
from sysforge.primitives import build_fingerprint as bf
from unittest.mock import MagicMock
from unittest.mock import patch
import os
import pytest
import time


def make_options(**kwargs):
    opts = RunOptions(no_pkg_logs=True)
    for k, v in kwargs.items():
        setattr(opts, k, v)
    return opts

def write_toolchain_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path

def make_pkgbuild(pkgbuild_dir: Path, name: str) -> Path:
    d = pkgbuild_dir / name
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")
    return pb

def _make_artifact(d: Path, name: str, ver: str = "1.0-1") -> Path:
    """Create a fake makepkg artifact (build_fingerprint reuse tests)."""
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}-{ver}-x86_64.pkg.tar.zst"
    p.write_bytes(b"pkg")
    return p

@pytest.fixture(autouse=True)
def _toolchain_gates_clean(monkeypatch):
    """Make the host-dependent toolchain-safety facts inert by default.

    Gate 1 (build-space / compiler smoke / multilib) and the install-time
    snapshot read the real machine — free disk, /usr/bin/clang, /etc/pacman.conf,
    the pacman cache — none of which a test controls. Mirroring the kernel
    suite's clean-axis convention, this autouse fixture stubs each pure fact to
    its no-finding result and the snapshot to "nothing cached" so the existing
    full-flow tests exercise the build/install plumbing without tripping a gate.
    Dedicated gate tests re-patch the specific function they target to inject a
    finding (monkeypatch lets a test override the same attribute).
    """
    from sysforge.primitives import toolchain_safety as _ts

    monkeypatch.setattr(_ts, "smoke_test_compilers", lambda: [], raising=True)
    monkeypatch.setattr(_ts, "check_build_space", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(_ts, "check_multilib_enabled", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(_ts, "check_pkgver_lockstep", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(_ts, "detect_residual_instrumentation", lambda: [], raising=True)
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: [], raising=True)
    monkeypatch.setattr(
        _ts, "check_system_consumer_symbols", lambda pkgs: [], raising=True)
    monkeypatch.setattr(
        _ts, "check_installed_consumer_symbols", lambda: [], raising=True)
    # On a successful llvm build the stage propagates profiles.toml [defaults]
    # toolchain via set_default_toolchain — which writes SYSFORGE_CONFIG_DIR,
    # pointed by conftest at the git-tracked fixture. Neutralise it so toolchain
    # tests never mutate tests/data/etc/sysforge/profiles.toml (no test asserts
    # propagation; set_default_toolchain has its own unit coverage).
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.identity.propagate_default_toolchain",
        lambda compiler, options: None, raising=True)
    # Snapshot: no suite package resolves to a cached file (offline-undo
    # unavailable). Tests that exercise rollback patch this explicitly.
    monkeypatch.setattr(
        "sysforge.primitives.pacman.cached_pkg_files_for",
        lambda names: {n: None for n in names},
        raising=True,
    )

def _write_packages_repo_mode(tmp_path: Path, mode: str) -> str:
    """Write a packages.toml with the given [build] repo_mode; return its path."""
    p = tmp_path / "packages.toml"
    p.write_text(f'[build]\nrepo_mode = "{mode}"\n')
    return str(p)

def _make_old_profraw(path: Path) -> Path:
    """Touch a profraw file and backdate its mtime by 30 seconds so it passes the settle filter."""
    path.touch()
    past = time.time() - 30
    os.utime(path, (past, past))
    return path

def fake_profdata_merge(cmd, **kwargs):
    """subprocess.run side_effect that creates the --output file."""
    result = MagicMock()
    result.returncode = 0
    result.stderr = ""
    if cmd and "llvm-profdata" in cmd[0]:
        idx = cmd.index("--output")
        Path(cmd[idx + 1]).touch()
    return result

def fake_profdata_merge_fail(cmd, **kwargs):
    result = MagicMock()
    result.returncode = 1
    result.stderr = "error: bad input"
    return result

def _g2_opts(rebuild_soname_consumers=None):
    """Minimal options stand-in for gate2_audit (only the heal path reads it)."""
    import types
    return types.SimpleNamespace(rebuild_soname_consumers=rebuild_soname_consumers)

def _make_so(base, name):
    libdir = base / "usr/lib"
    libdir.mkdir(parents=True, exist_ok=True)
    (libdir / name).touch()

def _reuse_ctx(tmp_path, *, pass_id="build-pgo", consult=False, config_digest="d",
               profdata_sha="p", staged_dep_fps=None):
    return ReuseCtx(
        pass_id=pass_id,
        cache=bf.load_cache(tmp_path / "build_cache.json"),
        cache_path=tmp_path / "build_cache.json",
        config_digest=config_digest,
        profdata_sha=profdata_sha,
        pkgdest=None,  # search the per-package build dir
        consult=consult,
        staged_dep_fps=staged_dep_fps or [],
    )

def _fake_clang(path: Path, version_line: str, filler: str = "") -> str:
    """A minimal executable that prints ``version_line`` for any argument.

    ``filler`` perturbs the file's bytes/size without changing its --version
    output — modelling the staged stage-2 clang vs the installed /usr clang,
    which report the same version but are different binaries at different paths.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{version_line}"\n# {filler}\n')
    path.chmod(0o755)
    return str(path)

_DEPS_BEFORE_INSTALL = {"cmake": "3.0-1", "llvm": "22.1.6-1", "llvm-libs": "22.1.6-1"}

_DEPS_AFTER_INSTALL = {"cmake": "3.0-1", "llvm": "22.1.8-2", "llvm-libs": "22.1.8-2"}

def _clang_pkgbuild(tmp_path) -> Path:
    pb = tmp_path / "clang" / "PKGBUILD"
    pb.parent.mkdir(parents=True, exist_ok=True)
    pb.write_text(
        "pkgname=clang\npkgver=1.0\npkgrel=1\n"
        "depends=('llvm-libs')\nmakedepends=('cmake' 'llvm')\n"
    )
    return pb

def _pgo_setup(tmp_path, pgo_pkgs, non_pgo_pkgs=None, lib32_pkgs=None):
    """
    Prepare filesystem and objects for a full PGO ToolchainStage run.

    Returns (toml_path, pkgbuild_dir, staging, pgo_store, state, config, options).
    Each package in every list gets a PKGBUILD directory and a fake .pkg.tar.zst
    so staging extraction does not error.
    """
    import json as _json
    non_pgo_pkgs = non_pgo_pkgs or []
    lib32_pkgs   = lib32_pkgs   or []

    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        f"[packages]\n"
        f"pgo = {_json.dumps(pgo_pkgs)}\n"
        f"non_pgo = {_json.dumps(non_pgo_pkgs)}\n"
        f"lib32 = {_json.dumps(lib32_pkgs)}\n"
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in pgo_pkgs + non_pgo_pkgs + lib32_pkgs:
        pb = make_pkgbuild(pkgbuild_dir, name)
        (pb.parent / f"{name}-18.0.0-1-x86_64.pkg.tar.zst").touch()

    state   = PipelineState(tmp_path / "state")
    config  = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    # auto_pgo=True: these tests exercise build behaviour, not prompt gating.
    # The dedicated pgo_confirm tests cover the prompt logic.
    options = make_options(dry_run=False, auto_pgo=True)
    return toml_path, pkgbuild_dir, staging, pgo_store, state, config, options

def _pgo_fake_run_factory(pgo_store, call_log):
    """
    Return a fake makepkg_wrapper.run() that records every invocation and
    writes a settled profraw file when Pass 3 runs (identified by CCACHE_DISABLE
    in extra_env, which is only injected during the training pass).
    """
    def fake_run(pkgbuild_path, options=None):
        env = dict(options.extra_env or {}) if options else {}
        call_log.append({
            "cc":      options.cc_override if options else None,
            "pkgbuild": str(pkgbuild_path),
            "cfe":     options.compiler_flags_extra if options else None,
            "lfe":     options.linker_flags_extra if options else None,
            "variant": options.toolchain_variant if options else None,
            "env":     env,
        })
        # Pass 3 is the training run: it injects CCACHE_DISABLE into extra_env.
        if env.get("CCACHE_DISABLE") == "1":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / f"p{len(call_log)}.profraw")
    return fake_run

def _fake_subprocess_factory(profdata_size=100 * 1024 * 1024):
    """
    Return a subprocess.run side_effect that handles llvm-profdata by writing
    profdata_size bytes to the --output path, and returns success for everything else.
    """
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr     = ""
        result.stdout     = ""
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).write_bytes(b"\x00" * profdata_size)
        return result
    return fake_run

def _run_pgo(tmp_path, pgo_pkgs, non_pgo_pkgs=None, lib32_pkgs=None,
             instrumented=False, runtime_flag="-L/fake -lclang_rt.profile-x86_64",
             profdata_size=100 * 1024 * 1024):
    """
    Run a full PGO ToolchainStage with standard mocking.  Returns call_log.
    instrumented=True simulates a prior aborted Pass 1 leaving the system
    LLVM static libs in an instrumented state.
    """
    toml_path, pkgbuild_dir, staging, pgo_store, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs, non_pgo_pkgs, lib32_pkgs)

    call_log = []
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=_pgo_fake_run_factory(pgo_store, call_log)), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_stage_instrumented"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.assert_staging_has_llvm_cmake"), \
         patch("subprocess.run", side_effect=_fake_subprocess_factory(profdata_size)), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("sys.stdin.isatty", return_value=False), \
         patch("sysforge.pipeline.stages.toolchain.profdata.validate_pgo_environment"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.system_llvm_is_instrumented",
               return_value=instrumented), \
         patch("sysforge.pipeline.stages.toolchain.profdata.profile_runtime_ldflag",
               return_value=runtime_flag):
        ToolchainStage().run(config, state, options)

    return call_log

def _instrument_calls(call_log):
    return [c for c in call_log if "-fprofile-generate" in (c["cfe"] or "")]

def _bootstrap_calls(call_log):
    """Pass 2: non-instrumented build against stage1 — identified by the
    CMAKE_PREFIX_PATH→stage1 env and the absence of training/profile flags."""
    return [
        c for c in call_log
        if c["env"].get("CMAKE_PREFIX_PATH", "").startswith("/var/tmp/sysforge-llvm-stage1")
        and "-fprofile-generate" not in (c["cfe"] or "")
        and "-fprofile-use" not in (c["cfe"] or "")
        and c["env"].get("CCACHE_DISABLE") != "1"
    ]

def _train_calls(call_log):
    return [c for c in call_log if c["env"].get("CCACHE_DISABLE") == "1"]

def _build_calls(call_log):
    return [c for c in call_log if "-fprofile-use" in (c["cfe"] or "")]

def _fake_healthy_clang(_cmd, **_kwargs):
    """subprocess.run side_effect: clang compile probe succeeds, everything else no-ops."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = ""
    result.stderr = ""
    return result

def _confirm_options(*, auto_pgo=False):
    """Build a minimal RunOptions for pgo_confirm tests."""
    return RunOptions(auto_pgo=auto_pgo)

def _single_pass_setup(tmp_path, pkgs=("llvm", "clang")):
    """A non-PGO (single-pass) LLVM toolchain config + built package fixtures."""
    toml_path = tmp_path / "toolchain.toml"
    import json as _json
    # Scope every build path (staging dirs + pgo_store, hence the PGO build lock
    # at <staging1>.parent/sysforge-pgo.lock) into tmp_path. Without this, a test
    # that drives ToolchainStage.run far enough grabs the real
    # /var/tmp/sysforge-pgo.lock and collides with a concurrent live
    # `sysforge run toolchain` (2.5.1-B2 follow-up).
    build = tmp_path / "pgo"
    toml_path.write_text(
        'enabled = true\ncompiler = "llvm"\npgo = false\n'
        f'pgo_staging1 = "{build / "stage1"}"\n'
        f'pgo_staging = "{build / "stage2"}"\n'
        f'pgo_staging3 = "{build / "stage3"}"\n'
        f'pgo_store = "{build / "store"}"\n'
        f"[packages]\npgo = {_json.dumps(list(pkgs))}\nnon_pgo = []\nlib32 = []\n"
    )
    pkgbuild_dir = tmp_path / "builds"
    for name in pkgs:
        pb = make_pkgbuild(pkgbuild_dir, name)
        (pb.parent / f"{name}-22.1.5-1-x86_64.pkg.tar.zst").touch()
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, state_dir=tmp_path / "state")
    return toml_path, state, config, options

def _sentinel_exists(state_dir):
    from sysforge.primitives.stage_sentinel import StageSentinel
    return StageSentinel(state_dir).get_active() is not None

def _bootstrap_missing(*check_ids):
    """Build a smoke_test_compilers stub returning bricks for ``check_ids``."""
    from sysforge.primitives import toolchain_safety as _ts

    msgs = {
        "smoke:clang_missing": "/usr/bin/clang not found",
        "smoke:lld_missing": "lld not found on PATH",
        "smoke:clang_broken": "/usr/bin/clang is not functional",
    }
    return [
        _ts.ToolchainFinding("error", cid, msgs[cid], "install it", is_brick=True)
        for cid in check_ids
    ]

def _impact(consumers=("mesa",)):
    from sysforge.primitives.toolchain_safety import SonameImpact
    return SonameImpact("libLLVM.so.22.1", "libLLVM.so.23.0", list(consumers))

def _gate_map(tmp_path):
    """A pkgbuild_map whose 'llvm' parses to a real pkgver."""
    return {"llvm": make_pkgbuild(tmp_path, "llvm")}

def _patch_rebuild(monkeypatch, outcome, *, resolve_fail=()):
    from sysforge import build_core

    def fake_find(pkg, config):
        if pkg in resolve_fail:
            raise FileNotFoundError(pkg)
        return Path(f"/src/{pkg}/PKGBUILD")

    monkeypatch.setattr("sysforge.primitives.config.find_pkgbuild", fake_find)
    monkeypatch.setattr(
        build_core, "target_from_pkgbuild",
        lambda p: MagicMock(pkgbase=p.parent.name),
    )
    monkeypatch.setattr(build_core, "build_and_install", lambda *a, **k: outcome)
    monkeypatch.setattr("sysforge.pipeline.state.get_toolchain_variant", lambda s: "pgo_llvm")

def _bolt_opts(dry_run=False):
    from types import SimpleNamespace
    return SimpleNamespace(dry_run=dry_run, state_dir=None, no_update=True,
                           makepkg_flags=[])

def _identity(**kw):
    from sysforge.pipeline.stages.toolchain import ToolchainIdentity

    return ToolchainIdentity(**kw)


def toolchain_cfg(**toml) -> ToolchainConfig:
    """Build a :class:`ToolchainConfig` from toolchain.toml-shaped kwargs.

    Tests keep expressing intent in the TOML keys a user actually writes
    (``toolchain_cfg(bolt={"enabled": True})``) while exercising the same
    ``from_toml`` path the stage uses — so a defaulting bug cannot pass here and
    fail in production, which a hand-built dataclass would allow (3.2.0-F6).
    """
    return ToolchainConfig.from_toml(dict(toml))
