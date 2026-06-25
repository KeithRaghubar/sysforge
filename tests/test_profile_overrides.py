from sysforge.primitives.profile import resolve_profile


def _pkgmeta(pkgbase):
    return {"globals": {"pkgbase": pkgbase, "pkgname": pkgbase}}


def _config_with_override(cc, cxx, ld):
    # A bare profile defaulting to clang, plus an override row for "htop".
    return {
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CC": "clang", "CXX": "clang++"}},
        "package_compiler_overrides": {
            "htop": {"cc": cc, "cxx": cxx, "ld": ld},
        },
    }


def test_override_wins_over_matched_profile_gcc():
    cfg = _config_with_override("gcc", "g++", "bfd")
    prof = resolve_profile(_pkgmeta("htop"), [], cfg)
    assert prof["CC"] == "gcc"
    assert prof["CXX"] == "g++"
    assert "-fuse-ld=bfd" in prof.get("LDFLAGS", "")


def test_override_wins_over_matched_profile_llvm():
    cfg = _config_with_override("clang", "clang++", "lld")
    # Start from a gcc bare profile so the override must flip it to clang/lld.
    cfg["profiles"]["bare"] = {"CC": "gcc", "CXX": "g++"}
    prof = resolve_profile(_pkgmeta("htop"), [], cfg)
    assert prof["CC"] == "clang"
    assert prof["CXX"] == "clang++"
    assert "-fuse-ld=lld" in prof.get("LDFLAGS", "")


def test_no_override_for_unlisted_pkgbase():
    cfg = _config_with_override("gcc", "g++", "bfd")
    prof = resolve_profile(_pkgmeta("vim"), [], cfg)
    assert prof["CC"] == "clang"  # untouched


def test_override_keys_off_pkgbase_not_pkgname():
    # Split package: pkgbase differs from pkgname. The override row keyed on
    # the pkgbase must apply; a row keyed on the pkgname must NOT.
    pkgmeta = {"globals": {"pkgbase": "ffmpeg", "pkgname": "ffmpeg-libs"}}

    cfg_pkgbase = {
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CC": "clang", "CXX": "clang++"}},
        "package_compiler_overrides": {
            "ffmpeg": {"cc": "gcc", "cxx": "g++", "ld": "bfd"},
        },
    }
    prof = resolve_profile(pkgmeta, [], cfg_pkgbase)
    assert prof["CC"] == "gcc"
    assert prof["CXX"] == "g++"
    assert "-fuse-ld=bfd" in prof.get("LDFLAGS", "")

    cfg_pkgname = {
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CC": "clang", "CXX": "clang++"}},
        "package_compiler_overrides": {
            "ffmpeg-libs": {"cc": "gcc", "cxx": "g++", "ld": "bfd"},
        },
    }
    prof = resolve_profile(pkgmeta, [], cfg_pkgname)
    assert prof["CC"] == "clang"  # pkgname-keyed row must not apply


def test_explicit_none_table_is_none_safe():
    # An explicit None table must not crash the resolution.
    cfg = {
        "defaults": {"profile": "bare"},
        "profiles": {"bare": {"CC": "clang", "CXX": "clang++"}},
        "package_compiler_overrides": None,
    }
    prof = resolve_profile(_pkgmeta("htop"), [], cfg)
    assert prof["CC"] == "clang"


import tomllib
from sysforge.profile_writer import write_package_compiler_override


_BASE_TOML = """\
# profiles.toml
[defaults]
profile = "bare"

[profiles.bare]
CC = "clang"
"""


def test_writer_creates_section_and_row(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML)
    assert write_package_compiler_override(p, "htop", "gcc", "g++", "bfd") is True
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"]["htop"] == {
        "cc": "gcc", "cxx": "g++", "ld": "bfd"
    }
    # Existing content preserved.
    assert data["profiles"]["bare"]["CC"] == "clang"
    assert "# profiles.toml" in p.read_text()


def test_writer_upserts_existing_row(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML)
    write_package_compiler_override(p, "htop", "gcc", "g++", "bfd")
    # Second write with new values replaces, not duplicates.
    write_package_compiler_override(p, "htop", "clang", "clang++", "lld")
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"]["htop"]["cc"] == "clang"
    # Exactly one htop row.
    assert p.read_text().count("htop =") == 1


def test_writer_returns_false_on_missing_parent(tmp_path):
    p = tmp_path / "does-not-exist" / "profiles.toml"
    assert write_package_compiler_override(p, "htop", "gcc", "g++", "bfd") is False
