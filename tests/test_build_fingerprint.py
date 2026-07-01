# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/build_fingerprint.py — the opt-in Pass-3 reuse cache.

The whole point of this module is to *never* reuse a stale artifact, so the
tests lean on the failure-safe direction: every input change must change the
fingerprint, and any deviation in the recorded artifact (missing / resized /
re-timestamped) must be a cache miss.
"""
import json

import pytest

from sysforge.primitives import build_fingerprint as bf


def _artifact(d, pkgname, ver="1.0-1", content=b"pkg"):
    """Create a fake makepkg artifact in ``d`` and return its path."""
    p = d / f"{pkgname}-{ver}-x86_64.pkg.tar.zst"
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# compute_fingerprint — input sensitivity + order independence
# ---------------------------------------------------------------------------

def test_fingerprint_stable_for_identical_inputs():
    a = bf.compute_fingerprint({"pass_id": "3a", "pkgbase": "llvm", "x": 1})
    b = bf.compute_fingerprint({"x": 1, "pkgbase": "llvm", "pass_id": "3a"})
    assert a == b  # key order irrelevant


@pytest.mark.parametrize("key,val", [
    ("pass_id", "3b"),
    ("pkgbase", "clang"),
    ("pkgbuild_sha", "deadbeef"),
    ("source_commit", "abc123"),
    ("cc_identity", "clang 99"),
    ("compiler_flags_extra", "-fprofile-use=/other"),
    ("linker_flags_extra", "-Wl,foo"),
    ("cmake_llvm_dir", "/staging/x"),
    ("config_digest", "other"),
    ("profdata_sha", "ffff"),
])
def test_fingerprint_changes_on_each_input(key, val):
    base = {
        "pass_id": "3a", "pkgbase": "llvm", "pkgbuild_sha": "aaaa",
        "source_commit": "c0", "cc_identity": "clang 1",
        "compiler_flags_extra": "-fprofile-use=/p", "linker_flags_extra": None,
        "cmake_llvm_dir": None, "config_digest": "d", "profdata_sha": "0000",
    }
    before = bf.compute_fingerprint(base)
    after = bf.compute_fingerprint({**base, key: val})
    assert before != after, f"changing {key} must change the fingerprint"


def test_fingerprint_changes_on_makedep_version():
    base = {"pkgbase": "llvm", "makedep_versions": {"cmake": "3.29-1"}}
    bumped = {"pkgbase": "llvm", "makedep_versions": {"cmake": "3.30-1"}}
    assert bf.compute_fingerprint(base) != bf.compute_fingerprint(bumped)


def test_fingerprint_changes_on_staged_dep_fp():
    """Merkle chain: a changed staged-dep fingerprint changes the consumer's."""
    base = {"pkgbase": "clang", "staged_dep_fps": ["fp-llvm-v1"]}
    rebuilt = {"pkgbase": "clang", "staged_dep_fps": ["fp-llvm-v2"]}
    assert bf.compute_fingerprint(base) != bf.compute_fingerprint(rebuilt)


def test_fingerprint_schema_bump_invalidates(monkeypatch):
    components = {"pass_id": "3a", "pkgbase": "llvm"}
    before = bf.compute_fingerprint(components)
    monkeypatch.setattr(bf, "_SCHEMA", bf._SCHEMA + 1)
    assert bf.compute_fingerprint(components) != before


# ---------------------------------------------------------------------------
# hash_file / hash_obj
# ---------------------------------------------------------------------------

def test_hash_file_none_when_missing(tmp_path):
    assert bf.hash_file(tmp_path / "nope") is None


def test_hash_file_differs_on_content(tmp_path):
    a = tmp_path / "a"
    a.write_bytes(b"one")
    h1 = bf.hash_file(a)
    a.write_bytes(b"two")
    h2 = bf.hash_file(a)
    assert h1 and h2 and h1 != h2


def test_hash_obj_order_independent():
    assert bf.hash_obj({"a": 1, "b": 2}) == bf.hash_obj({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# source_commit / clang_identity — never raise
# ---------------------------------------------------------------------------

def test_source_commit_none_for_non_repo(tmp_path):
    assert bf.source_commit(tmp_path) is None


def test_source_commit_reads_head(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "c"], check=True)
    sha = bf.source_commit(tmp_path)
    assert sha and len(sha) == 40


def test_clang_identity_none_for_empty():
    assert bf.clang_identity(None) == "none"
    assert bf.clang_identity("") == "none"


def test_clang_identity_never_raises_for_bogus_path(tmp_path):
    # A path that exists but is not a runnable compiler: stat works, --version
    # fails, and the function still returns a string (path + size/mtime).
    f = tmp_path / "fake-clang"
    f.write_text("not a binary")
    ident = bf.clang_identity(str(f))
    assert str(f) in ident


def test_clang_identity_tolerates_mocked_subprocess(monkeypatch):
    # Full-flow toolchain tests mock subprocess.run globally; a MagicMock
    # stdout must not poison the "|".join (the result must stay a str).
    from unittest.mock import MagicMock
    monkeypatch.setattr(bf.subprocess, "run", lambda *a, **k: MagicMock())
    ident = bf.clang_identity("/usr/bin/clang")
    assert isinstance(ident, str)


# ---------------------------------------------------------------------------
# toolchain_fingerprint / resolve_libllvm (Q9)
# ---------------------------------------------------------------------------

def test_toolchain_fingerprint_default_method_is_clang_identity():
    # The "fingerprint" method (default) is exactly clang_identity — fast, no
    # hashing.
    assert bf.toolchain_fingerprint("fingerprint", "/usr/bin/clang") == \
        bf.clang_identity("/usr/bin/clang")


def test_toolchain_fingerprint_unknown_method_falls_back_to_clang_identity():
    assert bf.toolchain_fingerprint("bogus", "/usr/bin/clang") == \
        bf.clang_identity("/usr/bin/clang")


def test_toolchain_fingerprint_content_hash_hashes_libllvm(tmp_path, monkeypatch):
    # content_hash mode hashes the resolved libLLVM.so (the codegen carrier),
    # not the driver binary. Different libLLVM bytes → different fingerprint,
    # even with an identical clang driver / version line.
    so = tmp_path / "libLLVM.so.18.1"
    so.write_bytes(b"llvm-bytes-A")
    monkeypatch.setattr(bf, "resolve_libllvm", lambda cc: so)
    monkeypatch.setattr(bf, "compiler_version_line", lambda cc: "clang 18")
    fp_a = bf.toolchain_fingerprint("content_hash", "/usr/bin/clang")

    so.write_bytes(b"llvm-bytes-B-rebuilt-pgo")
    fp_b = bf.toolchain_fingerprint("content_hash", "/usr/bin/clang")
    assert fp_a != fp_b
    assert "content_hash" in fp_a


def test_toolchain_fingerprint_content_hash_falls_back_when_no_libllvm(monkeypatch):
    # A gcc variant (or any host where libLLVM can't be resolved) must not
    # crash — it degrades to clang_identity.
    monkeypatch.setattr(bf, "resolve_libllvm", lambda cc: None)
    fp = bf.toolchain_fingerprint("content_hash", "/usr/bin/gcc")
    assert fp == bf.clang_identity("/usr/bin/gcc")


def test_resolve_libllvm_finds_sibling_lib(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib").mkdir()
    cc = tmp_path / "bin" / "clang"
    cc.write_text("#!/bin/sh\n")
    so = tmp_path / "lib" / "libLLVM.so.18.1"
    so.write_bytes(b"x")
    assert bf.resolve_libllvm(str(cc)) == so


def test_resolve_libllvm_none_when_absent(tmp_path):
    cc = tmp_path / "bin" / "clang"
    cc.parent.mkdir()
    cc.write_text("#!/bin/sh\n")
    assert bf.resolve_libllvm(str(cc)) is None
    assert bf.resolve_libllvm(None) is None


# ---------------------------------------------------------------------------
# cache_key / load_cache / save_cache
# ---------------------------------------------------------------------------

def test_cache_key_is_distinct_per_pass():
    assert bf.cache_key("3a", "llvm") != bf.cache_key("3b", "llvm")
    assert bf.cache_key("3a", "llvm") != bf.cache_key("3a", "clang")


def test_load_cache_missing_returns_empty(tmp_path):
    assert bf.load_cache(tmp_path / "nope.json") == {}


def test_load_cache_corrupt_returns_empty(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text("{ not json")
    assert bf.load_cache(p) == {}


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "cache.json"  # parent created by save_cache
    cache = {"k": {"fingerprint": "fp", "artifacts": []}}
    bf.save_cache(p, cache)
    assert bf.load_cache(p) == cache
    assert json.loads(p.read_text()) == cache


# ---------------------------------------------------------------------------
# record_build / cache_hit — the safety-critical pair
# ---------------------------------------------------------------------------

def test_record_then_hit(tmp_path):
    _artifact(tmp_path, "llvm")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    recorded = bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    assert len(recorded) == 1
    hit = bf.cache_hit(cache, key, "fp1")
    assert hit and len(hit) == 1


def test_hit_miss_on_wrong_fingerprint(tmp_path):
    _artifact(tmp_path, "llvm")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    assert bf.cache_hit(cache, key, "fp-DIFFERENT") is None


def test_hit_miss_when_artifact_removed(tmp_path):
    art = _artifact(tmp_path, "llvm")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    art.unlink()
    assert bf.cache_hit(cache, key, "fp1") is None


def test_hit_miss_when_artifact_size_changes(tmp_path):
    art = _artifact(tmp_path, "llvm", content=b"small")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    art.write_bytes(b"a much larger payload than before")  # size + mtime change
    assert bf.cache_hit(cache, key, "fp1") is None


def test_hit_miss_when_artifact_mtime_changes(tmp_path):
    import os
    art = _artifact(tmp_path, "llvm", content=b"same-size")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    st = art.stat()
    os.utime(art, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert bf.cache_hit(cache, key, "fp1") is None


def test_split_members_all_required(tmp_path):
    """A split build only reuses when *every* member artifact is still present."""
    _artifact(tmp_path, "llvm")
    libs = _artifact(tmp_path, "llvm-libs")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    recorded = bf.record_build(cache, key, "fp1", [tmp_path], ["llvm", "llvm-libs"])
    assert len(recorded) == 2
    assert bf.cache_hit(cache, key, "fp1")  # both present
    libs.unlink()
    assert bf.cache_hit(cache, key, "fp1") is None  # one missing → miss


def test_record_nothing_when_no_artifact(tmp_path):
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    assert bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"]) == []
    assert key not in cache  # nothing cached → nothing to falsely hit later


def test_version_anchor_avoids_sibling_swallow(tmp_path):
    """The 'llvm' glob must not pick up the 'llvm-libs' sibling artifact."""
    _artifact(tmp_path, "llvm")
    _artifact(tmp_path, "llvm-libs")
    cache: dict = {}
    key = bf.cache_key("3a", "llvm")
    recorded = bf.record_build(cache, key, "fp1", [tmp_path], ["llvm"])
    assert len(recorded) == 1
    assert recorded[0].name.startswith("llvm-1")
