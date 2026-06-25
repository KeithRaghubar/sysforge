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
