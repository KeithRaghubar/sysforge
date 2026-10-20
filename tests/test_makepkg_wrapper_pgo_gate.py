# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""The `pgo_llvm_toolchain` profdata gate in `makepkg_wrapper.run()` (3.5.0-DEV2).

A build on that profile reuses saved profdata when it matches the LLVM being
built; otherwise it skips (`PGOBuildSkipped`, the default and the only
non-interactive answer) or, when asked, builds without PGO.
"""
from unittest.mock import patch

import pytest

from sysforge.build import makepkg_wrapper as mw
from sysforge.build.makepkg_wrapper import BuildOptions, PGOBuildSkipped


def _run(tmp_path, pgo_state, *, tty=False, answer="s", **opts):
    pkgbuild = tmp_path / "llvm" / "PKGBUILD"
    pkgbuild.parent.mkdir()
    pkgbuild.write_text("pkgname=llvm\npkgver=22.1.0\npkgrel=1\n")
    seen = {}
    with patch.object(mw, "_resolve_pgo_state", return_value=pgo_state), \
         patch.object(mw.sys.stdin, "isatty", return_value=tty), \
         patch.object(mw, "prompt_choice", return_value=answer), \
         patch.object(mw, "_run_build",
                      side_effect=lambda *a, **k: seen.update(k)):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override="pgo_llvm_toolchain", **opts))
    return seen


def test_matching_profdata_is_injected(tmp_path):
    seen = _run(tmp_path, ("ready", "/store/clang.profdata"))
    assert seen["compiler_flags_extra"] == "-fprofile-use=/store/clang.profdata"


def test_matching_profdata_joins_the_stage_s_own_flags(tmp_path):
    seen = _run(tmp_path, ("ready", "/s/c.profdata"), compiler_flags_extra="-g1")
    assert seen["compiler_flags_extra"] == "-g1 -fprofile-use=/s/c.profdata"


def test_missing_profdata_skips_off_a_terminal(tmp_path):
    with pytest.raises(PGOBuildSkipped, match="sysforge run toolchain"):
        _run(tmp_path, ("absent", "no profdata at /store"))


def test_missing_profdata_skips_by_default_on_a_terminal(tmp_path):
    with pytest.raises(PGOBuildSkipped, match="LLVM 21"):
        _run(tmp_path, ("mismatch", "profdata is from LLVM 21"), tty=True, answer="s")


def test_plain_build_on_request_builds_without_profile_use(tmp_path):
    seen = _run(tmp_path, ("absent", "no profdata"), tty=True, answer="p")
    assert "-fprofile-use" not in (seen.get("compiler_flags_extra") or "")


def test_a_stage_managed_build_never_consults_the_gate(tmp_path):
    with patch.object(mw, "_resolve_pgo_state",
                      side_effect=AssertionError("gate consulted")):
        pkgbuild = tmp_path / "PKGBUILD"
        pkgbuild.write_text("pkgname=llvm\npkgver=22.1.0\npkgrel=1\n")
        with patch.object(mw, "_run_build"):
            mw.run(pkgbuild, options=BuildOptions(
                update=False, pkg_log=False, profile_override="pgo_llvm_toolchain",
                pgo_managed=True))
