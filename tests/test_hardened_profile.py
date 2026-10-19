"""[profiles.hardened]: OpenSSF compiler hardening beyond Arch's baseline.

3.5.0-F9; enforces standards row 33.
"""
import tomllib
from pathlib import Path

import pytest

from sysforge.primitives.profile import _expand_toolchain, merge_extends

_SHIPPED = Path(__file__).resolve().parent.parent / "etc" / "sysforge" / "profiles.toml"

# The adopted OpenSSF set: each flag here is one standards row 33 commits to.
HARDENED_CFLAGS = ["-ftrivial-auto-var-init=zero", "-fstrict-flex-arrays=3"]
HARDENED_CXXFLAGS = HARDENED_CFLAGS + ["-Wp,-D_GLIBCXX_ASSERTIONS"]
HARDENED_LDFLAGS = ["-z,noexecstack", "-z,relro", "-z,now"]


def _resolved(toolchain):
    cfg = tomllib.loads(_SHIPPED.read_text())
    prof = merge_extends("hardened", cfg["profiles"])
    prof["CXXFLAGS"] = prof["CXXFLAGS"].replace("$CFLAGS", prof["CFLAGS"])
    return _expand_toolchain(prof, toolchain)


@pytest.mark.parametrize("toolchain", ["gcc", "llvm"])
def test_hardened_carries_the_openssf_set(toolchain):
    prof = _resolved(toolchain)
    for flag in HARDENED_CFLAGS:
        assert flag in prof["CFLAGS"].split()
    for flag in HARDENED_CXXFLAGS:
        assert flag in prof["CXXFLAGS"].split()
    for frag in HARDENED_LDFLAGS:
        assert frag in prof["LDFLAGS"]
    assert "-O2" in prof["CFLAGS"].split()


def test_llvm_path_adds_lld_gcc_path_does_not():
    assert "-fuse-ld=lld" in _resolved("llvm")["LDFLAGS"]
    assert "-fuse-ld=" not in _resolved("gcc")["LDFLAGS"]
    assert _resolved("gcc")["CC"] == "gcc"
    assert _resolved("llvm")["CC"] == "clang"


def test_hardened_does_not_block_dlopen():
    # -z nodlopen marks every object DF_1_NOOPEN: glibc then refuses to dlopen
    # it, breaking plugins, Python extensions, PAM/NSS modules and mesa drivers.
    for tc in ("gcc", "llvm"):
        assert "nodlopen" not in _resolved(tc)["LDFLAGS"]
