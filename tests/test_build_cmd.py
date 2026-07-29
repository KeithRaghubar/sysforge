#!/usr/bin/env python3
"""
Tests for sysforge.build_cmd — the ``build`` verb's presentation layer.

The engine itself is covered by test_build_core.py; here we cover the
verb-side summary: multi-package runs end with an update-style totals block,
single-package runs stay quiet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import types

import pytest

import sysforge.build_cmd as build_cmd
import sysforge.build_core as build_core
import sysforge.pipeline.state as pipeline_state
from sysforge.build_cmd import BuildVerb, _print_build_summary
from sysforge.build_core import BuildOutcome, BuildTarget
from sysforge.verbs import PreCheckResult


def _run_build(monkeypatch, tmp_path, pkgname, *, is_repo, force=True,
               interactive=False, config=None, opt_in_yes=False):
    """Run BuildVerb.execute for one package with the heavy collaborators
    stubbed. Returns (targets_passed_to_build_and_install, exec_result)."""
    captured: dict = {"targets": []}

    pkgbuild = tmp_path / pkgname / "PKGBUILD"
    monkeypatch.setattr(build_cmd, "find_pkgbuild", lambda pkg, cfg: pkgbuild)
    monkeypatch.setattr(build_cmd, "is_repo_package", lambda name: is_repo)
    monkeypatch.setattr(build_core, "target_from_pkgbuild",
                        lambda p: BuildTarget(pkgbase=pkgname, pkgnames=[pkgname],
                                              pkgbuild_path=Path(p)))
    monkeypatch.setattr(build_cmd, "is_interactive", lambda: interactive)
    monkeypatch.setattr(build_cmd, "prompt_choice",
                        lambda *a, **k: "y" if opt_in_yes else "n")

    def _fake_build_and_install(targets, **kwargs):
        captured["targets"] = targets
        return BuildOutcome()
    monkeypatch.setattr(build_core, "build_and_install", _fake_build_and_install)
    monkeypatch.setattr(pipeline_state, "resolve_state_dir",
                        lambda d: (tmp_path, None))
    monkeypatch.setattr(pipeline_state, "get_toolchain_variant", lambda st: "system")
    monkeypatch.setattr(pipeline_state, "get_toolchain_fingerprint", lambda st: None)
    monkeypatch.setattr(pipeline_state, "PipelineState", lambda d: None)

    args = types.SimpleNamespace(
        makepkg=None, pkgbuilds=[pkgname], cleansrc=False, cleansrc_force=False,
        no_update=True, interactive=False, profile_conf=None, cc=None, cxx=None,
        ld=None, state_dir=None, no_pkg_log=True, persist_log=False, log_dir=None,
        cache_report=False, abi_check=False, no_review=True, timings=False,
        force=force,
    )
    result = BuildVerb().execute(args, PreCheckResult(ctx={"config": config or {}}))
    return captured["targets"], result


def _drive_build(monkeypatch, tmp_path, pkgname, *, is_repo):
    """Back-compat shim for the source-stamping tests: bypass the repo gate via
    --force and return the single BuildTarget handed to build_and_install."""
    targets, _ = _run_build(monkeypatch, tmp_path, pkgname, is_repo=is_repo, force=True)
    return targets[0]


def test_build_records_repo_source_for_repo_package(monkeypatch, tmp_path):
    """`sysforge build <repo-pkg>` stamps source="repo" so build_state is
    self-describing and `update` classifies it repo_class="source"."""
    target = _drive_build(monkeypatch, tmp_path, "mesa", is_repo=True)
    assert target.source == "repo"


def test_build_leaves_source_none_for_non_repo_package(monkeypatch, tmp_path):
    """A non-repo (AUR/local) build keeps source=None — origin is recovered
    from pacman -Qm foreign-ness at update time; guessing risks mis-routing."""
    target = _drive_build(monkeypatch, tmp_path, "neovim-git", is_repo=False)
    assert target.source is None


# ---------------------------------------------------------------------------
# Repo-package opt-in gate
# ---------------------------------------------------------------------------

def test_gate_aborts_repo_pkg_not_opted_in_non_interactive(monkeypatch, tmp_path):
    """A repo package with no opt-in, non-interactive, no --force is skipped
    (no targets reach build_and_install)."""
    cfg = {"packages_file": str(tmp_path / "packages.toml")}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=False, config=cfg)
    assert targets == []


def test_gate_force_builds_without_touching_packages_toml(monkeypatch, tmp_path):
    """--force builds the repo package and never writes the opt-in key."""
    pkg_path = tmp_path / "packages.toml"
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=True, interactive=False, config=cfg)
    assert [t.pkgbase for t in targets] == ["mesa"]
    assert not pkg_path.exists()


def test_gate_interactive_yes_builds_and_writes_opt_in(monkeypatch, tmp_path):
    """Interactive confirm builds AND records enable_build_from_source=true."""
    pkg_path = tmp_path / "packages.toml"
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=True, opt_in_yes=True, config=cfg)
    assert [t.pkgbase for t in targets] == ["mesa"]
    text = pkg_path.read_text()
    assert 'name = "mesa"' in text
    assert "enable_build_from_source = true" in text


def test_gate_interactive_no_skips(monkeypatch, tmp_path):
    """Interactive decline skips the target and never writes packages.toml."""
    pkg_path = tmp_path / "packages.toml"
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=True, opt_in_yes=False, config=cfg)
    assert targets == []
    assert not pkg_path.exists()


def test_gate_already_opted_in_builds_silently(monkeypatch, tmp_path):
    """A repo package already opted in (per-package key) builds with no prompt."""
    pkg_path = tmp_path / "packages.toml"
    pkg_path.write_text(
        '[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "mesa"\nenable_build_from_source = true\n'
    )
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=False, config=cfg)
    assert [t.pkgbase for t in targets] == ["mesa"]


def test_gate_global_repo_mode_opts_in(monkeypatch, tmp_path):
    """repo_mode = build_from_source opts in every repo package globally."""
    pkg_path = tmp_path / "packages.toml"
    pkg_path.write_text('[build]\nrepo_mode = "build_from_source"\n')
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=False, config=cfg)
    assert [t.pkgbase for t in targets] == ["mesa"]


def test_gate_global_repo_mode_profiled_raises(monkeypatch, tmp_path):
    """End-to-end regression for the 2.6.1-F5 whitelist hazard: build_cmd reads
    packages.toml directly and calls resolve_repo_mode without ever passing
    through _load_packages's REPO_MODE_ACCEPTED_INPUTS gate. Before the fix,
    the removed "profiled" token would silently resolve to "pacman" here and
    the repo package would build unopted-in. It must now raise instead."""
    pkg_path = tmp_path / "packages.toml"
    pkg_path.write_text('[build]\nrepo_mode = "profiled"\n')
    cfg = {"packages_file": str(pkg_path)}
    with pytest.raises(ValueError, match="build_from_source"):
        _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                   force=False, interactive=False, config=cfg)


def test_gate_does_not_apply_to_non_repo_package(monkeypatch, tmp_path):
    """An AUR/git target is never gated — built unconditionally, no prompt."""
    cfg = {"packages_file": str(tmp_path / "packages.toml")}
    targets, _ = _run_build(monkeypatch, tmp_path, "neovim-git", is_repo=False,
                            force=False, interactive=False, config=cfg)
    assert [t.pkgbase for t in targets] == ["neovim-git"]


def test_gate_legacy_pkgbuild_patch_no_longer_counts_as_opted_in(monkeypatch, tmp_path):
    """3.0.0 removed the read-side rename: a pre-rename ``pkgbuild_patch=true``
    entry no longer counts as opted in — it's gated same as an absent entry.
    (A rewrite via `packages add`/`remove` still migrates the key.)"""
    pkg_path = tmp_path / "packages.toml"
    pkg_path.write_text(
        '[build]\npkgbuild_src_dir = "~/src"\n\n'
        '[[package]]\nname = "mesa"\npkgbuild_patch = true\n'
    )
    cfg = {"packages_file": str(pkg_path)}
    targets, _ = _run_build(monkeypatch, tmp_path, "mesa", is_repo=True,
                            force=False, interactive=False, config=cfg)
    assert targets == []


# ---------------------------------------------------------------------------
# [build] config defaults for abi_check / cache_report / persist_log (F1)
# ---------------------------------------------------------------------------

def _run_build_capture_kwargs(monkeypatch, tmp_path, pkgname, *, force,
                               packages_toml_text):
    """Like _run_build, but captures the kwargs passed to build_and_install
    instead of the targets, so precedence on the three flag kwargs can be
    asserted. Uses a non-repo package so the opt-in gate never blocks."""
    captured: dict = {}

    pkgbuild = tmp_path / pkgname / "PKGBUILD"
    monkeypatch.setattr(build_cmd, "find_pkgbuild", lambda pkg, cfg: pkgbuild)
    monkeypatch.setattr(build_cmd, "is_repo_package", lambda name: False)
    monkeypatch.setattr(build_core, "target_from_pkgbuild",
                        lambda p: BuildTarget(pkgbase=pkgname, pkgnames=[pkgname],
                                              pkgbuild_path=Path(p)))
    monkeypatch.setattr(build_cmd, "is_interactive", lambda: False)
    monkeypatch.setattr(build_cmd, "prompt_choice", lambda *a, **k: "n")

    def _fake_build_and_install(targets, **kwargs):
        captured.update(kwargs)
        return BuildOutcome()
    monkeypatch.setattr(build_core, "build_and_install", _fake_build_and_install)
    monkeypatch.setattr(pipeline_state, "resolve_state_dir",
                        lambda d: (tmp_path, None))
    monkeypatch.setattr(pipeline_state, "get_toolchain_variant", lambda st: "system")
    monkeypatch.setattr(pipeline_state, "get_toolchain_fingerprint", lambda st: None)
    monkeypatch.setattr(pipeline_state, "PipelineState", lambda d: None)

    pkg_path = tmp_path / "packages.toml"
    pkg_path.write_text(packages_toml_text)
    cfg = {"packages_file": str(pkg_path)}

    args = types.SimpleNamespace(
        makepkg=None, pkgbuilds=[pkgname], cleansrc=False, cleansrc_force=False,
        no_update=True, interactive=False, profile_conf=None, cc=None, cxx=None,
        ld=None, state_dir=None, no_pkg_log=True, persist_log=False, log_dir=None,
        cache_report=False, abi_check=False, no_review=True, timings=False,
        force=force,
    )
    BuildVerb().execute(args, PreCheckResult(ctx={"config": cfg}))
    return captured


def test_build_abi_check_config_default_applies(monkeypatch, tmp_path):
    """abi_check not passed on the CLI, but [build] abi_check = true —
    the config default reaches build_and_install."""
    captured = _run_build_capture_kwargs(
        monkeypatch, tmp_path, "neovim-git", force=False,
        packages_toml_text='[build]\nabi_check = true\n',
    )
    assert captured["abi_check"] is True


def test_build_cache_report_and_persist_log_config_defaults_apply(monkeypatch, tmp_path):
    """Same precedence for cache_report / persist_log."""
    captured = _run_build_capture_kwargs(
        monkeypatch, tmp_path, "neovim-git", force=False,
        packages_toml_text='[build]\ncache_report = true\npersist_log = true\n',
    )
    assert captured["cache_report"] is True
    assert captured["persist_log"] is True


def test_build_force_ignores_config_default(monkeypatch, tmp_path):
    """--force sets build_cfg={} — the [build] abi_check default must NOT
    apply on a forced run (explicit this-run-only override)."""
    captured = _run_build_capture_kwargs(
        monkeypatch, tmp_path, "neovim-git", force=True,
        packages_toml_text='[build]\nabi_check = true\n',
    )
    assert captured["abi_check"] is False


def test_summary_lists_built_and_failed(capsys):
    outcome = BuildOutcome(
        built_pkgs=["vulkan-icd-loader-git", "vulkan-utility-libraries-git"],
        failed_pkgs=["vulkan-validation-layers-git"],
    )
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert "Build complete: 2 built, 1 failed." in out
    assert "Built:       vulkan-icd-loader-git vulkan-utility-libraries-git" in out
    assert "Failed:      vulkan-validation-layers-git" in out
    assert "Skipped:" not in out
    assert "PGO-skipped:" not in out


def test_summary_marks_install_failure_and_optional_sections(capsys):
    outcome = BuildOutcome(
        built_pkgs=["foo"],
        failed_pkgs=[],
        review_skipped=["bar"],
        pgo_skipped_pkgs=["baz"],
        install_failed=True,
    )
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert (
        "Build complete: 1 built, 0 failed, 1 skipped at review, "
        "1 pgo-skipped (install FAILED)." in out
    )
    assert "Skipped:     bar (PKGBUILD review)" in out
    assert "PGO-skipped: baz" in out


def test_summary_all_failed(capsys):
    outcome = BuildOutcome(failed_pkgs=["foo", "bar"])
    _print_build_summary(outcome)
    out = capsys.readouterr().err
    assert "Build complete: 0 built, 2 failed." in out
    assert "Built:" not in out


# ---------------------------------------------------------------------------
# --pgo LLVM-toolchain gate (mesa instrumentation PGO has no gcc path)
# ---------------------------------------------------------------------------

def _pgo_args(pgo_mode):
    return types.SimpleNamespace(
        pgo_mode=pgo_mode,
        no_pkg_log=False,
        log_dir=None,
        dry_run=False,
        no_llvm_preflight=True,
        pkgbuilds=["mesa"],
    )


def test_pre_check_pgo_blocks_under_gcc(monkeypatch):
    monkeypatch.setattr(
        build_cmd, "load_config_with_overrides",
        lambda args: {"defaults": {"toolchain": "gcc"}},
    )
    pre = BuildVerb().pre_check(_pgo_args("record"))
    assert pre.blocker is not None
    assert "LLVM toolchain" in pre.blocker


def test_pre_check_pgo_allowed_under_llvm(monkeypatch):
    monkeypatch.setattr(
        build_cmd, "load_config_with_overrides",
        lambda args: {"defaults": {"toolchain": "llvm"}},
    )
    pre = BuildVerb().pre_check(_pgo_args("use"))
    assert pre.blocker is None


def test_pre_check_no_pgo_never_gates(monkeypatch):
    # Without --pgo the toolchain gate must not fire even under gcc.
    monkeypatch.setattr(
        build_cmd, "load_config_with_overrides",
        lambda args: {"defaults": {"toolchain": "gcc"}},
    )
    pre = BuildVerb().pre_check(_pgo_args(None))
    assert pre.blocker is None
