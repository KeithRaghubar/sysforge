"""
test_converge.py — unit tests for sysforge.converge and related drift-detection
primitives.

Covers:
  - serialize_flags (profile.py)
  - flags_string in build_state record/reload
  - _diff_flags (converge.py)
  - cmd_converge: IN_SYNC, DRIFTED, NO_FLAGS, NO_PKGBUILD, PACMAN_ONLY
  - --apply path (makepkg_wrapper called for DRIFTED packages)
"""
import tomllib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.primitives.profile import serialize_flags, SYSFORGE_KEYS
from sysforge.primitives.build_state import BuildState
from sysforge.converge import _diff_flags, cmd_converge


# ---------------------------------------------------------------------------
# serialize_flags
# ---------------------------------------------------------------------------

def test_serialize_flags_sorts_keys():
    profile = {"CXXFLAGS": "-O3", "CFLAGS": "-O2", "LDFLAGS": "-Wl,-O1"}
    result = serialize_flags(profile)
    lines = result.splitlines()
    assert lines == ["CFLAGS=-O2", "CXXFLAGS=-O3", "LDFLAGS=-Wl,-O1"]


def test_serialize_flags_excludes_sysforge_keys():
    profile = {"CFLAGS": "-O3", "build_mode": "kernel", "batch": True, "makepkg_flags": []}
    result = serialize_flags(profile)
    assert "build_mode" not in result
    assert "batch" not in result
    assert "makepkg_flags" not in result
    assert "CFLAGS=-O3" in result


def test_serialize_flags_list_values_joined():
    profile = {"makepkg_flags": ["--noconfirm", "--noprogressbar"]}
    result = serialize_flags(profile)
    # makepkg_flags is a sysforge key, so it's excluded
    assert result == ""


def test_serialize_flags_non_sysforge_list():
    profile = {"CFLAGS": "-O3", "custom_list": ["a", "b", "c"]}
    result = serialize_flags(profile)
    assert "custom_list=a b c" in result


def test_serialize_flags_empty_profile():
    assert serialize_flags({}) == ""


def test_serialize_flags_only_sysforge_keys():
    profile = {k: "x" for k in SYSFORGE_KEYS}
    assert serialize_flags(profile) == ""


# ---------------------------------------------------------------------------
# flags_string in BuildState
# ---------------------------------------------------------------------------

def test_build_state_records_flags_string(tmp_path):
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nCXXFLAGS=-O3\nLDFLAGS=-Wl,-O1"
    bs.record(pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    entry = bs.get("htop")
    assert entry["flags_string"] == fs


def test_build_state_flags_string_persisted(tmp_path):
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nLDFLAGS=-Wl,-O1"
    bs.record(pkgname="htop", pkgver="3.4.1", pkgrel="1", epoch="0",
              pkgbase="htop", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    bs.save()

    bs2 = BuildState(tmp_path)
    assert bs2.get("htop")["flags_string"] == fs


def test_build_state_flags_string_valid_toml(tmp_path):
    """flags_string with newlines must serialize to valid TOML."""
    bs = BuildState(tmp_path)
    fs = "CFLAGS=-O3\nCXXFLAGS=-O3"
    bs.record(pkgname="mesa", pkgver="24.0.0", pkgrel="1", epoch="0",
              pkgbase="mesa", pkgbuild_dir=tmp_path,
              build_mode="profiled", flags_string=fs)
    bs.save()
    with open(bs.path, "rb") as f:
        data = tomllib.load(f)
    assert data["mesa"]["flags_string"] == fs


# ---------------------------------------------------------------------------
# _diff_flags
# ---------------------------------------------------------------------------

def test_diff_flags_identical_returns_empty():
    s = "CFLAGS=-O3\nCXXFLAGS=-O3"
    assert _diff_flags(s, s) == []


def test_diff_flags_changed_value():
    old = "CFLAGS=-O2\nLDFLAGS=-Wl,-O1"
    new = "CFLAGS=-O3\nLDFLAGS=-Wl,-O1"
    diffs = _diff_flags(old, new)
    assert len(diffs) == 1
    assert "CFLAGS" in diffs[0]
    assert "-O2" in diffs[0]
    assert "-O3" in diffs[0]


def test_diff_flags_added_key():
    old = "CFLAGS=-O3"
    new = "CFLAGS=-O3\nCXXFLAGS=-O3"
    diffs = _diff_flags(old, new)
    assert any("+CXXFLAGS" in d for d in diffs)


def test_diff_flags_removed_key():
    old = "CFLAGS=-O3\nCXXFLAGS=-O3"
    new = "CFLAGS=-O3"
    diffs = _diff_flags(old, new)
    assert any("-CXXFLAGS" in d for d in diffs)


def test_diff_flags_empty_strings():
    assert _diff_flags("", "") == []


# ---------------------------------------------------------------------------
# cmd_converge — test fixtures
# ---------------------------------------------------------------------------

def _make_state(tmp_path, entries: dict) -> BuildState:
    """Create a BuildState and populate it with the given pkgname→entry dicts."""
    bs = BuildState(tmp_path / "state")
    for pkgname, entry in entries.items():
        bs.record(
            pkgname=pkgname,
            pkgver=entry.get("pkgver", "1.0"),
            pkgrel=entry.get("pkgrel", "1"),
            epoch=entry.get("epoch", "0"),
            pkgbase=entry.get("pkgbase", pkgname),
            pkgbuild_dir=Path(entry.get("pkgbuild_dir", str(tmp_path / pkgname))),
            build_mode=entry.get("build_mode"),
            flags_string=entry.get("flags_string"),
        )
    bs.save()
    return bs


def _make_pkgbuild(tmp_path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    pb = d / "PKGBUILD"
    pb.write_text(f"pkgname={name}\npkgver=1.0\npkgrel=1\n")
    return pb


def _make_args(tmp_path, apply=False, state_dir=None, profile_conf=None, pkgnames=None):
    class Args:
        pass
    a = Args()
    a.apply = apply
    a.state_dir = str(state_dir or tmp_path / "state")
    a.profile_conf = str(profile_conf) if profile_conf else None
    a.no_pkg_log = True
    a.persist_log = False
    a.log_dir = None
    a.cache_report = False
    a.pkgnames = list(pkgnames) if pkgnames else []
    return a


_MINIMAL_CONFIG = {
    "rules": [],
    "defaults": {"profile": "bare"},
    "profiles": {
        "bare": {"CFLAGS": "-O2"},
    },
    "conflict_groups": {},
}

_BARE_FLAGS = serialize_flags({"CFLAGS": "-O2"})


# ---------------------------------------------------------------------------
# cmd_converge — status cases
# ---------------------------------------------------------------------------

def test_converge_in_sync(tmp_path, capsys):
    pb = _make_pkgbuild(tmp_path, "htop")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        }
    })
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "IN_SYNC" in out
    assert "htop" in out


def test_converge_drifted(tmp_path, capsys):
    pb = _make_pkgbuild(tmp_path, "mesa")
    _make_state(tmp_path, {
        "mesa": {
            "pkgbase": "mesa",
            "pkgbuild_dir": str(tmp_path / "mesa"),
            "build_mode": "profiled",
            "flags_string": "CFLAGS=-O2",  # old flags
        }
    })
    args = _make_args(tmp_path)

    # Config now has a different CFLAGS
    config = {
        "rules": [],
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CFLAGS": "-O3 -march=native"}},
        "conflict_groups": {},
    }

    with patch("sysforge.converge.load_config", return_value=config), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "DRIFTED" in out
    assert "mesa" in out
    assert "CFLAGS" in out


def test_converge_no_flags(tmp_path, capsys):
    pb = _make_pkgbuild(tmp_path, "htop")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            # no flags_string
        }
    })
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "NO_FLAGS" in out


def test_converge_no_pkgbuild(tmp_path, capsys):
    # Don't create the PKGBUILD file
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        }
    })
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "NO_PKGBUILD" in out


def test_converge_pacman_only_omitted(tmp_path, capsys):
    """Packages with build_mode != 'profiled' are silently omitted from output."""
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            # no build_mode (pacman-installed)
        }
    })
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "htop" not in out
    assert "PACMAN_ONLY" not in out


def test_converge_empty_state(tmp_path, capsys):
    (tmp_path / "state").mkdir()
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    err = capsys.readouterr().err
    assert "No packages" in err


def test_converge_pkgname_filter_limits_scope(tmp_path, capsys):
    _make_pkgbuild(tmp_path, "htop")
    _make_pkgbuild(tmp_path, "mesa")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        },
        "mesa": {
            "pkgbase": "mesa",
            "pkgbuild_dir": str(tmp_path / "mesa"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        },
    })
    args = _make_args(tmp_path, pkgnames=["mesa"])

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "mesa" in out
    assert "htop" not in out


def test_converge_pkgname_filter_unknown_warns_and_skips(tmp_path, capsys):
    _make_pkgbuild(tmp_path, "htop")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        },
    })
    args = _make_args(tmp_path, pkgnames=["nonexistent"])

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    captured = capsys.readouterr()
    assert "No matching packages" in captured.err


def test_converge_apply_rebuilds_drifted(tmp_path, capsys):
    pb = _make_pkgbuild(tmp_path, "mesa")
    _make_state(tmp_path, {
        "mesa": {
            "pkgbase": "mesa",
            "pkgbuild_dir": str(tmp_path / "mesa"),
            "build_mode": "profiled",
            "flags_string": "CFLAGS=-O2",
        }
    })
    args = _make_args(tmp_path, apply=True)

    config = {
        "rules": [],
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CFLAGS": "-O3"}},
        "conflict_groups": {},
    }

    built = []
    def fake_build(path, **kwargs):
        built.append(str(path))

    with patch("sysforge.converge.load_config", return_value=config), \
         patch("sysforge.converge.load_conflict_groups", return_value={}), \
         patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build):
        cmd_converge(args)

    assert len(built) == 1
    assert "mesa" in built[0]


def test_converge_apply_skips_in_sync(tmp_path, capsys):
    pb = _make_pkgbuild(tmp_path, "htop")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop",
            "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled",
            "flags_string": _BARE_FLAGS,
        }
    })
    args = _make_args(tmp_path, apply=True)

    built = []
    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}), \
         patch("sysforge.primitives.makepkg_wrapper.run", side_effect=lambda p, **kw: built.append(p)):
        cmd_converge(args)

    assert built == []


def test_converge_summary_counts(tmp_path, capsys):
    """Summary line includes correct drifted/in-sync counts."""
    pb_htop  = _make_pkgbuild(tmp_path, "htop")
    pb_mesa  = _make_pkgbuild(tmp_path, "mesa")
    _make_state(tmp_path, {
        "htop": {
            "pkgbase": "htop", "pkgbuild_dir": str(tmp_path / "htop"),
            "build_mode": "profiled", "flags_string": _BARE_FLAGS,
        },
        "mesa": {
            "pkgbase": "mesa", "pkgbuild_dir": str(tmp_path / "mesa"),
            "build_mode": "profiled", "flags_string": "CFLAGS=-O99",  # drifted
        },
    })
    args = _make_args(tmp_path)

    with patch("sysforge.converge.load_config", return_value=_MINIMAL_CONFIG), \
         patch("sysforge.converge.load_conflict_groups", return_value={}):
        cmd_converge(args)

    out = capsys.readouterr().out
    assert "In sync: 1" in out
    assert "Drifted: 1" in out
