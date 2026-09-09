# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_stage_toolchain.py — ToolchainStage.run() end to end.

Whole-stage behaviour: the GCC register-only path, the LLVM single pass,
the PGO 4-pass, skip_build, repo-install mode, and the sequencing between
gates, build and install. The per-module unit tests live in the sibling
test_toolchain_*.py files (3.2.0-F2).

Mocks makepkg_wrapper.run() and subprocess so nothing real is built.
"""
from pathlib import Path
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_LIB32
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_NON_PGO
from sysforge.pipeline.stages.toolchain.constants import DEFAULT_LLVM_PGO
from sysforge.pipeline.stages.toolchain.stage import ToolchainStage
from sysforge.pipeline.state import PipelineState
from unittest.mock import MagicMock
from unittest.mock import patch
import pytest

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _bootstrap_missing,
    _make_old_profraw,
    _pgo_setup,
    _sentinel_exists,
    _single_pass_setup,
    _toolchain_gates_clean,
    _write_packages_repo_mode,
    make_options,
    make_pkgbuild,
)


def test_toolchain_stage_noop_when_absent(tmp_path):
    state = PipelineState(tmp_path / "state")
    options = make_options()
    config = {}

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH",
               tmp_path / "nonexistent.toml"):
        ToolchainStage().run(config, state, options)

    # No result written
    assert state.get_stage_result("toolchain") == {}

def test_toolchain_stage_gcc_registers_paths_without_building(tmp_path):
    """compiler="gcc" writes /usr/bin/gcc paths into state and returns
    without resolving PKGBUILDs, syncing sources, or invoking makepkg."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')

    state = PipelineState(tmp_path / "state")
    # Empty pkgbuild dir on purpose: if the stage tries to resolve any
    # PKGBUILD, the resolver will raise — proving no build path runs.
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds"
               ".resolve_all_pkgbuilds") as resolve_mock:
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"
    assert "ld" not in result
    assert result["variant"] == "gcc"
    makepkg_mock.assert_not_called()
    resolve_mock.assert_not_called()

def test_toolchain_stage_default_compiler_is_gcc_register_only(tmp_path):
    """Implicit default (no 'compiler' key) resolves to gcc and registers
    system paths without building."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\n')

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock:
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"
    assert "ld" not in result
    assert result["variant"] == "gcc"
    makepkg_mock.assert_not_called()

def test_toolchain_stage_llvm_no_pgo_dry_run(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"
    assert result["variant"] == "stock_llvm"

def test_toolchain_stage_llvm_pgo_dry_run(tmp_path):
    staging = tmp_path / "staging"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"
    assert result["variant"] == "pgo_llvm"

def test_toolchain_stage_llvm_no_pgo_pacman_installs_from_repo(tmp_path):
    """compiler=llvm, pgo=false, repo_mode=pacman → install the LLVM suite from
    the repos via install_repo_pkgs; never resolve PKGBUILDs or build."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    state = PipelineState(tmp_path / "state")
    # Empty pkgbuild dir: any build path would raise on resolution.
    config = {
        "paths": {"pkgbuild_src_dir": str(tmp_path / "empty")},
        "packages_file": _write_packages_repo_mode(tmp_path, "pacman"),
    }
    options = make_options(dry_run=False, state_dir=str(tmp_path / "sdir"))

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds"
               ".resolve_all_pkgbuilds") as resolve_mock:
        ToolchainStage().run(config, state, options)

    install_mock.assert_called_once()
    installed = install_mock.call_args.args[0]
    assert set(DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO).issubset(set(installed))
    resolve_mock.assert_not_called()

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"
    assert result["variant"] == "stock_llvm"

def test_toolchain_stage_llvm_no_pgo_pacman_dry_run_skips_install(tmp_path):
    """Dry-run repo-install: log intent, write state, but never call pacman."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    state = PipelineState(tmp_path / "state")
    config = {
        "paths": {"pkgbuild_src_dir": str(tmp_path / "empty")},
        "packages_file": _write_packages_repo_mode(tmp_path, "pacman"),
    }
    options = make_options(dry_run=True, state_dir=str(tmp_path / "sdir"))

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock:
        ToolchainStage().run(config, state, options)

    install_mock.assert_not_called()
    result = state.get_stage_result("toolchain")
    assert result["variant"] == "stock_llvm"

def test_toolchain_stage_llvm_no_pgo_source_mode_builds(tmp_path):
    """compiler=llvm, pgo=false, repo_mode=build_from_source → single-pass build
    (no repo install)."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {
        "paths": {"pkgbuild_src_dir": str(pkgbuild_dir)},
        "packages_file": _write_packages_repo_mode(tmp_path, "build_from_source"),
    }
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock:
        ToolchainStage().run(config, state, options)

    install_mock.assert_not_called()
    result = state.get_stage_result("toolchain")
    assert result["variant"] == "stock_llvm"

def test_toolchain_stage_llvm_pgo_pacman_still_builds_from_source(tmp_path):
    """PGO wins over repo_mode: pgo=true, repo_mode=pacman → build from source,
    never a repo install."""
    staging = tmp_path / "staging"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\npgo_staging = "{staging}"\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {
        "paths": {"pkgbuild_src_dir": str(pkgbuild_dir)},
        "packages_file": _write_packages_repo_mode(tmp_path, "pacman"),
    }
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock:
        ToolchainStage().run(config, state, options)

    install_mock.assert_not_called()
    result = state.get_stage_result("toolchain")
    assert result["variant"] == "pgo_llvm"

def test_toolchain_stage_skip_build_reports_pgo_llvm_when_profdata_present(tmp_path):
    """skip_build = true with a profdata + version sidecar on disk reports
    variant=pgo_llvm (reflects what's installed, not just the stage's action)."""
    pgo_store = tmp_path / "pgo_store"
    pgo_store.mkdir()
    (pgo_store / "clang.profdata").write_bytes(b"fake")
    (pgo_store / "clang.profdata.version").write_text("19")

    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        'enabled = true\ncompiler = "llvm"\nskip_build = true\n'
        f'pgo_store = "{pgo_store}"\n'
    )

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock:
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["variant"] == "pgo_llvm"
    makepkg_mock.assert_not_called()

def test_toolchain_stage_skip_build_reports_stock_llvm_when_profdata_absent(tmp_path):
    """skip_build = true with no profdata on disk reports variant=stock_llvm."""
    pgo_store = tmp_path / "pgo_store_empty"
    pgo_store.mkdir()  # exists but has no profdata files

    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        'enabled = true\ncompiler = "llvm"\nskip_build = true\n'
        f'pgo_store = "{pgo_store}"\n'
    )

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock:
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["variant"] == "stock_llvm"
    makepkg_mock.assert_not_called()

def test_toolchain_stage_pgo_calls_makepkg_four_passes(tmp_path):
    """Verify makepkg_wrapper.run is called once per package per pass with correct PGO flags."""
    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, auto_pgo=True)

    call_log = []
    def fake_run(pkgbuild_path, options=None):
        call_log.append({
            "cc": options.cc_override if options else None,
            "flags": list(options.extra_flags or []) if options else [],
            "cfe": options.compiler_flags_extra if options else None,
            "env": dict(options.extra_env) if options and options.extra_env else {},
            "owner_stage": options.owner_stage if options else None,
        })
        # Simulate Pass 3: instrumented clang running as CC writes a profraw file
        if options and options.cc_override == "/usr/bin/clang":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / "default_0.profraw")

    # Fake .pkg.tar.zst for pass-3 staging extraction
    pkg_dir = pkgbuild_dir / "llvm"
    (pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst").touch()

    def fake_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        # Create the --output file so atomic rename in do_profraw_merge succeeds
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_stage_instrumented"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.run_llvm_preflight"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    # non_pgo=[] here, so Pass 4 is a single pgo-only sub-pass (4a): no non_pgo
    # suite to stage against → no 4b/4c. 3 makepkg calls: pass 1 (live system
    # clang — pinned, not the profile's CC=gcc), pass 3 (stage1 clang), pass 4a
    # (system CC fallback — no staged cc here).
    # The non_pgo coherent-staging (4b → stage3) is covered by the dedicated
    # test_build_non_pgo_links_against_staged_optimized_libllvm_* tests.
    assert len(call_log) == 3
    assert call_log[0]["cc"] == "/usr/bin/clang"            # pass 1: pinned clang
    assert call_log[1]["cc"] == "/usr/bin/clang"            # pass 3: pass-1 clang
    assert call_log[2]["cc"].endswith("/usr/bin/clang")     # pass 4a: system fallback
    # All PGO passes force a clean build and overwrite PKGDEST artifacts from
    # the prior pass (--force); none pass --install (install is via pgo_install)
    assert "--cleanbuild" in call_log[0]["flags"]
    assert "--cleanbuild" in call_log[1]["flags"]
    assert "--cleanbuild" in call_log[2]["flags"]
    assert "--force" in call_log[0]["flags"]
    assert "--force" in call_log[1]["flags"]
    assert "--force" in call_log[2]["flags"]
    assert "--install" not in call_log[0]["flags"]
    assert "--install" not in call_log[1]["flags"]
    assert "--install" not in call_log[2]["flags"]
    # Only the final install-bearing pass (Pass 4) stamps owner_stage so
    # `sysforge update` skips the LLVM suite; intermediate passes leave it None.
    assert call_log[0]["owner_stage"] is None
    assert call_log[1]["owner_stage"] is None
    assert call_log[2]["owner_stage"] == "toolchain"
    # Pass 1 injects -fprofile-generate so the installed clang is instrumented
    assert call_log[0]["cfe"] is not None
    assert "-fprofile-generate=" in call_log[0]["cfe"]
    assert str(pgo_store) in call_log[0]["cfe"]
    # Pass 3 has no extra compiler flags; profraw is generated by the instrumented CC running
    assert call_log[1]["cfe"] is None
    # Pass 4 injects -fprofile-use (IR PGO, matches -fprofile-generate)
    assert call_log[2]["cfe"] is not None
    assert "-fprofile-use=" in call_log[2]["cfe"]
    assert "-fprofile-correction" not in call_log[2]["cfe"]

    # Path B: Pass 3 picks up the staged Pass-1 libLLVM via env injection so
    # the live /usr stays untouched. Pass 4 redirects at the Pass-3 stage2.
    stage1_lib = "/var/tmp/sysforge-llvm-stage1/usr/lib"
    train_env = call_log[1]["env"]
    assert train_env.get("LLVM_PROFILE_FILE", "").startswith(str(pgo_store)), \
        "Pass 3 must set LLVM_PROFILE_FILE for profraw routing"
    assert train_env.get("CCACHE_DISABLE") == "1"
    assert train_env.get("SCCACHE_DISABLE") == "1"
    assert train_env.get("LD_LIBRARY_PATH", "").startswith(stage1_lib), \
        "Pass 3 must redirect dyld at stage1 before /usr"
    assert train_env.get("CMAKE_PREFIX_PATH", "").startswith(
        "/var/tmp/sysforge-llvm-stage1/usr"
    )
    build_env = call_log[2]["env"]
    assert build_env.get("LLVM_PROFILE_FILE") == "", \
        "Pass 4a must clear LLVM_PROFILE_FILE so Pass-3 training env doesn't leak"
    # Pass 4a (pgo sub-pass) on the system-clang fallback (the test setup does
    # not materialise staged_cc): stage_env must NOT be injected. System
    # /usr/bin/clang is linked against the live /usr libLLVM (full target list);
    # redirecting dyld at stage2's stripped LLVM_TARGETS_TO_BUILD-restricted
    # libLLVM via LD_LIBRARY_PATH triggers symbol lookup errors for missing
    # target init functions (e.g. LLVMInitializeBPFTarget). (The non_pgo
    # sub-pass 4b DOES redirect cmake — at stage3, the final shipped libLLVM
    # with full configured targets — but there is no non_pgo here.)
    assert "LD_LIBRARY_PATH" not in build_env, \
        "Pass 4a must NOT redirect dyld at stage2 when falling back to system clang"
    assert "CMAKE_PREFIX_PATH" not in build_env, \
        "Pass 4a (pgo sub-pass) does not redirect cmake; only 4b does, at stage3"

    # Sidecar is written after Pass 3 (not after Pass 4 install) so an aborted
    # Pass 4 still leaves recoverable profdata.  The major is derived from the
    # in-tree PKGBUILD pkgver (1.0 from make_pkgbuild) → major "1".
    sidecar = pgo_store / "clang.profdata.version"
    assert sidecar.exists(), "Sidecar must be written after Pass 3 completes"
    assert sidecar.read_text().strip() == "1"

def test_toolchain_stage_pgo_sidecar_persists_after_build_failure(tmp_path):
    """When Pass 4 fails (the version-skew failure mode the dyld fix exists to prevent
    can still strike other call paths), the sidecar written after Pass 3 must persist
    so the next invocation can short-circuit through profdata reuse."""
    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, auto_pgo=True)

    call_log = []
    def fake_run(pkgbuild_path, options=None):
        call_log.append({"cc": options.cc_override if options else None})
        if options and options.cc_override == "/usr/bin/clang":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / "default_0.profraw")
        # Pass 4 is the 3rd makepkg_run call — simulate a build failure there
        # (mirrors the real LLVMInitializeBPFTarget cmake probe abort).
        if len(call_log) == 3:
            raise RuntimeError("simulated Pass 4 build failure")

    pkg_dir = pkgbuild_dir / "llvm"
    (pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst").touch()

    def fake_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_stage_instrumented"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.run_llvm_preflight"), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="simulated Pass 4"):
            ToolchainStage().run(config, state, options)

    # Pass 4 raised — but the sidecar (written between Pass 3 and Pass 4) must
    # still be on disk so the next `sysforge run toolchain` sees ready profdata
    # and short-circuits through the reuse prompt rather than asking the user
    # to purge and start over.
    sidecar = pgo_store / "clang.profdata.version"
    assert sidecar.exists(), \
        "Sidecar must survive a Pass 4 failure so the next run can reuse profdata"
    assert sidecar.read_text().strip() == "1"
    # And staging is intentionally NOT removed on Pass 4 failure (only cleared
    # on full-flow success), so the user can inspect it.
    assert staging.exists(), "Staging must persist after Pass 4 failure for inspection"

def test_toolchain_stage_pgo_build_redirects_dyld_when_clang_staged(tmp_path):
    """When stage2 contains a staged clang (e.g. clang is in the PGO package set),
    Pass 4 must redirect cmake/dyld at stage2 — that clang's NEEDED libLLVM is
    stage2's libLLVM, so the redirect is ABI-coherent."""
    staging   = tmp_path / "staging"
    pgo_store = tmp_path / "pgo_store"
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, auto_pgo=True)

    call_log = []
    def fake_run(pkgbuild_path, options=None):
        call_log.append({
            "cc": options.cc_override if options else None,
            "env": dict(options.extra_env) if options and options.extra_env else {},
        })
        if options and options.cc_override == "/usr/bin/clang":
            pgo_store.mkdir(parents=True, exist_ok=True)
            _make_old_profraw(pgo_store / "default_0.profraw")
        # Materialise a staged clang in stage2 right before Pass 4 reads it
        # (Pass 4 is the 3rd makepkg_run invocation; Pass 3 is the 2nd).
        if len(call_log) == 2:
            staged_bin = staging / "usr" / "bin"
            staged_bin.mkdir(parents=True, exist_ok=True)
            (staged_bin / "clang").touch()
            (staged_bin / "clang++").touch()

    pkg_dir = pkgbuild_dir / "llvm"
    (pkg_dir / "llvm-18.0.0-1-x86_64.pkg.tar.zst").touch()

    def fake_subprocess(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd and "llvm-profdata" in cmd[0]:
            idx = cmd.index("--output")
            Path(cmd[idx + 1]).touch()
        return result

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run), \
         patch("sysforge.primitives.config.parse_system_makepkg_conf", return_value={}), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_stage_instrumented"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.run_llvm_preflight"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("subprocess.run", side_effect=fake_subprocess), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    assert len(call_log) == 3
    # Pass 4 picked up the staged clang from stage2
    assert call_log[2]["cc"] == str(staging / "usr/bin/clang")
    build_env = call_log[2]["env"]
    assert build_env.get("LLVM_PROFILE_FILE") == "", \
        "Pass 4 must clear LLVM_PROFILE_FILE so Pass-3 training env doesn't leak"
    assert build_env.get("LD_LIBRARY_PATH", "").startswith(str(staging / "usr/lib")), \
        "Pass 4 must redirect dyld at stage2 when the staged clang is used"
    assert build_env.get("CMAKE_PREFIX_PATH", "").startswith(str(staging / "usr")), \
        "Pass 4 must redirect cmake at stage2 when the staged clang is used"

def test_toolchain_stage_custom_packages(tmp_path):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        'enabled = true\ncompiler = "llvm"\npgo = false\n'
        '[packages]\npgo = ["llvm", "clang"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")
    make_pkgbuild(pkgbuild_dir, "clang")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=True)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"

def test_toolchain_skip_build_gcc(tmp_path):
    """skip_build=true registers gcc paths in state without building anything."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\nskip_build = true\n')
    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/gcc"
    assert result["cxx"] == "/usr/bin/g++"
    assert "ld" not in result

def test_toolchain_skip_build_llvm(tmp_path):
    """skip_build=true registers clang paths in state without building anything."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\nskip_build = true\n')
    state = PipelineState(tmp_path / "state")

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    result = state.get_stage_result("toolchain")
    assert result["cc"] == "/usr/bin/clang"
    assert result["cxx"] == "/usr/bin/clang++"
    assert result["ld"] == "lld"

def test_toolchain_state_overwrites_on_llvm_to_gcc_switch(tmp_path):
    """llvm→gcc switch must drop the stale 'ld' key from pipeline state.

    The state-write at run() end uses set_stage_result(), which replaces
    the result dict wholesale (state.py:324). A future refactor that
    switched to partial-update semantics would leak ld=lld into the gcc
    run; this test pins the structural overwrite invariant."""
    state = PipelineState(tmp_path / "state")
    toml_path = tmp_path / "toolchain.toml"

    # Run 1: llvm skip_build (no real makepkg)
    toml_path.write_text('enabled = true\ncompiler = "llvm"\nskip_build = true\n')
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())
    r1 = state.get_stage_result("toolchain")
    assert r1.get("ld") == "lld"
    assert r1.get("cc") == "/usr/bin/clang"

    # Run 2: switch to gcc — same state object
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())
    r2 = state.get_stage_result("toolchain")
    assert r2.get("cc") == "/usr/bin/gcc"
    assert "ld" not in r2  # stale 'ld' must not leak across the switch

def test_toolchain_state_overwrites_on_gcc_to_llvm_switch(tmp_path):
    """gcc→llvm switch must populate the 'ld' key (lld)."""
    state = PipelineState(tmp_path / "state")
    toml_path = tmp_path / "toolchain.toml"

    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())
    r1 = state.get_stage_result("toolchain")
    assert r1.get("cc") == "/usr/bin/gcc"
    assert "ld" not in r1

    toml_path.write_text('enabled = true\ncompiler = "llvm"\nskip_build = true\n')
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())
    r2 = state.get_stage_result("toolchain")
    assert r2.get("cc") == "/usr/bin/clang"
    assert r2.get("ld") == "lld"

def test_toolchain_disabled_clears_prior_state(tmp_path):
    """enabled=false must wipe any prior cc/cxx/ld result from state.

    Without this, disabling the stage after a prior llvm run would leave
    clang/lld as the propagated CC/LD for the packages and kernel stages
    even though the user opted out."""
    state = PipelineState(tmp_path / "state")
    state.set_stage_result("toolchain", {
        "cc": "/usr/bin/clang", "cxx": "/usr/bin/clang++", "ld": "lld",
    })
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = false\n')

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    assert state.get_stage_result("toolchain") == {}

def test_toolchain_absent_clears_prior_state(tmp_path):
    """toolchain.toml deleted between runs — same wipe behavior."""
    state = PipelineState(tmp_path / "state")
    state.set_stage_result("toolchain", {"cc": "/usr/bin/clang"})

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH",
               tmp_path / "nonexistent.toml"):
        ToolchainStage().run({}, state, make_options())

    assert state.get_stage_result("toolchain") == {}

def test_toolchain_disabled_no_op_when_no_prior_state(tmp_path):
    """enabled=false with no prior state must not write to state."""
    state = PipelineState(tmp_path / "state")
    state.save = MagicMock(wraps=state.save)
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = false\n')

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run({}, state, make_options())

    state.save.assert_not_called()

def test_toolchain_stage_missing_pkgbuild_raises(tmp_path):
    """LLVM path raises when PKGBUILDs can't be resolved.

    (The GCC path is register-only and never resolves PKGBUILDs, so this
    failure mode only applies to compiler="llvm".)"""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options()

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.aur.is_repo_package", return_value=False), \
         patch("sysforge.primitives.aur.aur_info", return_value={}):
        with pytest.raises(RuntimeError, match="Could not resolve PKGBUILDs"):
            ToolchainStage().run(config, state, options)

def test_validate_pgo_environment_runs_before_instrument(tmp_path):
    """
    Pre-flight validation must fire before Pass 1 starts.
    If it raises (e.g. missing lld), no makepkg_run calls should occur.
    """
    toml_path, _, _, _, state, config, options = \
        _pgo_setup(tmp_path, pgo_pkgs=["llvm"])

    makepkg_calls = []

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=lambda *a, **_: makepkg_calls.append(a)), \
         patch("sysforge.pipeline.stages.toolchain.profdata.validate_pgo_environment",
               side_effect=RuntimeError("lld not found")):
        with pytest.raises(RuntimeError, match="lld not found"):
            ToolchainStage().run(config, state, options)

    assert not makepkg_calls, "No builds should run when pre-flight check fails"

def test_gate1_version_skew_aborts_before_build(tmp_path, monkeypatch):
    """A Gate-1 pkgver-skew brick raises before any makepkg call, no sentinel."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    skew = _ts.ToolchainFinding(
        "error", "pkgver_lockstep", "LLVM PKGBUILD pkgver skew", is_brick=True,
    )
    monkeypatch.setattr(_ts, "check_pkgver_lockstep", lambda pv: skew)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Gate 1 .pkgver_lockstep."):
            ToolchainStage().run(config, state, options)

    makepkg_mock.assert_not_called()
    assert not _sentinel_exists(tmp_path / "state")

def test_gate1_build_space_aborts_overridable(tmp_path, monkeypatch):
    """A build-space brick aborts; --skip-build-space-check bypasses it."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    short = _ts.ToolchainFinding(
        "error", "build_space", "only 5 GiB free", is_brick=True,
    )
    monkeypatch.setattr(_ts, "check_build_space", lambda *a, **k: short)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Gate 1 .build_space."):
            ToolchainStage().run(config, state, options)

        # Override → the brick is skipped and the run proceeds.
        options.skip_build_space_check = True
        ToolchainStage().run(config, state, options)
    assert state.get_stage_result("toolchain")["variant"] == "stock_llvm"

def test_gate1_dry_run_downgrades_brick_to_warning(tmp_path, monkeypatch):
    """In dry-run a Gate-1 brick warns instead of aborting."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    options.dry_run = True

    short = _ts.ToolchainFinding(
        "error", "build_space", "only 5 GiB free", is_brick=True,
    )
    monkeypatch.setattr(_ts, "check_build_space", lambda *a, **k: short)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"):
        ToolchainStage().run(config, state, options)  # must not raise

def test_gate1_auto_installs_missing_bootstrap_clang(tmp_path, monkeypatch):
    """A clean machine with no clang/lld gets the bootstrap suite installed
    by Gate 1 rather than being bricked — the Pass-1 compiler is a build
    prerequisite the pipeline can satisfy itself."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    calls = iter([_bootstrap_missing("smoke:clang_missing", "smoke:lld_missing"), []])
    monkeypatch.setattr(_ts, "smoke_test_compilers", lambda: next(calls))

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock, \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)  # must not raise

    install_mock.assert_called_once_with(["clang", "lld"])
    assert state.get_stage_result("toolchain")["variant"] == "stock_llvm"

def test_gate1_bricks_when_bootstrap_install_does_not_help(tmp_path, monkeypatch):
    """If the re-probe still fails after the install, the brick stands."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    calls = iter([
        _bootstrap_missing("smoke:clang_missing"),
        _bootstrap_missing("smoke:clang_missing"),
    ])
    monkeypatch.setattr(_ts, "smoke_test_compilers", lambda: next(calls))

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs"), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Gate 1 .smoke:clang_missing."):
            ToolchainStage().run(config, state, options)

    makepkg_mock.assert_not_called()

def test_gate1_broken_clang_is_not_auto_installed(tmp_path, monkeypatch):
    """A *broken* clang is a mismatched-package problem, not a missing one —
    reinstalling it blindly is not the remediation, so it bricks directly."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    monkeypatch.setattr(
        _ts, "smoke_test_compilers", lambda: _bootstrap_missing("smoke:clang_broken")
    )

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="Gate 1 .smoke:clang_broken."):
            ToolchainStage().run(config, state, options)

    install_mock.assert_not_called()

def test_gate1_dry_run_previews_bootstrap_install(tmp_path, monkeypatch):
    """Dry-run never mutates: the bootstrap install is previewed, not run."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    options.dry_run = True

    monkeypatch.setattr(
        _ts, "smoke_test_compilers", lambda: _bootstrap_missing("smoke:clang_missing")
    )

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs") as install_mock, \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"):
        ToolchainStage().run(config, state, options)  # must not raise

    install_mock.assert_not_called()

def test_single_pass_builds_without_install_then_batches(tmp_path, monkeypatch):
    """The non-PGO path builds with install=False, audits (Gate 2), then
    installs via pgo_install inside the sentinel — no per-package install."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    build_calls = []

    def fake_run(pkgbuild_path, options=None):
        build_calls.append({
            "install": "--install" in list(options.extra_flags or []) if options else False,
            "owner_stage": options.owner_stage if options else None,
        })

    install_calls = []
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run", side_effect=fake_run), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install",
               side_effect=lambda *a, **k: install_calls.append(a)), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    # Build passes never carry --install (split: install is the caller's job).
    assert build_calls and all(not c["install"] for c in build_calls)
    assert all(c["owner_stage"] == "toolchain" for c in build_calls)
    # Exactly one install step (batched), reached after the build.
    assert len(install_calls) == 1
    assert not _sentinel_exists(tmp_path / "state")  # cleared on success

def test_gate3_failure_auto_restores_and_clears_sentinel(tmp_path, monkeypatch):
    """Gate-3 verify failure → snapshot rollback succeeds → sentinel cleared, raise."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    # A complete snapshot (every member cached) so rollback can run.
    cached = tmp_path / "cache" / "llvm-22.1.5-1-x86_64.pkg.tar.zst"
    cached.parent.mkdir(parents=True)
    cached.touch()
    monkeypatch.setattr(
        "sysforge.primitives.pacman.cached_pkg_files_for",
        lambda names: {n: cached for n in names},
    )

    restored = {}
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install",
               return_value=["clang --version: exit 127"]), \
         patch("sysforge.primitives.pacman.batch_install_pkgs",
               side_effect=lambda files: restored.setdefault("files", files) or True), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="prior toolchain was restored"):
            ToolchainStage().run(config, state, options)

    assert restored.get("files")  # rollback ran
    assert not _sentinel_exists(tmp_path / "state")  # system whole → cleared

def test_gate3_expected_targets_sourced_from_resolved_set(tmp_path, monkeypatch):
    """Gate 3 verifies against the *resolved* LLVM targets (resolve_or_detect),
    not just toolchain.toml [llvm] targets — so check #3 runs on autodetect
    hosts where that key is unset (the previously-skipped, unverified gap)."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    monkeypatch.setattr(
        "sysforge.primitives.llvm_targets.resolve_or_detect_llvm_targets",
        lambda tc, hw: ["X86", "NVPTX", "AMDGPU"],
    )
    seen = {}

    def spy_verify(expected_targets=None):
        seen["targets"] = expected_targets
        return []

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install",
               side_effect=spy_verify), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    assert seen["targets"] == ["X86", "NVPTX", "AMDGPU"]

def test_gate3_consumer_symbol_brick_triggers_rollback(tmp_path, monkeypatch):
    """A post-install graphics-consumer symbol brick is folded into Gate-3
    issues → snapshot rollback fires even though verify_llvm_install is clean.
    This is the post-install safety net for a target-reduced libLLVM."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    cached = tmp_path / "cache" / "llvm-22.1.5-1-x86_64.pkg.tar.zst"
    cached.parent.mkdir(parents=True)
    cached.touch()
    monkeypatch.setattr(
        "sysforge.primitives.pacman.cached_pkg_files_for",
        lambda names: {n: cached for n in names},
    )
    monkeypatch.setattr(
        "sysforge.primitives.llvm_targets.resolve_or_detect_llvm_targets",
        lambda tc, hw: ["X86", "AMDGPU"],
    )
    from sysforge.primitives import toolchain_safety as _ts
    brick = [_ts.ToolchainFinding(
        "error", "libllvm_consumer_symbols",
        "libgallium-26.so: dropped LLVM target(s): AMDGPU", "rebuild",
        is_brick=True,
    )]
    monkeypatch.setattr(_ts, "check_installed_consumer_symbols", lambda: brick)

    restored = {}
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install",
               return_value=[]), \
         patch("sysforge.primitives.pacman.batch_install_pkgs",
               side_effect=lambda files: restored.setdefault("files", files) or True), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="prior toolchain was restored"):
            ToolchainStage().run(config, state, options)

    assert restored.get("files")  # rollback ran because the consumer arm bricked
    assert not _sentinel_exists(tmp_path / "state")

def test_gate3_failure_restore_fails_keeps_sentinel(tmp_path, monkeypatch):
    """Gate-3 fails AND rollback fails → sentinel left in place for next-run recovery."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    cached = tmp_path / "cache" / "llvm-22.1.5-1-x86_64.pkg.tar.zst"
    cached.parent.mkdir(parents=True)
    cached.touch()
    monkeypatch.setattr(
        "sysforge.primitives.pacman.cached_pkg_files_for",
        lambda names: {n: cached for n in names},
    )

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install",
               return_value=["clang --version: exit 127"]), \
         patch("sysforge.primitives.pacman.batch_install_pkgs",
               return_value=False), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="rollback could not complete"):
            ToolchainStage().run(config, state, options)

    assert _sentinel_exists(tmp_path / "state")  # kept for recovery

def test_build_failure_leaves_no_sentinel(tmp_path, monkeypatch):
    """A build-pass failure raises before the sentinel scope — none left behind."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run",
               side_effect=RuntimeError("build blew up")), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sys.stdin.isatty", return_value=False):
        with pytest.raises(RuntimeError, match="build blew up"):
            ToolchainStage().run(config, state, options)

    assert not _sentinel_exists(tmp_path / "state")

def test_gcc_register_only_skips_all_gates(tmp_path, monkeypatch):
    """The gcc path registers paths and returns — no Gate 1, no smoke test, no build."""
    from sysforge.primitives import toolchain_safety as _ts
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False, state_dir=tmp_path / "state")

    gate_calls = []
    monkeypatch.setattr(_ts, "smoke_test_compilers",
                        lambda: gate_calls.append("smoke") or [])
    monkeypatch.setattr(_ts, "check_build_space",
                        lambda *a, **k: gate_calls.append("space"))

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run") as makepkg_mock:
        ToolchainStage().run(config, state, options)

    assert state.get_stage_result("toolchain")["variant"] == "gcc"
    assert gate_calls == []  # no gate ran on the register-only path
    makepkg_mock.assert_not_called()

def test_toolchain_stage_gcc_never_assesses_soname(tmp_path, monkeypatch):
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n')

    def _boom(*a, **k):
        raise AssertionError("gcc path must not assess libLLVM soname")

    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact", _boom
    )
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    options = make_options(dry_run=False)

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, options)

    assert state.get_stage_result("toolchain")["variant"] == "gcc"

def test_change_extras_returns_empty_before_run():
    """Without a captured before-state there is nothing honest to report."""
    stage = ToolchainStage()
    stage._identity_before = None
    assert stage.change_extras({}, MagicMock(), MagicMock()) == []

def test_change_extras_labels_the_block(tmp_path):
    """The block carries the Toolchain: label the renderer indents under."""
    from sysforge.pipeline.stages.toolchain import ToolchainIdentity

    stage = ToolchainStage()
    stage._identity_before = ToolchainIdentity(variant="system")
    state = MagicMock()
    state.get_stage_result.return_value = {"variant": "gcc"}
    blocks = stage.change_extras({}, state, MagicMock(state_dir=tmp_path))
    assert len(blocks) == 1
    assert blocks[0].label == "Toolchain:"
    assert "variant: system → gcc" in blocks[0].lines
