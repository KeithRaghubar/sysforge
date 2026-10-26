# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/gates.py — Gate 1/2, soname consumers, snapshot rollback.
"""
from sysforge.pipeline.stages.toolchain.gates import gate_soname_consumers
from sysforge.pipeline.stages.toolchain.gates import rebuild_soname_consumers
from sysforge.pipeline.state import PipelineState
from pathlib import Path
import pytest

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    toolchain_cfg,
    _gate_map,
    _impact,
    _patch_rebuild,
    _toolchain_gates_clean,
    make_options,
)


def test_gate_soname_no_impact_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: None,
    )
    opts = make_options(dry_run=False)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == []

def test_gate_soname_dry_run_previews_no_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=True)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == []

def test_gate_soname_off_mode_warns_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=False)
    tcfg = toolchain_cfg(rebuild_soname_consumers="off")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == []

def test_gate_soname_auto_mode_returns_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(("mesa", "julia")),
    )
    opts = make_options(dry_run=False)
    tcfg = toolchain_cfg(rebuild_soname_consumers="auto")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == ["mesa", "julia"]

def test_gate_soname_prompt_non_tty_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: False)
    opts = make_options(dry_run=False)
    with pytest.raises(RuntimeError, match="non-interactive"):
        gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg())

def test_gate_soname_prompt_approve_returns_consumers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: True)
    monkeypatch.setattr(
        "sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "y"
    )
    opts = make_options(dry_run=False)
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg()) == ["mesa"]

def test_gate_soname_prompt_decline_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    monkeypatch.setattr("sysforge.primitives.prompt.is_interactive", lambda: True)
    monkeypatch.setattr(
        "sysforge.primitives.prompt.prompt_choice", lambda *a, **k: "n"
    )
    opts = make_options(dry_run=False)
    with pytest.raises(RuntimeError, match="not approved"):
        gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, toolchain_cfg())

def test_gate_soname_cli_flag_overrides_config(tmp_path, monkeypatch):
    # CLI --rebuild-soname-consumers=off beats toolchain.toml auto.
    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact",
        lambda ver, *, exclude: _impact(),
    )
    opts = make_options(dry_run=False, rebuild_soname_consumers="off")
    tcfg = toolchain_cfg(rebuild_soname_consumers="auto")
    assert gate_soname_consumers(_gate_map(tmp_path), ["llvm"], opts, tcfg) == []

def test_gate_soname_exclude_passed_to_assessor(tmp_path, monkeypatch):
    captured = {}

    def fake_assess(ver, *, exclude):
        captured["exclude"] = exclude
        return None

    monkeypatch.setattr(
        "sysforge.primitives.toolchain_safety.assess_libllvm_soname_impact", fake_assess
    )
    opts = make_options(dry_run=False)
    gate_soname_consumers(_gate_map(tmp_path), ["llvm", "lib32-llvm"], opts, toolchain_cfg())
    # LLVM lockstep suite + in-scope build names are all excluded.
    from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE
    assert set(LLVM_LOCKSTEP_SUITE) <= captured["exclude"]
    assert {"llvm", "lib32-llvm"} <= captured["exclude"]

def test_rebuild_consumers_success(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=["mesa"])
    _patch_rebuild(monkeypatch, out)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    # No raise == success.
    rebuild_soname_consumers(["mesa"], {}, opts, state)

def test_rebuild_consumers_build_failure_raises_with_manual_cmd(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=[], failed_pkgs=["mesa"])
    _patch_rebuild(monkeypatch, out)
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="sysforge build mesa"):
        rebuild_soname_consumers(["mesa"], {}, opts, state)

def test_rebuild_consumers_all_unresolved_raises(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=[])
    _patch_rebuild(monkeypatch, out, resolve_fail=("mesa",))
    state = PipelineState(tmp_path / "state")
    opts = make_options(state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="could be resolved for rebuild"):
        rebuild_soname_consumers(["mesa"], {}, opts, state)


def test_rebuild_consumers_resolves_the_review_policy(tmp_path, monkeypatch):
    """3.0.0-F3: the soname-consumer rebuild goes through the same review
    policy as update and build, not a hardcoded mode."""
    from sysforge import build_core
    from sysforge.build_core import BuildOutcome
    seen = {}
    _patch_rebuild(monkeypatch, BuildOutcome(built_pkgs=["mesa"]))
    monkeypatch.setattr(build_core, "build_and_install",
                        lambda *a, **k: (seen.update(k), BuildOutcome(built_pkgs=["mesa"]))[1])
    state = PipelineState(tmp_path / "state")
    rebuild_soname_consumers(["mesa"], {}, make_options(state_dir=tmp_path), state)
    assert seen["review"] == "prompt"
    assert seen["review_trusted"] == frozenset({"repo"})


def test_rebuild_consumers_refusal_is_not_reported_as_rebuilt(tmp_path, monkeypatch):
    from sysforge.build_core import BuildOutcome
    out = BuildOutcome(built_pkgs=[], review_refused={"mesa": "unreviewed source change"})
    _patch_rebuild(monkeypatch, out)
    state = PipelineState(tmp_path / "state")
    with pytest.raises(RuntimeError, match="mesa"):
        rebuild_soname_consumers(["mesa"], {}, make_options(state_dir=tmp_path), state)


def test_rebuild_consumers_marks_repo_consumers_trusted_for_review(tmp_path, monkeypatch):
    """3.5.0-B16: a consumer cloned from its official packaging repo (doxygen,
    or the split linux-tools) is source="repo", which the default
    review_trusted_sources accepts without a prompt. Provenance is the clone's
    origin, never the names a PKGBUILD declares: an AUR PKGBUILD listing a
    repo pkgname (glibc) stays source=None and is still reviewed."""
    from sysforge import build_core
    from sysforge.build_core import BuildOutcome
    from sysforge.primitives import aur

    def pkgbuild(name, pkgnames):
        d = tmp_path / name
        d.mkdir()
        (d / "PKGBUILD").write_text(
            f"pkgbase={name}\npkgname=({' '.join(pkgnames)})\npkgver=1\npkgrel=1\n")
        return d / "PKGBUILD"

    paths = {"doxygen": pkgbuild("doxygen", ["doxygen", "doxygen-docs"]),
             "linux-tools": pkgbuild("linux-tools", ["bpftool", "perf"]),
             "spoof": pkgbuild("spoof", ["spoof", "glibc"]),
             "renamed": pkgbuild("renamed", ["renamed"])}
    origin = {"doxygen": "doxygen", "linux-tools": "linux-tools",
              "spoof": None, "renamed": "something-else"}
    monkeypatch.setattr("sysforge.primitives.config.find_pkgbuild",
                        lambda pkg, config: paths[pkg])
    monkeypatch.setattr(aur, "repo_checkout_pkgbase", lambda d: origin[Path(d).name])
    monkeypatch.setattr(aur, "is_repo_package", lambda n: True)  # never consulted
    seen = {}
    monkeypatch.setattr(build_core, "build_and_install",
                        lambda targets, **k: (seen.update(t={t.pkgbase: t.source for t in targets}),
                                              BuildOutcome(built_pkgs=list(paths)))[1])
    monkeypatch.setattr("sysforge.pipeline.state.get_toolchain_variant", lambda s: "pgo_llvm")
    rebuild_soname_consumers(list(paths), {}, make_options(state_dir=tmp_path),
                             PipelineState(tmp_path / "state"))
    assert seen["t"] == {"doxygen": "repo", "linux-tools": "repo",
                         "spoof": None, "renamed": None}


def test_snapshot_skips_names_that_were_never_installed(tmp_path, monkeypatch):
    """3.5.0-B76: a new subpackage's None read as a missing archive and
    refused the whole rollback."""
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import pacman
    installed = {"llvm": "21.1.0-1", "clang": "21.1.0-1"}
    monkeypatch.setattr(gates, "_SNAPSHOT_SUITE", ("llvm", "clang", "lib32-new"))
    monkeypatch.setattr(pacman, "get_installed_version", installed.get)
    monkeypatch.setattr(pacman, "cached_pkg_files_for",
                        lambda names: {n: tmp_path / f"{n}.pkg.tar.zst" for n in names})
    snap = gates.snapshot_suite({"llvm-new-sub": tmp_path / "llvm" / "PKGBUILD"})
    assert set(snap) == {"llvm", "clang"}
    monkeypatch.setattr(pacman, "batch_install_pkgs", lambda files: True)
    assert gates.rollback_to_snapshot(snap) is True


def test_empty_snapshot_rollback_says_why(monkeypatch, capsys):
    """3.5.0-B114: an empty snapshot (a first toolchain install) refused the
    rollback with no reason, so the stage reported only that it 'could not
    complete'. It now names the cause and never runs pacman."""
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import pacman
    calls = []
    monkeypatch.setattr(pacman, "batch_install_pkgs", lambda files: calls.append(files))
    assert gates.rollback_to_snapshot({}) is False
    assert calls == []
    err = capsys.readouterr().err
    assert "snapshot is empty" in err
    assert "none of the toolchain packages was installed before this run" in err


# --- a member the install adds (clang-libs, 3.5.0-B146) ---------------------


def _added_setup(tmp_path, monkeypatch, installed):
    """llvm's build produced clang, clang-libs and llvm-libs; clang depends on
    clang-libs. ``installed`` is what pacman reports now."""
    from sysforge.pipeline.stages.toolchain import gates, profdata
    from sysforge.primitives import pacman
    files = {n: tmp_path / f"{n}-23.1.3-4-x86_64.pkg.tar.zst"
             for n in ("clang", "clang-libs", "llvm-libs", "wasi-libc++")}
    monkeypatch.setattr(profdata, "collect_pgo_packages", lambda m: list(files.values()))
    monkeypatch.setattr(pacman, "read_pkgname_from_file",
                        lambda p: next(n for n, f in files.items() if f == p))
    deps = {"clang": {"clang-libs", "llvm-libs"}}
    monkeypatch.setattr(pacman, "read_pkginfo_relations", lambda p: {
        "depend": deps.get(pacman.read_pkgname_from_file(p), set()), "provides": set()})
    monkeypatch.setattr(pacman, "get_installed_version", installed.get)
    return gates


def test_install_adds_names_the_members_not_installed_yet(tmp_path, monkeypatch):
    gates = _added_setup(tmp_path, monkeypatch,
                         {"clang": "23.1.3-3", "llvm-libs": "23.1.3-3"})
    built = {"clang": tmp_path / "llvm" / "PKGBUILD"}
    assert gates.install_adds(built, {"clang", "llvm-libs"}) == ("clang-libs",)


def test_install_adds_nothing_once_the_member_is_installed(tmp_path, monkeypatch):
    gates = _added_setup(tmp_path, monkeypatch, {
        "clang": "23.1.3-4", "clang-libs": "23.1.3-4", "llvm-libs": "23.1.3-4"})
    built = {"clang": tmp_path / "llvm" / "PKGBUILD"}
    assert gates.install_adds(built, {"clang", "clang-libs", "llvm-libs"}) == ()


def test_rollback_removes_an_added_member_before_reinstalling(tmp_path, monkeypatch):
    """The cached clang 23.1.3-3 owns libclang-cpp and the builtin headers,
    which the installed clang-libs now owns: pacman -U alone hits file
    conflicts, so the member the install added goes first."""
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import pacman
    calls = []
    monkeypatch.setattr(pacman, "get_installed_version", {"clang-libs": "23.1.3-4"}.get)
    monkeypatch.setattr(pacman, "remove_pkgs",
                        lambda names, nodeps=False: calls.append(("remove", names, nodeps)))
    monkeypatch.setattr(pacman, "batch_install_pkgs",
                        lambda files: calls.append(("install", files)) or True)
    snap = {"clang": tmp_path / "clang-23.1.3-3.pkg.tar.zst"}
    assert gates.rollback_to_snapshot(snap, added=("clang-libs",)) is True
    assert calls == [("remove", ["clang-libs"], True),
                     ("install", [tmp_path / "clang-23.1.3-3.pkg.tar.zst"])]


def test_rollback_skips_an_added_member_that_is_not_installed(tmp_path, monkeypatch):
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import pacman
    removed = []
    monkeypatch.setattr(pacman, "get_installed_version", {}.get)
    monkeypatch.setattr(pacman, "remove_pkgs",
                        lambda names, nodeps=False: removed.append(names))
    monkeypatch.setattr(pacman, "batch_install_pkgs", lambda files: True)
    assert gates.rollback_to_snapshot({"clang": tmp_path / "c.pkg.tar.zst"},
                                      added=("clang-libs",)) is True
    assert removed == []


def test_rollback_stops_when_the_added_member_cannot_be_removed(tmp_path, monkeypatch):
    import subprocess
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import pacman
    installs = []

    def remove(names, nodeps=False):
        raise subprocess.CalledProcessError(1, ["pacman", "-Rdd"])

    monkeypatch.setattr(pacman, "get_installed_version", {"clang-libs": "23.1.3-4"}.get)
    monkeypatch.setattr(pacman, "remove_pkgs", remove)
    monkeypatch.setattr(pacman, "batch_install_pkgs", lambda files: installs.append(files))
    assert gates.rollback_to_snapshot({"clang": tmp_path / "c.pkg.tar.zst"},
                                      added=("clang-libs",)) is False
    assert installs == []


def test_recovery_goes_online_when_the_install_adds_a_member(tmp_path):
    """One stored command cannot remove clang-libs and downgrade clang, so the
    recovery reinstalls the repo suite, clang-libs named explicitly: at the
    same version string a dependency resolve would keep the sysforge copy."""
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import stage_sentinel
    snap = {"clang": tmp_path / "clang-23.1.3-3.pkg.tar.zst"}
    rec = gates.snapshot_recovery(snap, added=("clang-libs",))
    assert rec.kind == stage_sentinel.RECOVERY_PACMAN_SYNC
    assert {"clang", "clang-libs", "llvm-libs"} <= set(rec.args)


def test_recovery_stays_offline_when_nothing_is_added(tmp_path):
    from sysforge.pipeline.stages.toolchain import gates
    from sysforge.primitives import stage_sentinel
    snap = {"clang": tmp_path / "clang-23.1.3-3.pkg.tar.zst"}
    assert gates.snapshot_recovery(snap).kind == stage_sentinel.RECOVERY_PACMAN_UPGRADE
