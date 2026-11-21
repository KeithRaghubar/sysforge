# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""The toolchain's PGO cmake options against the current official llvm
PKGBUILD (4.0.0-B12).

The unit tests patch PKGBUILD excerpts shaped like the official one, so they
only check sysforge against what it assumes the recipe looks like. On
2026-10-07 the official llvm PKGBUILD merged the whole suite and began building
compiler-rt, openmp and libc++ with the just-built clang. That made the
CFLAGS-delivered ``-fprofile-generate`` fail Pass 1, and every unit test still
passed. This canary runs ``patch_llvm_pgo`` on the live PKGBUILD and checks the
options land in the main ``cmake -S llvm`` configure, the one the runtimes and
device builds never inherit, and that the result is still valid bash.

Skipped by ``make test``; ``make test-live`` runs it. It downloads one text
file.
"""
import subprocess
import urllib.error
import urllib.request

import pytest

from sysforge.primitives.pkgbuild_patcher import (
    _cmake_statement_end,
    patch_llvm_pgo,
)

pytestmark = [pytest.mark.network]

_PKGBUILD_URL = (
    "https://gitlab.archlinux.org/archlinux/packaging/packages/llvm/-/raw/main/PKGBUILD"
)


@pytest.fixture(scope="module")
def live_pkgbuild() -> str:
    req = urllib.request.Request(_PKGBUILD_URL, headers={"User-Agent": "sysforge-tests"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https URL
            return resp.read().decode()
    except (urllib.error.URLError, TimeoutError) as exc:
        pytest.fail(f"could not fetch the official llvm PKGBUILD: {exc}")


def _main_configure(text: str) -> str:
    start = text.index("cmake -S llvm")
    line_start = text.rfind("\n", 0, start) + 1
    return text[start:_cmake_statement_end(text, line_start)]


@pytest.mark.parametrize(("mode", "path", "expected"), [
    ("generate", "/store", "-DLLVM_BUILD_INSTRUMENTED=IR"),
    ("use", "/store/clang.profdata", '-DLLVM_PROFDATA_FILE="/store/clang.profdata"'),
])
def test_live_llvm_pgo_lands_in_main_configure(tmp_path, live_pkgbuild, mode, path, expected):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(live_pkgbuild)
    assert patch_llvm_pgo(p, mode, path) is True
    text = p.read_text()
    assert expected in _main_configure(text)
    assert text.count(expected) == 1
    result = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)  # noqa: TID251 — syntax check of a downloaded fixture, never executed
    assert result.returncode == 0, result.stderr
