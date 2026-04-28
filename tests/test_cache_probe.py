"""
test_cache_probe.py — unit tests for passive cache monitoring.

All subprocess calls are replaced by passing output directly to the internal
parsers, so no real ccache/sccache installation is required.

Covers:
    _parse_ccache_tab        — key=value tab parsing, missing keys, bad ints
    _parse_sccache_text      — hit/miss/size parsing, sub-category skipping
    diff_ccache              — correct delta computation
    diff_sccache             — correct delta computation
    _fmt_bytes               — B, KiB, MiB, GiB thresholds
    probe_pacman_cache       — temp dir with fake pkg files, empty dir, missing dir
    probe_ldso_mtime         — temp file, missing path
    probe_thinlto_cache      — LDFLAGS parsing: bare token, -Wl form, absent,
                               dir not yet created, dir with files
    emit_system_probes       — INFO lines emitted when probes find data
    emit_build_stats         — INFO lines for cc/sc deltas (hits, misses, n/a)
    record_build_result /
      emit_session_report    — accumulation, totals, empty session
    reset_session            — clears accumulated records
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_DATA = Path(__file__).parent / "data"

from sysforge.primitives.cache_probe import (
    _fmt_bytes,
    _parse_ccache_tab,
    _parse_sccache_text,
    diff_ccache,
    diff_sccache,
    emit_build_stats,
    emit_session_report,
    emit_system_probes,
    probe_ldso_mtime,
    probe_pacman_cache,
    probe_thinlto_cache,
    record_build_result,
    reset_session,
)


# ---------------------------------------------------------------------------
# _parse_ccache_tab
# ---------------------------------------------------------------------------

CCACHE_TAB_SAMPLE = (_DATA / "ccache_sample.txt").read_text()


def test_parse_ccache_tab_full():
    r = _parse_ccache_tab(CCACHE_TAB_SAMPLE)
    assert r["direct_hits"] == 10
    assert r["preprocessed_hits"] == 2
    assert r["misses"] == 5
    assert r["files"] == 173
    assert r["size_bytes"] == 536870912


def test_parse_ccache_tab_missing_keys():
    r = _parse_ccache_tab("cache_dir\t/tmp\n")
    assert r["direct_hits"] == 0
    assert r["misses"] == 0
    assert r["size_bytes"] == 0


def test_parse_ccache_tab_bad_int():
    r = _parse_ccache_tab("direct_cache_hit\tnot_a_number\n")
    assert r["direct_hits"] == 0


def test_parse_ccache_tab_empty():
    r = _parse_ccache_tab("")
    assert r == {"direct_hits": 0, "preprocessed_hits": 0, "misses": 0, "files": 0, "size_bytes": 0}


# ---------------------------------------------------------------------------
# _parse_sccache_text
# ---------------------------------------------------------------------------

SCCACHE_SAMPLE = (_DATA / "sccache_sample.txt").read_text()


def test_parse_sccache_text_full():
    r = _parse_sccache_text(SCCACHE_SAMPLE)
    assert r["hits"] == 12
    assert r["misses"] == 6
    assert r["requests"] == 20
    assert r["size_str"] == "500 MiB"


def test_parse_sccache_text_subcategories_not_counted():
    # "Cache hits (C++)" and "Cache hits rate" must not corrupt the hit count
    r = _parse_sccache_text(SCCACHE_SAMPLE)
    assert r["hits"] == 12   # only the top-level "Cache hits" line


def test_parse_sccache_text_rate_lines_not_counted():
    # sccache 0.14+ emits "Cache hits rate  86.09 %" which starts with "Cache hits"
    # and has no "(". The float value must not zero out the previously parsed hit count.
    text = (
        "Cache hits                 50\n"
        "Cache hits (Rust)          50\n"
        "Cache hits rate         83.33 %\n"
        "Cache hits rate (Rust)  83.33 %\n"
        "Cache misses               10\n"
    )
    r = _parse_sccache_text(text)
    assert r["hits"] == 50
    assert r["misses"] == 10


def test_parse_sccache_text_empty():
    r = _parse_sccache_text("")
    assert r == {"hits": 0, "misses": 0, "requests": 0, "size_str": ""}


def test_parse_sccache_text_no_size():
    text = "Cache hits                 5\nCache misses               1\n"
    r = _parse_sccache_text(text)
    assert r["hits"] == 5
    assert r["size_str"] == ""


# ---------------------------------------------------------------------------
# diff_ccache / diff_sccache
# ---------------------------------------------------------------------------

def test_diff_ccache_positive_delta():
    before = {"direct_hits": 10, "preprocessed_hits": 2, "misses": 5, "files": 100, "size_bytes": 1000}
    after  = {"direct_hits": 15, "preprocessed_hits": 3, "misses": 8, "files": 120, "size_bytes": 2000}
    d = diff_ccache(before, after)
    assert d["direct_hits"] == 5
    assert d["preprocessed_hits"] == 1
    assert d["misses"] == 3
    # Post-build totals (not deltas)
    assert d["files"] == 120
    assert d["size_bytes"] == 2000


def test_diff_ccache_no_activity():
    snap = {"direct_hits": 10, "preprocessed_hits": 0, "misses": 3, "files": 50, "size_bytes": 500}
    d = diff_ccache(snap, snap)
    assert d["direct_hits"] == 0
    assert d["misses"] == 0


def test_diff_sccache_positive_delta():
    before = {"hits": 5, "misses": 2, "requests": 7, "size_str": "100 MiB"}
    after  = {"hits": 9, "misses": 4, "requests": 13, "size_str": "120 MiB"}
    d = diff_sccache(before, after)
    assert d["hits"] == 4
    assert d["misses"] == 2
    assert d["requests"] == 6
    assert d["size_str"] == "120 MiB"


def test_diff_sccache_no_activity():
    snap = {"hits": 5, "misses": 2, "requests": 7, "size_str": "100 MiB"}
    d = diff_sccache(snap, snap)
    assert d["hits"] == 0
    assert d["misses"] == 0


# ---------------------------------------------------------------------------
# _fmt_bytes
# ---------------------------------------------------------------------------

def test_fmt_bytes_bytes():
    assert _fmt_bytes(512) == "512.0 B"


def test_fmt_bytes_kib():
    assert _fmt_bytes(1024) == "1.0 KiB"


def test_fmt_bytes_mib():
    assert _fmt_bytes(1024 * 1024) == "1.0 MiB"


def test_fmt_bytes_gib():
    assert _fmt_bytes(1024 ** 3) == "1.0 GiB"


def test_fmt_bytes_fractional():
    assert _fmt_bytes(1536) == "1.5 KiB"


# ---------------------------------------------------------------------------
# probe_pacman_cache
# ---------------------------------------------------------------------------

def test_probe_pacman_cache_with_files(tmp_path):
    # Create fake pkg files
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "foo-1.0-1-x86_64.pkg.tar.zst").write_bytes(b"A" * 1024)
    (pkg_dir / "bar-2.0-1-x86_64.pkg.tar.zst").write_bytes(b"B" * 2048)
    (pkg_dir / "unrelated.txt").write_text("ignored")

    r = probe_pacman_cache(str(pkg_dir))
    assert r is not None
    assert r["count"] == 2
    assert r["size_bytes"] == 3072
    assert r["path"] == str(pkg_dir)


def test_probe_pacman_cache_empty_dir(tmp_path):
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    r = probe_pacman_cache(str(pkg_dir))
    assert r is not None
    assert r["count"] == 0
    assert r["size_bytes"] == 0


def test_probe_pacman_cache_missing_dir(tmp_path):
    r = probe_pacman_cache(str(tmp_path / "nonexistent"))
    assert r is None


def test_probe_pacman_cache_uncompressed(tmp_path):
    # PKGEXT='.pkg.tar' produces uncompressed packages; the probe must
    # count both forms alongside compressed artifacts.
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "foo-1.0-1-x86_64.pkg.tar.zst").write_bytes(b"A" * 1024)
    (pkg_dir / "bar-2.0-1-x86_64.pkg.tar").write_bytes(b"B" * 2048)

    r = probe_pacman_cache(str(pkg_dir))
    assert r is not None
    assert r["count"] == 2
    assert r["size_bytes"] == 3072


# ---------------------------------------------------------------------------
# probe_ldso_mtime
# ---------------------------------------------------------------------------

def test_probe_ldso_mtime_exists(tmp_path):
    f = tmp_path / "ld.so.cache"
    f.write_bytes(b"\x00")
    result = probe_ldso_mtime(str(f))
    assert result is not None
    # Should be a datetime-formatted string
    assert len(result) == 19  # "YYYY-MM-DD HH:MM:SS"


def test_probe_ldso_mtime_missing(tmp_path):
    result = probe_ldso_mtime(str(tmp_path / "nonexistent"))
    assert result is None


# ---------------------------------------------------------------------------
# probe_thinlto_cache
# ---------------------------------------------------------------------------

def test_probe_thinlto_cache_not_configured():
    assert probe_thinlto_cache("") is None
    assert probe_thinlto_cache("-O3 -march=native") is None


def test_probe_thinlto_cache_bare_token_not_created(tmp_path):
    nonexistent = str(tmp_path / "thinlto")
    ldflags = f"--thinlto-cache-dir={nonexistent}"
    r = probe_thinlto_cache(ldflags)
    assert r is not None
    assert r["exists"] is False
    assert r["path"] == nonexistent


def test_probe_thinlto_cache_wl_form_not_created(tmp_path):
    nonexistent = str(tmp_path / "thinlto")
    ldflags = f"-Wl,--thinlto-cache-dir={nonexistent}"
    r = probe_thinlto_cache(ldflags)
    assert r is not None
    assert r["exists"] is False


def test_probe_thinlto_cache_exists_with_files(tmp_path):
    cache_dir = tmp_path / "thinlto"
    cache_dir.mkdir()
    (cache_dir / "a.bc").write_bytes(b"X" * 4096)
    (cache_dir / "b.bc").write_bytes(b"Y" * 8192)

    ldflags = f"--thinlto-cache-dir={cache_dir}"
    r = probe_thinlto_cache(ldflags)
    assert r is not None
    assert r["exists"] is True
    assert r["size_bytes"] == 12288
    assert r["files"] == 2


def test_probe_thinlto_cache_wl_with_other_flags(tmp_path):
    cache_dir = tmp_path / "thinlto"
    cache_dir.mkdir()
    ldflags = f"-Wl,--icf=all,--thinlto-cache-dir={cache_dir},--build-id"
    r = probe_thinlto_cache(ldflags)
    assert r is not None
    assert r["exists"] is True


# ---------------------------------------------------------------------------
# emit_build_stats (log output)
# ---------------------------------------------------------------------------

def test_emit_build_stats_with_hits(capsys):
    cc = {"direct_hits": 8, "preprocessed_hits": 2, "misses": 5, "files": 100, "size_bytes": 1048576}
    sc = {"hits": 3, "misses": 1, "size_str": "200 MiB"}

    with patch("sysforge.log._VERBOSITY", 2):
        emit_build_stats("mypkg", cc, sc)

    captured = capsys.readouterr()
    assert "mypkg" in captured.err
    assert "ccache" in captured.err
    assert "sccache" in captured.err
    assert "66%" in captured.err   # 10/15 hits = 66%
    assert "75%" in captured.err   # 3/4 hits = 75%


def test_emit_build_stats_no_compilations(capsys):
    cc = {"direct_hits": 0, "preprocessed_hits": 0, "misses": 0, "files": 50, "size_bytes": 0}
    sc = {"hits": 0, "misses": 0, "size_str": "0 MiB"}

    with patch("sysforge.log._VERBOSITY", 2):
        emit_build_stats("emptypkg", cc, sc)

    captured = capsys.readouterr()
    assert "no compilations recorded" in captured.err


def test_emit_build_stats_none_skipped(capsys):
    with patch("sysforge.log._VERBOSITY", 2):
        emit_build_stats("nopkg", None, None)
    # No output expected for None deltas
    captured = capsys.readouterr()
    assert "nopkg" not in captured.err


# ---------------------------------------------------------------------------
# Session accumulation and report
# ---------------------------------------------------------------------------

def test_reset_session_clears_records():
    reset_session()
    cc = {"direct_hits": 5, "preprocessed_hits": 0, "misses": 2, "files": 10, "size_bytes": 0}
    record_build_result("pkg1", cc, None)
    reset_session()
    # After reset, report should say no data
    captured_lines = []
    with patch("sys.stderr") as mock_stderr:
        mock_stderr.write = lambda s: captured_lines.append(s)
        emit_session_report()
    assert any("No cache data" in ln for ln in captured_lines)


def test_emit_session_report_empty(capsys):
    reset_session()
    emit_session_report()
    captured = capsys.readouterr()
    assert "No cache data" in captured.err


def test_emit_session_report_with_records(capsys):
    reset_session()
    cc1 = {"direct_hits": 10, "preprocessed_hits": 0, "misses": 5, "files": 100, "size_bytes": 1048576}
    cc2 = {"direct_hits": 20, "preprocessed_hits": 5, "misses": 3, "files": 200, "size_bytes": 2097152}
    record_build_result("pkg-a", cc1, None)
    record_build_result("pkg-b", cc2, None)

    emit_session_report()
    captured = capsys.readouterr()

    assert "pkg-a" in captured.err
    assert "pkg-b" in captured.err
    # Total: 35 hits / 43 total = 81%
    assert "ccache total" in captured.err
    assert "35/43" in captured.err


def test_emit_session_report_sccache_totals(capsys):
    reset_session()
    sc = {"hits": 8, "misses": 2, "size_str": "300 MiB"}
    record_build_result("mypkg", None, sc)

    emit_session_report()
    captured = capsys.readouterr()

    assert "sccache" in captured.err
    assert "8/10" in captured.err
    assert "80%" in captured.err


def test_emit_session_report_no_cache_installed(capsys):
    reset_session()
    record_build_result("nopkg", None, None)

    emit_session_report()
    captured = capsys.readouterr()
    assert "not installed" in captured.err


# ---------------------------------------------------------------------------
# emit_system_probes (integration-style, mocked probes)
# ---------------------------------------------------------------------------

def test_emit_system_probes_emits_info(capsys, tmp_path):
    # Create a fake ld.so.cache and pacman cache dir
    ldso = tmp_path / "ld.so.cache"
    ldso.write_bytes(b"\x00")
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "foo-1.0-1-x86_64.pkg.tar.zst").write_bytes(b"A" * 512)

    with patch("sysforge.primitives.cache_probe.probe_ldso_mtime", return_value="2026-01-01 12:00:00"), \
         patch("sysforge.primitives.cache_probe.probe_pacman_cache",
               return_value={"count": 1, "size_bytes": 512, "path": str(pkg_dir)}), \
         patch("sysforge.log._VERBOSITY", 2):
        emit_system_probes()

    captured = capsys.readouterr()
    assert "ld.so.cache" in captured.err
    assert "pacman cache" in captured.err


def test_emit_system_probes_thinlto(capsys, tmp_path):
    cache_dir = tmp_path / "thinlto"
    cache_dir.mkdir()
    (cache_dir / "obj.bc").write_bytes(b"X" * 4096)
    ldflags = f"--thinlto-cache-dir={cache_dir}"

    with patch("sysforge.primitives.cache_probe.probe_ldso_mtime", return_value=None), \
         patch("sysforge.primitives.cache_probe.probe_pacman_cache", return_value=None), \
         patch("sysforge.log._VERBOSITY", 2):
        emit_system_probes(ldflags)

    captured = capsys.readouterr()
    assert "ThinLTO" in captured.err
