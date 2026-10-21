# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`build --toolchain` / `--save-toolchain` / `--reset-toolchain` (3.5.0-F14).

A CLI compiler choice swaps the whole bundle (compilers, binutils, linker)
through the same ``profile.swap_toolchain`` the recovery menu uses, and can be
kept for a package without a failed build first. Dual-toolchain: each
direction is pinned."""
import tomllib
from pathlib import Path

import pytest

from sysforge import build_cmd
from sysforge.build import makepkg_wrapper as mw
from sysforge.build.makepkg_wrapper import BuildOptions
from sysforge.primitives.makepkg_conf import emit_makepkg_conf
from sysforge.primitives.profile import resolve_profile
from sysforge.profile_writer import (
    delete_package_compiler_override,
    write_package_compiler_override,
)
from sysforge.verbs.registry import build_parser

_LLVM = {
    "toolchain": "llvm",
    "CFLAGS": "-march=native -O3 -pipe -flto=thin",
    "LDFLAGS": "-fuse-ld=lld -Wl,-O1,--icf=all -flto=thin",
}
_GCC = {"toolchain": "gcc", "CFLAGS": "-O2", "LDFLAGS": "-Wl,-O1"}


def _pkgmeta(name):
    return {"globals": {"pkgname": name, "pkgbase": name}, "functions": {}}


def _resolved(profile, rows=None):
    cfg = {"defaults": {"profile": "p", "toolchain": profile["toolchain"]},
           "profiles": {"p": dict(profile)},
           "package_compiler_overrides": rows or {}}
    return resolve_profile(_pkgmeta("htop"), [], cfg)


def _emitted(prof) -> str:
    with emit_makepkg_conf(prof, system_conf_path="/nonexistent") as conf:
        return Path(conf).read_text()


# --- the swap, both directions ----------------------------------------------

def test_toolchain_gcc_on_an_llvm_profile_emits_the_whole_gcc_bundle():
    prof = mw._apply_cli_toolchain(_resolved(_LLVM), BuildOptions(toolchain_override="gcc"))
    assert (prof["CC"], prof["CXX"]) == ("gcc", "g++")
    assert not any(k in prof for k in ("AR", "NM", "RANLIB", "STRIP"))
    conf = _emitted(prof)
    assert "llvm-" not in conf and "-fuse-ld=lld" not in conf


def test_toolchain_llvm_on_a_gcc_profile_emits_the_whole_llvm_bundle():
    prof = mw._apply_cli_toolchain(_resolved(_GCC), BuildOptions(toolchain_override="llvm"))
    assert (prof["CC"], prof["CXX"], prof["AR"]) == ("clang", "clang++", "llvm-ar")
    assert "-fuse-ld=lld" in prof["LDFLAGS"]


def test_cli_toolchain_beats_a_saved_row():
    prof = _resolved(_GCC, rows={"htop": {"toolchain": "llvm"}})
    assert prof["CC"] == "clang"
    prof = mw._apply_cli_toolchain(prof, BuildOptions(toolchain_override="gcc"))
    assert prof["CC"] == "gcc" and "AR" not in prof


def test_no_toolchain_leaves_the_profile_alone():
    prof = _resolved(_LLVM)
    assert mw._apply_cli_toolchain(prof, BuildOptions()) is prof


# --- which CLI spellings count as a toolchain --------------------------------

@pytest.mark.parametrize(("argv", "expected"), [
    (["--toolchain", "gcc"], "gcc"),
    (["--cc", "gcc", "--cxx", "g++"], "gcc"),
    (["--cc", "clang", "--cxx", "clang++"], "llvm"),
    (["--cc", "clang-19", "--cxx", "clang++-19"], None),   # raw escape hatch
    (["--cc", "gcc"], None),                                # one half only
    (["--toolchain", "llvm", "--cc", "gcc", "--cxx", "g++"], "llvm"),
])
def test_cli_toolchain_resolution(argv, expected):
    args = build_parser().parse_args(["build", "htop", *argv])
    assert build_cmd._cli_toolchain(args) == expected


@pytest.mark.parametrize("argv", [
    ["--save-toolchain"],
    ["--save-toolchain", "--reset-toolchain", "--toolchain", "gcc"],
])
def test_save_toolchain_misuse_is_refused(argv):
    args = build_parser().parse_args(["build", "htop", *argv])
    pre = build_cmd.BuildVerb().pre_check(args)
    assert pre.blocker and pre.exit_code == 2


def test_pgo_with_a_gcc_toolchain_is_refused(monkeypatch):
    monkeypatch.setattr(build_cmd, "load_config_with_overrides",
                        lambda _a: {"defaults": {"toolchain": "llvm"}})
    args = build_parser().parse_args(["build", "htop", "--pgo=record", "--toolchain", "gcc"])
    assert "requires the LLVM toolchain" in (build_cmd.BuildVerb().pre_check(args).blocker or "")


def test_run_kernel_takes_toolchain_with_the_same_values():
    args = build_parser().parse_args(["run", "kernel", "--toolchain", "llvm"])
    assert args.compiler == "llvm"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "kernel", "--compiler", "llvm"])


# --- saving and resetting, no failed build needed -----------------------------

_PROFILES = '[defaults]\nprofile = "bare"\n'


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    p = tmp_path / "profiles.toml"
    p.write_text(_PROFILES)
    monkeypatch.setattr(mw, "_active_profiles_path", lambda: p)
    return p


def _rows(p):
    return tomllib.loads(p.read_text()).get("package_compiler_overrides", {})


def test_save_writes_the_row_before_the_build(profiles):
    opts = BuildOptions(toolchain_override="gcc", save_toolchain=True)
    mw._apply_toolchain_row_choice(opts, {}, "htop")
    assert _rows(profiles) == {"htop": {"toolchain": "gcc"}}


def test_reset_removes_the_row_and_the_in_memory_copy(profiles):
    write_package_compiler_override(profiles, "htop", "", "", "", toolchain="gcc")
    write_package_compiler_override(profiles, "vim", "gcc", "g++", "bfd")
    config = {"package_compiler_overrides": {"htop": {"toolchain": "gcc"},
                                             "vim": {"cc": "gcc"}}}
    out = mw._apply_toolchain_row_choice(BuildOptions(reset_toolchain=True), config, "htop")
    assert _rows(profiles) == {"vim": {"cc": "gcc", "cxx": "g++", "ld": "bfd"}}
    assert out["package_compiler_overrides"] == {"vim": {"cc": "gcc"}}
    assert "htop" in config["package_compiler_overrides"]  # caller's dict untouched


def test_neither_flag_touches_profiles_toml(profiles):
    before = profiles.read_text()
    config = {}
    assert mw._apply_toolchain_row_choice(BuildOptions(toolchain_override="gcc"),
                                          config, "htop") is config
    assert profiles.read_text() == before


# --- the row delete itself ---------------------------------------------------

def test_delete_removes_only_that_row(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text(_PROFILES + "\n[profiles.bare]\nCC = \"clang\"\n")
    for pkg in ("htop", "libstdc++", "vim"):
        write_package_compiler_override(p, pkg, "gcc", "g++", "bfd")
    assert delete_package_compiler_override(p, "libstdc++") is True
    data = tomllib.loads(p.read_text())
    assert set(data["package_compiler_overrides"]) == {"htop", "vim"}
    assert data["profiles"]["bare"]["CC"] == "clang"


@pytest.mark.parametrize("text", [_PROFILES, _PROFILES + "\n[package_compiler_overrides]\n"])
def test_delete_without_a_row_is_a_no_op(tmp_path, text):
    p = tmp_path / "profiles.toml"
    p.write_text(text)
    assert delete_package_compiler_override(p, "htop") is False
    assert p.read_text() == text


def test_delete_does_not_reach_past_the_section(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text('[package_compiler_overrides]\n\n[other]\n"htop" = 1\n')
    assert delete_package_compiler_override(p, "htop") is False


def test_build_core_passes_the_choice_to_named_targets_only():
    """Dependencies keep their own profile: the choice rides each target's
    BuildOptions, never prepare_deps."""
    import inspect

    from sysforge import build_core
    deps_params = inspect.signature(build_core.prepare_deps).parameters
    assert not {"toolchain", "save_toolchain", "reset_toolchain"} & set(deps_params)
    assert {"toolchain", "save_toolchain", "reset_toolchain"} <= set(
        inspect.signature(build_core.build_and_install).parameters)
