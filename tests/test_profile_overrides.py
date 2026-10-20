import pytest
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
    assert p.read_text().count('"htop" =') == 1


def test_writer_returns_false_on_missing_parent(tmp_path):
    p = tmp_path / "does-not-exist" / "profiles.toml"
    assert write_package_compiler_override(p, "htop", "gcc", "g++", "bfd") is False


def test_writer_escalates_a_root_owned_target(tmp_path, monkeypatch):
    """3.5.0-B113: the default layout's target is root's /etc file, so the
    write goes through the config-replace seam's privilege escalation."""
    from sysforge.primitives import atomic_write
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML)
    calls = []

    def refuse(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(atomic_write, "_replace_direct", refuse)
    monkeypatch.setattr(atomic_write, "run_privileged",
                        lambda argv, **k: calls.append(argv))
    assert write_package_compiler_override(p, "htop", "gcc", "g++", "bfd") is True
    assert [c[0] for c in calls] == ["install", "mv"]
    assert calls[1][-1] == str(p)


def test_writer_failed_escalation_is_an_error_not_a_quiet_warning(
        tmp_path, monkeypatch, capsys):
    from sysforge.primitives import atomic_write
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML)

    def refuse(*a, **k):
        raise PermissionError(13, "denied")

    def sudo_declined(argv, **k):
        raise RuntimeError("sudo declined")

    monkeypatch.setattr(atomic_write, "_replace_direct", refuse)
    monkeypatch.setattr(atomic_write, "run_privileged", sudo_declined)
    assert write_package_compiler_override(p, "htop", "gcc", "g++", "bfd") is False
    assert p.read_text() == _BASE_TOML
    err = capsys.readouterr()
    assert "Could not save the compiler swap for htop" in err.out + err.err


def test_writer_keeps_a_symlink_and_the_mode(tmp_path):
    real = tmp_path / "real-profiles.toml"
    real.write_text(_BASE_TOML)
    real.chmod(0o640)
    link = tmp_path / "profiles.toml"
    link.symlink_to(real)
    assert write_package_compiler_override(link, "htop", "gcc", "g++", "bfd") is True
    assert link.is_symlink()
    assert "htop" in real.read_text()
    assert real.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("pkgbase, cc", [
    ("libstdc++", "gcc"),                 # `+` is invalid in a bare key
    ("python-ruamel.yaml", "gcc"),       # `.` made a bare key a nested table
    ("htop", 'gcc "quoted"'),            # a `"` typed into the manual entry
    ("odd\\name", "back\\slash"),
])
def test_writer_round_trips_names_and_values_toml_cannot_take_bare(
        tmp_path, pkgbase, cc):
    """3.5.0-B45: the key is quoted and the values escaped, so the file stays
    valid TOML and the override is found under the same pkgbase."""
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML)
    assert write_package_compiler_override(p, pkgbase, cc, "g++", "bfd") is True
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"][pkgbase] == {
        "cc": cc, "cxx": "g++", "ld": "bfd"}
    prof = resolve_profile(_pkgmeta(pkgbase), [], {
        "profiles": data["profiles"], "defaults": data["defaults"],
        "package_compiler_overrides": data["package_compiler_overrides"],
    })
    assert prof["CC"] == cc
    # A second write replaces the row rather than adding one.
    write_package_compiler_override(p, pkgbase, "clang", "clang++", "lld")
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"][pkgbase]["cc"] == "clang"
    assert len(data["package_compiler_overrides"]) == 1


def test_writer_replaces_a_legacy_bare_key_row(tmp_path):
    """A row an older version wrote with a bare key is replaced, not duplicated."""
    p = tmp_path / "profiles.toml"
    p.write_text(_BASE_TOML + "\n[package_compiler_overrides]\n"
                 'htop = { cc = "gcc", cxx = "g++", ld = "bfd" }\n')
    write_package_compiler_override(p, "htop", "clang", "clang++", "lld")
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"] == {
        "htop": {"cc": "clang", "cxx": "clang++", "ld": "lld"}}


# ---------------------------------------------------------------------------
# 3.5.0-F12: a row's `toolchain` swaps the whole bundle
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from sysforge.primitives.makepkg_conf import emit_makepkg_conf  # noqa: E402
from sysforge.primitives.profile import (  # noqa: E402
    swap_toolchain,
    toolchain_for_compilers,
)

_LLVM_PROFILE = {
    "toolchain": "llvm",
    "CFLAGS": "-march=native -O3 -pipe -flto=thin",
    "CXXFLAGS": "-march=native -O3 -pipe -flto=thin",
    "LDFLAGS": "-fuse-ld=lld -Wl,-O1,--icf=all -flto=thin",
}


def _llvm_cfg(row, toolchain_default="llvm"):
    return {
        "defaults": {"profile": "fast", "toolchain": toolchain_default},
        "profiles": {"fast": dict(_LLVM_PROFILE)},
        "package_compiler_overrides": {"htop": row},
    }


def _emitted(prof) -> str:
    with emit_makepkg_conf(prof, system_conf_path="/nonexistent") as conf:
        return Path(conf).read_text()


def test_toolchain_gcc_row_on_llvm_profile_yields_gcc_bundle():
    prof = resolve_profile(_pkgmeta("htop"), [], _llvm_cfg({"toolchain": "gcc"}))
    assert (prof["CC"], prof["CXX"]) == ("gcc", "g++")
    assert prof["toolchain"] == "gcc"
    for key in ("AR", "NM", "RANLIB", "STRIP"):
        assert key not in prof, key  # system binutils, not llvm-*
    assert "-fuse-ld=" not in prof["LDFLAGS"]
    conf = _emitted(prof)
    assert "llvm-" not in conf
    assert "-flto=thin" not in conf
    assert "--icf=all" not in conf  # lld-only, dropped with lld


def test_raw_row_still_replaces_only_cc_cxx_ld():
    """A raw cc/cxx/ld row behaves as before 3.5.0-F12: the bundle stays."""
    prof = resolve_profile(_pkgmeta("htop"), [],
                           _llvm_cfg({"cc": "gcc", "cxx": "g++", "ld": "bfd"}))
    assert (prof["CC"], prof["CXX"]) == ("gcc", "g++")
    assert prof["AR"] == "llvm-ar"
    assert "-fuse-ld=bfd" in prof["LDFLAGS"]


def test_toolchain_llvm_row_on_gcc_profile_yields_llvm_bundle():
    cfg = {
        "defaults": {"profile": "plain", "toolchain": "gcc"},
        "profiles": {"plain": {"CFLAGS": "-O2", "LDFLAGS": "-Wl,-O1"}},
        "package_compiler_overrides": {"htop": {"toolchain": "llvm"}},
    }
    prof = resolve_profile(_pkgmeta("htop"), [], cfg)
    assert (prof["CC"], prof["CXX"], prof["AR"]) == ("clang", "clang++", "llvm-ar")
    assert "-fuse-ld=lld" in prof["LDFLAGS"]


def test_toolchain_row_overrides_explicit_profile_compiler_keys():
    """Explicit CC/AR in the profile beat the bundle, but not a toolchain row."""
    cfg = _llvm_cfg({"toolchain": "gcc"})
    cfg["profiles"]["fast"].update(CC="clang-23", AR="llvm-ar")
    prof = resolve_profile(_pkgmeta("htop"), [], cfg)
    assert prof["CC"] == "gcc" and "AR" not in prof


def test_toolchain_row_with_ld_applies_the_linker_last():
    prof = resolve_profile(_pkgmeta("htop"), [],
                           _llvm_cfg({"toolchain": "gcc", "ld": "mold"}))
    assert prof["CC"] == "gcc"
    assert prof["LDFLAGS"].split().count("-fuse-ld=mold") == 1


def test_unknown_toolchain_row_is_ignored():
    prof = resolve_profile(_pkgmeta("htop"), [], _llvm_cfg({"toolchain": "icc"}))
    assert prof["CC"] == "clang" and prof["AR"] == "llvm-ar"


def test_swap_toolchain_is_in_place_and_idempotent():
    prof = dict(_LLVM_PROFILE, CC="clang", AR="llvm-ar")
    assert swap_toolchain(prof, "gcc") is prof
    once = dict(prof)
    assert swap_toolchain(prof, "gcc") == once


def test_toolchain_for_compilers():
    assert toolchain_for_compilers("gcc", "g++") == "gcc"
    assert toolchain_for_compilers("clang", "clang++") == "llvm"
    assert toolchain_for_compilers("gcc", "clang++") is None
    assert toolchain_for_compilers("/opt/cc", "/opt/c++") is None


def test_writer_toolchain_row_round_trips(tmp_path):
    p = tmp_path / "profiles.toml"
    p.write_text("[defaults]\nprofile = \"standard\"\n")
    assert write_package_compiler_override(
        p, "htop", "gcc", "g++", "", toolchain="gcc") is True
    data = tomllib.loads(p.read_text())
    assert data["package_compiler_overrides"]["htop"] == {"toolchain": "gcc"}
    # Upserting over a raw row replaces it with the toolchain form (+ ld).
    write_package_compiler_override(p, "vim", "gcc", "g++", "bfd")
    write_package_compiler_override(p, "vim", "gcc", "g++", "bfd", toolchain="gcc")
    rows = tomllib.loads(p.read_text())["package_compiler_overrides"]
    assert rows["vim"] == {"toolchain": "gcc", "ld": "bfd"}
