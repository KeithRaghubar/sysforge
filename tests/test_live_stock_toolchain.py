# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""The stock toolchain ``bench toolchain`` compares against, from the live
mirrors (3.5.0-B146).

The unit tests stub the repo, so they only check sysforge against what it
assumes the stock packages contain. Arch's clang 23.1.3-4 broke that
assumption (it moved libclang-cpp and the builtin headers to ``clang-libs``)
and every unit test still passed. This canary runs the bench's own fetch,
unpack and probe against the current repo, then checks the two things a
repackaging breaks: that the stock compiler loads its libraries from the
stock prefix, never the host's (which would time the sysforge build as
stock), and that it compiles with its builtin headers.

Skipped by ``make test``; ``make test-live`` (its own CI job) runs it. It
downloads the stock packages unless the pacman cache already holds them. The
stock compiler needs llvm-libs' runtime dependencies on the host; CI installs
``llvm-libs`` for them, which is also the host copy the library check below
must not resolve to.
"""
from pathlib import Path

import pytest

from sysforge import bench_core
from sysforge.primitives import run, stock_pkg

pytestmark = [pytest.mark.network, pytest.mark.host_pacman]


@pytest.fixture(scope="module")
def stock_prefix(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("stock-toolchain")
    try:
        _pkgs, archives = bench_core._fetch_stock(tmp, offline=False)
    except stock_pkg.StockUnavailable as exc:
        pytest.fail(f"could not obtain the stock packages: {exc}")
    prefix = tmp / "stock"
    stock_pkg.extract(archives, prefix, downloaded_in=tmp)
    return prefix


def test_the_stock_prefix_holds_everything_the_compiler_needs(stock_prefix):
    stock_pkg.check_complete(stock_prefix)
    bench_core._probe_stock(stock_prefix)


@pytest.mark.parametrize("lib", ["libLLVM", "libclang-cpp"])
def test_the_stock_compiler_loads_its_libraries_from_the_prefix(stock_prefix, lib):
    res = run.probe(["ldd", str(stock_prefix / "usr/bin/clang")],
                    env=bench_core._stock_env(stock_prefix))
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if f"{lib}.so" in ln]
    assert lines, f"stock clang does not link {lib}:\n{res.stdout}"
    assert all(str(stock_prefix) in ln for ln in lines), \
        f"{lib} resolves outside the stock prefix:\n" + "\n".join(lines)


def test_the_stock_compiler_compiles_with_its_builtin_headers(stock_prefix, tmp_path):
    src = tmp_path / "builtins.c"
    src.write_text("#include <stddef.h>\n#include <stdint.h>\n#include <stdarg.h>\n"
                   "size_t width(void) { return sizeof(uint64_t); }\n")
    res = run.probe([str(stock_prefix / "usr/bin/clang"), "-O2", "-c", str(src),
                     "-o", str(tmp_path / "builtins.o")],
                    env=bench_core._stock_env(stock_prefix))
    assert res.returncode == 0, res.stderr
