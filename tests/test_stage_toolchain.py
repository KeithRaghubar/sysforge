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

def test_toolchain_stage_header_states_only_what_is_known(tmp_path, capsys):
    """3.5.0-B28: before PKGBUILD resolution folds split packages, the
    pgo/non-pgo split is unknown (the build plan prints the real one), and PGO
    reads on/off rather than a Python bool."""
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\npgo_staging = "{tmp_path / "s"}"\n'
    )
    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, PipelineState(tmp_path / "state"),
                             make_options(dry_run=True))
    out, err = capsys.readouterr()
    header = next(ln for ln in (out + err).splitlines() if "Compiler:" in ln)
    total = len(DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32)
    assert "PGO: on" in header and "True" not in header
    assert header.rstrip().endswith(f"Packages: {total}")


def test_toolchain_stage_llvm_pgo_dry_run_keeps_staging3(tmp_path):
    """3.3.0-B9: Pass 4 cleared staging3 before re-extracting even on a dry run.
    A real `run toolchain` in Pass 4b compiles clang against that prefix, so a
    concurrent dry run deleted its headers mid-build (clang 23 then fell back
    to /usr/include's LLVM 22 headers)."""
    staging3 = tmp_path / "stage3"
    header = staging3 / "usr" / "include" / "llvm" / "ADT" / "DenseMap.h"
    header.parent.mkdir(parents=True)
    header.touch()
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text(
        f'enabled = true\ncompiler = "llvm"\npgo = true\n'
        f'pgo_staging = "{tmp_path / "staging"}"\npgo_staging3 = "{staging3}"\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    for name in DEFAULT_LLVM_PGO + DEFAULT_LLVM_NON_PGO + DEFAULT_LLVM_LIB32:
        make_pkgbuild(pkgbuild_dir, name)

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, make_options(dry_run=True))

    assert header.exists()

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
        f'pgo_staging1 = "{tmp_path / "staging1"}"\npgo_staging3 = "{tmp_path / "staging3"}"\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, yes=True)

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

    # validate_pgo_environment needs a real /usr/bin/clang (3.4.0-DEV1).
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.profdata.validate_pgo_environment"), \
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
    stage1_lib = str(tmp_path / "staging1" / "usr" / "lib")
    train_env = call_log[1]["env"]
    assert train_env.get("LLVM_PROFILE_FILE", "").startswith(str(pgo_store)), \
        "Pass 3 must set LLVM_PROFILE_FILE for profraw routing"
    assert train_env.get("CCACHE_DISABLE") == "1"
    assert train_env.get("SCCACHE_DISABLE") == "1"
    assert train_env.get("LD_LIBRARY_PATH", "").startswith(stage1_lib), \
        "Pass 3 must redirect dyld at stage1 before /usr"
    assert train_env.get("CMAKE_PREFIX_PATH", "").startswith(
        str(tmp_path / "staging1" / "usr")
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
        f'pgo_staging1 = "{tmp_path / "staging1"}"\npgo_staging3 = "{tmp_path / "staging3"}"\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, yes=True)

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

    # validate_pgo_environment needs a real /usr/bin/clang (3.4.0-DEV1).
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.profdata.validate_pgo_environment"), \
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
        f'pgo_staging1 = "{tmp_path / "staging1"}"\npgo_staging3 = "{tmp_path / "staging3"}"\n'
        f'pgo_staging = "{staging}"\npgo_store = "{pgo_store}"\n'
        '[packages]\npgo = ["llvm"]\nnon_pgo = []\nlib32 = []\n'
    )

    pkgbuild_dir = tmp_path / "builds"
    make_pkgbuild(pkgbuild_dir, "llvm")

    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(pkgbuild_dir)}}
    options = make_options(dry_run=False, yes=True)

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

    # validate_pgo_environment needs a real /usr/bin/clang (3.4.0-DEV1).
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.profdata.validate_pgo_environment"), \
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
    """A build-space brick aborts; --allow-low-build-space bypasses it."""
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
        options.allow_low_build_space = True
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
    # The snapshot keeps only installed names (3.5.0-B76); pin "installed"
    # rather than read the host's pacman DB, which a CI container lacks.
    monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                        lambda name: "1")

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
    # The snapshot keeps only installed names (3.5.0-B76); pin "installed"
    # rather than read the host's pacman DB, which a CI container lacks.
    monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                        lambda name: "1")
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
    monkeypatch.setattr(_ts, "check_installed_consumer_symbols", lambda **kw: brick)

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

def test_gate3_consumer_check_excludes_what_the_batch_ships(tmp_path, monkeypatch):
    """3.5.0-B20: the post-install consumer gate skips every pkgname the built
    batch ships (lldb, libclc, ... from the llvm pkgbase): the stage just
    replaced those, so the installed copies are not stale consumers."""
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    from sysforge.primitives import toolchain_safety as _ts
    built = [tmp_path / "llvm-libs.pkg.tar.zst", tmp_path / "lldb.pkg.tar.zst"]
    monkeypatch.setattr(
        "sysforge.pipeline.stages.toolchain.profdata.collect_pgo_packages",
        lambda m: built,
    )
    monkeypatch.setattr(_ts, "shipped_pkgnames",
                        lambda files: {"llvm-libs", "lldb"} if files == built else set())
    seen = {}
    monkeypatch.setattr(_ts, "check_installed_consumer_symbols",
                        lambda **kw: seen.update(exclude=kw.get("exclude")) or [])
    monkeypatch.setattr(_ts, "check_system_consumer_symbols", lambda pkgs: [])
    monkeypatch.setattr(_ts, "scan_abi_hazards", lambda pkgs: [])

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install"), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install",
               return_value=[]), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)

    assert seen["exclude"] == {"llvm-libs", "lldb"}

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
    # The snapshot keeps only installed names (3.5.0-B76); pin "installed"
    # rather than read the host's pacman DB, which a CI container lacks.
    monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                        lambda name: "1")

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


def test_gcc_register_only_path_never_hands_off_to_the_bench(tmp_path, monkeypatch):
    """3.4.0-F11, gcc path: nothing was built, so there is nothing to measure."""
    from sysforge.pipeline.stages.toolchain import measure

    calls = []
    monkeypatch.setattr(measure, "after_install", lambda *a: calls.append(a))
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "gcc"\n[bench]\nafter_build = true\n')
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")}}
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path):
        ToolchainStage().run(config, state, make_options(dry_run=False))
    assert calls == []


# ---------------------------------------------------------------------------
# 3.5.0-F2: upstream's single llvm pkgbase
# ---------------------------------------------------------------------------

def _merge_into_llvm(pkgbuild_dir, members):
    """Rewrite the fixture llvm PKGBUILD as the merged pkgbase and drop the
    per-package dirs of the members it now builds."""
    import shutil
    (pkgbuild_dir / "llvm" / "PKGBUILD").write_text(
        f"pkgbase=llvm\npkgname=({' '.join(repr(m) for m in members)})\n"
        "pkgver=23.1.3\npkgrel=4\n"
    )
    for m in members:
        if m != "llvm" and (pkgbuild_dir / m).exists():
            shutil.rmtree(pkgbuild_dir / m)


def test_pgo_plan_builds_merged_members_in_the_pgo_passes(tmp_path):
    toml_path, pkgbuild_dir, _s, _p, state, config, options = _pgo_setup(
        tmp_path, ["llvm", "llvm-libs"], ["clang", "lld", "spirv-llvm-translator"])
    _merge_into_llvm(pkgbuild_dir, ["llvm", "llvm-libs", "clang", "lld"])
    seen = {}

    def fake_inner(pgo_map, non_pgo_map, lib32_map, *a, **k):
        seen["pgo"], seen["non_pgo"] = set(pgo_map), set(non_pgo_map)
        raise RuntimeError("stop after planning")

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.pgo.build_llvm_pgo_inner",
               side_effect=fake_inner), \
         pytest.raises(RuntimeError, match="stop after planning"):
        ToolchainStage().run(config, state, options)
    assert seen["pgo"] == {"llvm", "llvm-libs", "clang", "lld"}
    assert seen["non_pgo"] == {"spirv-llvm-translator"}


def test_install_and_snapshot_cover_installed_pkgbase_members(tmp_path, monkeypatch):
    """lldb comes from the llvm pkgbase and is installed: the toolchain must
    install its new build (and be able to roll it back); wasi-libc++ is
    built but was never installed, so it is left out."""
    from sysforge.primitives import pacman
    toml_path, state, config, options = _single_pass_setup(tmp_path, pkgs=("llvm", "clang"))
    pkgbuild_dir = Path(config["paths"]["pkgbuild_src_dir"])
    _merge_into_llvm(pkgbuild_dir, ["llvm", "llvm-libs", "clang", "lldb", "wasi-libc++"])
    installed = {"llvm": "1", "clang": "1", "lldb": "1"}
    monkeypatch.setattr(pacman, "get_all_installed_packages", lambda: installed)
    monkeypatch.setattr(pacman, "get_installed_version", installed.get)
    snap_names = []
    monkeypatch.setattr(pacman, "cached_pkg_files_for",
                        lambda names: snap_names.extend(names) or {n: None for n in names})
    installs = []
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.passes.makepkg_run"), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.profdata.pgo_install",
               side_effect=lambda *a, **k: installs.append(k)), \
         patch("sysforge.pipeline.stages.toolchain.verify.verify_llvm_install", return_value=[]), \
         patch("sys.stdin.isatty", return_value=False):
        ToolchainStage().run(config, state, options)
    assert installs and installs[0]["members"] == {"llvm", "clang", "lldb"}
    assert "lldb" in snap_names and "wasi-libc++" not in snap_names


# ---------------------------------------------------------------------------
# 3.5.0-F7: --bolt-only routes around the build
# ---------------------------------------------------------------------------

def test_bolt_only_calls_bolt_only_and_never_the_build(tmp_path):
    toml_path, pkgbuild_dir, _s, pgo_store, state, config, options = _pgo_setup(
        tmp_path, ["llvm", "llvm-libs"], ["spirv-llvm-translator"])
    options.bolt_only = True
    seen = {}

    def fake_bolt_only(pgo_map, non_pgo_map, lib32_map, store, opts, *, tcfg):
        seen["maps"] = (set(pgo_map), set(non_pgo_map), store)
        raise RuntimeError("stop after bolt")

    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sysforge.pipeline.stages.toolchain.pgo.build_llvm_pgo_inner",
               side_effect=AssertionError("built")), \
         patch("sysforge.pipeline.stages.toolchain.pgo.bolt_only", side_effect=fake_bolt_only), \
         pytest.raises(RuntimeError, match="stop after bolt"):
        ToolchainStage().run(config, state, options)
    assert seen["maps"] == ({"llvm", "llvm-libs"}, {"spirv-llvm-translator"}, pgo_store)


def test_bolt_only_refuses_a_non_pgo_toolchain(tmp_path):
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    options.bolt_only = True
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pgo.build_llvm_single",
               side_effect=AssertionError("built")), \
         pytest.raises(RuntimeError, match="--bolt-only needs"):
        ToolchainStage().run(config, state, options)


def test_bolt_only_confirmation_does_not_promise_a_build(monkeypatch):
    """3.5.0-F7: --bolt-only builds nothing, so its confirmation must not ask
    to 'proceed with toolchain build'; it still installs, so it still asks."""
    from sysforge.pipeline.stages.toolchain import pkgbuilds
    asked = []
    monkeypatch.setattr(pkgbuilds.prompt, "prompt_choice",
                        lambda text, **kw: asked.append(text) or "y")
    pkgbuilds.confirm_or_abort(None, mode="--bolt-only")
    pkgbuilds.confirm_or_abort(None)
    assert "toolchain build" not in asked[0]
    assert "BOLT" in asked[0] and "install" in asked[0]
    assert asked[1] == "Proceed with toolchain build? [y/N]: "


def test_bolt_only_plan_names_the_mode_and_omits_the_build_staging(tmp_path, capsys):
    from sysforge.pipeline.stages.toolchain import gates
    from tests.toolchain_helpers import toolchain_cfg
    opts = make_options(dry_run=False)
    opts.bolt_only = True
    gates.log_toolchain_resolution_summary(
        compiler="llvm", pgo=True, variant="pgo_llvm", pgo_pkgs=["llvm"],
        non_pgo_pkgs=[], lib32_pkgs=[], staging1=tmp_path / "s1", staging=tmp_path / "s2",
        staging3=tmp_path / "s3", pgo_store=tmp_path / "store", tcfg=toolchain_cfg(),
        options=opts, snapshot={})
    out, err = capsys.readouterr()
    text = out + err
    assert "--bolt-only" in text and "nothing is built" in text
    assert "Toolchain build plan" not in text and "staging1" not in text


def test_bolt_only_skips_the_build_space_gate(tmp_path, monkeypatch):
    """3.5.0-F7: --bolt-only builds nothing, so Gate 1's build-space headroom
    (sized for a multi-hour build) does not apply; the other checks still do."""
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import toolchain_safety as _ts
    from tests.toolchain_helpers import toolchain_cfg
    monkeypatch.setattr(_ts, "check_build_space",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("space checked")))
    monkeypatch.setattr(gates, "install_bootstrap_compilers", lambda *a, **k: [])
    skews = []
    monkeypatch.setattr(_ts, "check_pkgver_lockstep", lambda v: skews.append(v))
    opts = make_options(dry_run=False)
    opts.bolt_only = True
    gates.gate1_preflight([], tmp_path / "s1", tmp_path / "s2", tmp_path / "store",
                          {}, opts, toolchain_cfg(), snapshot={})
    assert skews  # the lockstep check still ran


# ---------------------------------------------------------------------------
# 3.5.0-B30: sudo is authorized before the install sentinel goes down
# ---------------------------------------------------------------------------

def test_unauthorized_sudo_installs_nothing_and_leaves_no_sentinel(tmp_path, monkeypatch):
    """An unanswered sudo prompt means nothing ran: no install attempt, no
    sentinel (whose recovery would replace the live toolchain), and a pointer
    to installing what was built without rebuilding it."""
    from sysforge.primitives import sudo_session
    toml_path, _d, _s, _p, state, config, options = _pgo_setup(
        tmp_path, ["llvm", "llvm-libs"], ["spirv-llvm-translator"])
    monkeypatch.setattr(sudo_session, "authenticate", lambda: False)
    options.state_dir = tmp_path / "state"
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "config.TOOLCHAIN_PATH", toml_path), \
         patch(T + "pkgbuilds.sync_pkgbuild_dirs"), \
         patch(T + "pgo.build_llvm_pgo_inner",
               side_effect=lambda p, n, l32, *a, **k: ({**p, **n, **l32}, "/usr/bin/clang",
                                                     "/usr/bin/clang++", "lld", "pgo_llvm")), \
         patch(T + "gates.gate2_audit", return_value=[]), \
         patch(T + "profdata.pgo_install", side_effect=AssertionError("installed")), \
         patch("sys.stdin.isatty", return_value=False), \
         pytest.raises(RuntimeError, match="nothing was installed.*--install-only"):
        ToolchainStage().run(config, state, options)
    assert not _sentinel_exists(tmp_path / "state")


# ---------------------------------------------------------------------------
# 3.5.0-F8: --install-only routes around the build
# ---------------------------------------------------------------------------

def test_install_only_calls_install_only_and_never_the_build(tmp_path):
    toml_path, _d, _s, _p, state, config, options = _pgo_setup(
        tmp_path, ["llvm", "llvm-libs"], ["spirv-llvm-translator"])
    options.install_only = True
    seen = {}

    def fake_install_only(pgo_map, non_pgo_map, lib32_map, opts):
        seen["maps"] = (set(pgo_map), set(non_pgo_map))
        raise RuntimeError("stop after check")

    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "config.TOOLCHAIN_PATH", toml_path), \
         patch(T + "pkgbuilds.sync_pkgbuild_dirs"), \
         patch(T + "pgo.build_llvm_pgo_inner", side_effect=AssertionError("built")), \
         patch(T + "pgo.install_only", side_effect=fake_install_only), \
         pytest.raises(RuntimeError, match="stop after check"):
        ToolchainStage().run(config, state, options)
    assert seen["maps"] == ({"llvm", "llvm-libs"}, {"spirv-llvm-translator"})


def test_install_only_refuses_a_non_pgo_toolchain(tmp_path):
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    options.install_only = True
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pgo.build_llvm_single",
               side_effect=AssertionError("built")), \
         pytest.raises(RuntimeError, match="--install-only needs"):
        ToolchainStage().run(config, state, options)


def test_install_only_confirmation_asks_to_install_not_build(monkeypatch):
    from sysforge.pipeline.stages.toolchain import pkgbuilds
    asked = []
    monkeypatch.setattr(pkgbuilds.prompt, "prompt_choice",
                        lambda text, **kw: asked.append(text) or "y")
    pkgbuilds.confirm_or_abort(None, mode="--install-only")
    assert asked == ["Proceed with installing the last built toolchain? [y/N]: "]


def test_install_only_plan_names_the_mode_and_skips_the_space_gate(
        tmp_path, monkeypatch, capsys):
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import toolchain_safety as _ts
    from tests.toolchain_helpers import toolchain_cfg
    opts = make_options(dry_run=False)
    opts.install_only = True
    gates.log_toolchain_resolution_summary(
        compiler="llvm", pgo=True, variant="pgo_llvm", pgo_pkgs=["llvm"],
        non_pgo_pkgs=[], lib32_pkgs=[], staging1=tmp_path / "s1", staging=tmp_path / "s2",
        staging3=tmp_path / "s3", pgo_store=tmp_path / "store", tcfg=toolchain_cfg(),
        options=opts, snapshot={})
    out, err = capsys.readouterr()
    assert "--install-only" in out + err and "staging1" not in out + err
    monkeypatch.setattr(_ts, "check_build_space",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("space checked")))
    monkeypatch.setattr(gates, "install_bootstrap_compilers", lambda *a, **k: [])
    monkeypatch.setattr(_ts, "check_pkgver_lockstep", lambda v: None)
    gates.gate1_preflight([], tmp_path / "s1", tmp_path / "s2", tmp_path / "store",
                          {}, opts, toolchain_cfg(), snapshot={})


def test_failed_pgo_install_points_at_install_only(tmp_path):
    """3.5.0-F8: a pacman -U failure after a PGO build names the recovery that
    skips the rebuild; the sentinel stays (pacman may have half-applied)."""
    toml_path, _d, _s, _p, state, config, options = _pgo_setup(
        tmp_path, ["llvm", "llvm-libs"], ["spirv-llvm-translator"])
    options.state_dir = tmp_path / "state"
    T = "sysforge.pipeline.stages.toolchain."
    with patch(T + "config.TOOLCHAIN_PATH", toml_path), \
         patch(T + "pkgbuilds.sync_pkgbuild_dirs"), \
         patch(T + "pgo.build_llvm_pgo_inner",
               side_effect=lambda p, n, l32, *a, **k: ({**p, **n, **l32}, "/usr/bin/clang",
                                                     "/usr/bin/clang++", "lld", "pgo_llvm")), \
         patch(T + "gates.gate2_audit", return_value=[]), \
         patch(T + "profdata.pgo_install",
               side_effect=RuntimeError("[TOOLCHAIN] pacman -U failed (exit 1)")), \
         patch("sys.stdin.isatty", return_value=False), \
         pytest.raises(RuntimeError, match="pacman -U failed.*--install-only"):
        ToolchainStage().run(config, state, options)
    assert _sentinel_exists(tmp_path / "state")


def test_declining_a_standalone_run_names_no_pipeline_resume(monkeypatch, capsys):
    """3.5.0-B33: `run toolchain` alone is not inside the bootstrap, so a no
    at its prompt must not send the user to `run pipeline --resume`; inside
    `run pipeline` that hint is still the way back."""
    from sysforge.pipeline.stages.toolchain import pkgbuilds
    monkeypatch.setattr(pkgbuilds.prompt, "prompt_choice", lambda text, **kw: "n")
    with pytest.raises(RuntimeError, match="nothing was built or installed"):
        pkgbuilds.confirm_or_abort(None, standalone=True)
    assert "run pipeline" not in "".join(capsys.readouterr())
    with pytest.raises(RuntimeError, match="resume command"):
        pkgbuilds.confirm_or_abort(None)
    assert "sysforge run pipeline --resume" in "".join(capsys.readouterr())


# ---------------------------------------------------------------------------
# 3.5.0-B74 / B75: no sentinel for a dry run or an unauthorized repo install
# ---------------------------------------------------------------------------

def test_dry_run_writes_no_install_sentinel(tmp_path, monkeypatch):
    toml_path, state, config, _options = _single_pass_setup(tmp_path)
    options = make_options(dry_run=True, state_dir=tmp_path / "state")
    import sysforge.pipeline.stages.toolchain.stage as stage_mod
    monkeypatch.setattr(stage_mod, "sentinel_scope",
                        lambda *a, **k: pytest.fail("dry run entered the sentinel"))
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"):
        ToolchainStage().run(config, state, options)
    assert not _sentinel_exists(tmp_path / "state")


def test_repo_install_unauthorized_sudo_leaves_no_sentinel(tmp_path, monkeypatch):
    from sysforge.primitives import sudo_session
    toml_path = tmp_path / "toolchain.toml"
    toml_path.write_text('enabled = true\ncompiler = "llvm"\npgo = false\n')
    state = PipelineState(tmp_path / "state")
    config = {"paths": {"pkgbuild_src_dir": str(tmp_path / "empty")},
              "packages_file": _write_packages_repo_mode(tmp_path, "pacman")}
    options = make_options(dry_run=False, state_dir=str(tmp_path / "sdir"))
    monkeypatch.setattr(sudo_session, "authenticate", lambda: False)
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs",
               side_effect=AssertionError("installed")), \
         pytest.raises(RuntimeError, match="sudo was not authorized"):
        ToolchainStage().run(config, state, options)
    assert not _sentinel_exists(tmp_path / "sdir")


def test_gate1_bootstrap_install_unauthorized_sudo_leaves_no_sentinel(tmp_path, monkeypatch):
    from sysforge.primitives import sudo_session
    from sysforge.primitives import toolchain_safety as _ts
    toml_path, state, config, options = _single_pass_setup(tmp_path)
    options.state_dir = tmp_path / "state"
    monkeypatch.setattr(_ts, "smoke_test_compilers",
                        lambda: _bootstrap_missing("smoke:clang_missing"))
    monkeypatch.setattr(sudo_session, "authenticate", lambda: False)
    with patch("sysforge.pipeline.stages.toolchain.config.TOOLCHAIN_PATH", toml_path), \
         patch("sysforge.primitives.pacman.install_repo_pkgs",
               side_effect=AssertionError("installed")), \
         patch("sysforge.pipeline.stages.toolchain.pkgbuilds.sync_pkgbuild_dirs"), \
         patch("sys.stdin.isatty", return_value=False), \
         pytest.raises(RuntimeError, match="sudo was not authorized"):
        ToolchainStage().run(config, state, options)
    assert not _sentinel_exists(tmp_path / "state")


def test_healable_drift_line_names_the_manual_rebuild_when_nothing_is_queued():
    """3.5.0-B98: rebuild_soname_consumers = off queues nothing."""
    from sysforge.pipeline.stages.toolchain.stage import healable_drift_line
    assert "queued consumer rebuild" in healable_drift_line("m", queued=True)
    off = healable_drift_line("m", queued=False)
    assert "no consumer rebuild is queued" in off and "sysforge build" in off
