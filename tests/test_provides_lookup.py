"""
test_provides_lookup.py — unit tests for the pacman -Fq wrapper used by
`sysforge doctor --suggest`.

Covers:
    _soname_query_path       — entry parsing, lib/lib32 selection
    suggest_for_soname       — hit / multi-hit / no-hit / error exit /
                               pacman missing / non-soname input
    files_db_present         — absent/present dir, with and without .files
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives import provides_lookup as pl


# ---------------------------------------------------------------------------
# _soname_query_path
# ---------------------------------------------------------------------------

def test_soname_query_path_versioned_entry():
    assert pl._soname_query_path("libcap.so=2", lib32=False) == "usr/lib/libcap.so.2"


def test_soname_query_path_unversioned_entry():
    assert pl._soname_query_path("libfoo.so", lib32=False) == "usr/lib/libfoo.so"


def test_soname_query_path_arch_suffix_stripped():
    assert pl._soname_query_path("libcap.so=2-64", lib32=False) == "usr/lib/libcap.so.2"


def test_soname_query_path_lib32():
    assert pl._soname_query_path("libcap.so=2", lib32=True) == "usr/lib32/libcap.so.2"


def test_soname_query_path_resolved_soname():
    """A plain soname like 'libfoo.so.3' (from an ABI warning) also works."""
    assert pl._soname_query_path("libfoo.so.3", lib32=False) == "usr/lib/libfoo.so.3"


def test_soname_query_path_non_soname_returns_none():
    assert pl._soname_query_path("glibc>=2.40", lib32=False) is None


# ---------------------------------------------------------------------------
# suggest_for_soname
# ---------------------------------------------------------------------------

def _fake_run(stdout: str, returncode: int):
    def run(cmd, **_kw):
        r = MagicMock()
        r.stdout = stdout
        r.stderr = ""
        r.returncode = returncode
        return r
    return run


def test_suggest_single_hit():
    out = pl.suggest_for_soname(
        "libcap.so=2",
        run_fn=_fake_run("core/libcap\n", 0),
    )
    assert out == ["core/libcap"]


def test_suggest_multi_hit_deduped():
    out = pl.suggest_for_soname(
        "libfoo.so",
        run_fn=_fake_run("core/foo\nextra/foo-compat\ncore/foo\n", 0),
    )
    assert out == ["core/foo", "extra/foo-compat"]


def test_suggest_no_match_returns_empty():
    out = pl.suggest_for_soname(
        "libghost.so=99",
        run_fn=_fake_run("", 1),
    )
    assert out == []


def test_suggest_error_exit_returns_empty():
    """Non-0/1 exit (e.g. db lock) is treated as a miss, not a crash."""
    out = pl.suggest_for_soname(
        "libcap.so=2",
        run_fn=_fake_run("error: something\n", 2),
    )
    assert out == []


def test_suggest_filters_error_lines():
    """pacman warnings on stdout shouldn't surface as candidate pkgs."""
    out = pl.suggest_for_soname(
        "libcap.so=2",
        run_fn=_fake_run("error: config not found\ncore/libcap\n", 0),
    )
    assert out == ["core/libcap"]


def test_suggest_pacman_missing_returns_empty():
    def run(_cmd, **_kw):
        raise FileNotFoundError("pacman")
    assert pl.suggest_for_soname("libcap.so=2", run_fn=run) == []


def test_suggest_non_soname_returns_empty():
    # Non-soname entries shouldn't even invoke pacman.
    called = []
    def run(cmd, **_kw):
        called.append(cmd)
        return MagicMock(stdout="", stderr="", returncode=0)
    out = pl.suggest_for_soname("glibc>=2.40", run_fn=run)
    assert out == []
    assert called == []


# ---------------------------------------------------------------------------
# files_db_present
# ---------------------------------------------------------------------------

def test_files_db_present_false_when_dir_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_FILES_DB_DIR", tmp_path / "nonexistent")
    assert pl.files_db_present() is False


def test_files_db_present_false_when_dir_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_FILES_DB_DIR", tmp_path)
    assert pl.files_db_present() is False


def test_files_db_present_true_with_files_entry(tmp_path, monkeypatch):
    (tmp_path / "core.files").write_bytes(b"")
    monkeypatch.setattr(pl, "_FILES_DB_DIR", tmp_path)
    assert pl.files_db_present() is True


def test_files_db_present_ignores_non_files_entries(tmp_path, monkeypatch):
    (tmp_path / "core.db").write_bytes(b"")
    (tmp_path / "core.db.sig").write_bytes(b"")
    monkeypatch.setattr(pl, "_FILES_DB_DIR", tmp_path)
    assert pl.files_db_present() is False
