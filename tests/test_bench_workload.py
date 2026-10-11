# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""bench_workload: the bench's own TUs, corpus resolution and fingerprint (3.4.0-F6)."""
from pathlib import Path

import pytest

from sysforge.primitives import bench_workload as bw


def test_default_workload_writes_c_and_cxx(tmp_path):
    w = bw.default_workload(tmp_path)
    assert w.label == "built-in synthetic TUs"
    assert sorted(p.suffix for p in w.files) == [".c", ".cpp"]
    assert all(p.is_file() and p.stat().st_size > 0 for p in w.files)


def test_default_fingerprint_matches_written_files(tmp_path):
    w = bw.default_workload(tmp_path)
    assert w.fingerprint == bw.default_fingerprint()
    assert bw.fingerprint(w.files) == w.fingerprint


def test_default_tu_is_not_the_bolt_training_tu():
    from sysforge.primitives import bolt
    assert bolt._DEFAULT_WORKLOAD_TU not in (bw._CXX_TU, bw._C_TU)


def test_configured_workload_filters_and_sorts_a_dir(tmp_path):
    (tmp_path / "b.cpp").write_text("int b;")
    (tmp_path / "a.c").write_text("int a;")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.cc").write_text("int c;")
    w = bw.configured_workload([str(tmp_path)])
    assert [p.name for p in w.files] == ["a.c", "b.cpp", "c.cc"]
    assert str(tmp_path) in w.label


def test_configured_workload_accepts_files(tmp_path):
    f = tmp_path / "x.cxx"
    f.write_text("int x;")
    assert bw.configured_workload([str(f)]).files == (f,)


def test_configured_workload_with_no_sources_raises(tmp_path):
    (tmp_path / "readme.md").write_text("x")
    with pytest.raises(ValueError, match="no C/C\\+\\+ sources"):
        bw.configured_workload([str(tmp_path)])


def test_configured_workload_missing_path_raises(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        bw.configured_workload([str(tmp_path / "nope")])


def test_fingerprint_changes_with_bytes(tmp_path):
    f = tmp_path / "a.c"
    f.write_text("int a;")
    before = bw.fingerprint([f])
    f.write_text("int a = 1;")
    assert bw.fingerprint([f]) != before


def test_compile_argv_cxx_gets_std_and_c_does_not():
    cxx = bw.compile_argv("/p/clang", "/p/clang++", Path("t.cpp"), Path("t.o"))
    c = bw.compile_argv("/p/clang", "/p/clang++", Path("t.c"), Path("t.o"))
    assert cxx == ["/p/clang++", "-std=c++17", "-O2", "-c", "t.cpp", "-o", "t.o"]
    assert c == ["/p/clang", "-O2", "-c", "t.c", "-o", "t.o"]
