"""[profiles.optimized] keeps LTO under the default gcc toolchain (3.5.0-B111).

A hardcoded -fuse-ld=lld paired with gcc tripped emit_makepkg_conf's GCC+lld
guard, which clears LTOFLAGS and flips OPTIONS to !lto on every package.
"""
import shutil
import tomllib
from pathlib import Path

import pytest

from sysforge.build.makepkg_wrapper import emit_makepkg_conf
from sysforge.primitives.config import parse_system_makepkg_conf
from sysforge.primitives.profile import _expand_toolchain, merge_extends

_ROOT = Path(__file__).resolve().parent.parent
_SHIPPED = _ROOT / "etc" / "sysforge" / "profiles.toml"
_FIXTURE = _ROOT / "tests" / "data" / "etc" / "sysforge" / "profiles.toml"


@pytest.fixture(autouse=True)
def _linkers_on_path(monkeypatch):
    # The emitter downgrades a linker it cannot find; keep the host out of it.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")


@pytest.fixture
def sys_conf(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text('CARCH="x86_64"\nCFLAGS="-O2"\nLTOFLAGS="-flto=auto"\n'
                 'OPTIONS=(strip docs lto)\n')
    return p


def _resolved(source, toolchain):
    prof = merge_extends("optimized", tomllib.loads(source.read_text())["profiles"])
    return _expand_toolchain(prof, toolchain)


def _emit(prof, sys_conf):
    with emit_makepkg_conf(prof, system_conf_path=sys_conf) as conf_path:
        return parse_system_makepkg_conf(conf_path)


@pytest.mark.parametrize("source", [_SHIPPED, _FIXTURE], ids=["shipped", "fixture"])
def test_optimized_declares_no_linker(source):
    assert "-fuse-ld=" not in _resolved(source, None)["LDFLAGS"]


def test_optimized_under_gcc_keeps_lto(sys_conf):
    conf = _emit(_resolved(_SHIPPED, "gcc"), sys_conf)
    assert conf["LTOFLAGS"] != ""
    assert "!lto" not in conf.get("OPTIONS", "")
    assert "-fuse-ld=" not in conf["LDFLAGS"]
    # lld-only, so the guard strips it under bfd.
    assert "--icf=all" not in conf["LDFLAGS"]


def test_optimized_under_llvm_links_with_lld(sys_conf):
    prof = _resolved(_SHIPPED, "llvm")
    assert "-fuse-ld=lld" in prof["LDFLAGS"]
    conf = _emit(prof, sys_conf)
    assert "-fuse-ld=lld" in conf["LDFLAGS"]
    assert "--icf=all" in conf["LDFLAGS"]
    assert "!lto" not in conf.get("OPTIONS", "")
