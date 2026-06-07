"""
test_flag_drift.py — unit tests for primitives/flag_drift.py.

flag_drift is the shared engine for flag-drift detection used by both
`sysforge update` (Phase 4.3) and the deprecated `sysforge converge` verb.
These tests drive the primitive directly against real PKGBUILD parsing +
profile resolution; only the deliberately-unparseable case fakes parse_pkgbuild.
"""

from sysforge.primitives.profile import serialize_flags
from sysforge.primitives.flag_drift import (
    STATUS_DRIFTED,
    STATUS_IN_SYNC,
    STATUS_NO_FLAGS,
    STATUS_NO_PKGBUILD,
    STATUS_NOT_PROFILED,
    STATUS_PARSE_ERROR,
    diff_flags,
    resolve_flag_drift,
)


_MINIMAL_CONFIG = {
    "rules": [],
    "defaults": {"profile": "bare"},
    "profiles": {"bare": {"CFLAGS": "-O2"}},
    "conflict_groups": {},
}
_BARE_FLAGS = serialize_flags({"CFLAGS": "-O2"})


def _pkgbuild(tmp_path, name="htop"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "PKGBUILD").write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")
    return d


# ---------------------------------------------------------------------------
# diff_flags
# ---------------------------------------------------------------------------

def test_diff_flags_identical_is_empty():
    s = "CFLAGS=-O2\nLDFLAGS=-fuse-ld=lld"
    assert diff_flags(s, s) == []


def test_diff_flags_reports_change_add_remove():
    old = "CFLAGS=-O2\nLDFLAGS=-x"
    new = "CFLAGS=-O3\nCXXFLAGS=-O3"
    diffs = diff_flags(old, new)
    joined = "\n".join(diffs)
    assert "CFLAGS" in joined and "-O2" in joined and "-O3" in joined  # changed
    assert "+CXXFLAGS" in joined                                       # added
    assert "-LDFLAGS" in joined                                        # removed


# ---------------------------------------------------------------------------
# resolve_flag_drift — status cases
# ---------------------------------------------------------------------------

def test_not_profiled_short_circuits(tmp_path):
    entry = {"build_mode": "pacman", "pkgbuild_dir": str(tmp_path / "htop")}
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_NOT_PROFILED
    assert not r.drifted


def test_no_pkgbuild(tmp_path):
    # build_mode profiled but the recorded dir has no PKGBUILD on disk.
    entry = {
        "build_mode": "profiled",
        "pkgbuild_dir": str(tmp_path / "ghost"),
        "flags_string": _BARE_FLAGS,
    }
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_NO_PKGBUILD
    assert r.pkgbuild_path == tmp_path / "ghost" / "PKGBUILD"


def test_no_flags(tmp_path):
    d = _pkgbuild(tmp_path, "htop")
    entry = {"build_mode": "profiled", "pkgbuild_dir": str(d)}  # no flags_string
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_NO_FLAGS


def test_in_sync(tmp_path):
    d = _pkgbuild(tmp_path, "htop")
    entry = {
        "build_mode": "profiled",
        "pkgbuild_dir": str(d),
        "flags_string": _BARE_FLAGS,  # exactly what `bare` resolves to
    }
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_IN_SYNC
    assert r.diffs == []


def test_drifted(tmp_path):
    d = _pkgbuild(tmp_path, "mesa")
    entry = {
        "build_mode": "profiled",
        "pkgbuild_dir": str(d),
        "flags_string": "CFLAGS=-this-is-stale",  # != resolved `bare`
    }
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_DRIFTED
    assert r.drifted
    assert r.diffs  # at least one per-key diff line
    assert any("CFLAGS" in line for line in r.diffs)


def test_parse_error_is_reported_not_raised(tmp_path, monkeypatch):
    d = _pkgbuild(tmp_path, "htop")

    def _boom(*a, **k):
        raise ValueError("malformed PKGBUILD")

    monkeypatch.setattr("sysforge.primitives.flag_drift.parse_pkgbuild", _boom)
    entry = {
        "build_mode": "profiled",
        "pkgbuild_dir": str(d),
        "flags_string": _BARE_FLAGS,
    }
    r = resolve_flag_drift(entry, _MINIMAL_CONFIG, {})
    assert r.status == STATUS_PARSE_ERROR
    assert "malformed" in (r.error or "")
