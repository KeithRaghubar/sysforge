"""
test_hardening_preservation.py — Arch's compiler hardening baseline survives a
wholesale profile CFLAGS override (3.1.0-B8).

Two layers: the pure token merge (profile.merge_preserved_tokens, profile-wins
precedence) and the conf-emission seam that applies it (emit_makepkg_conf,
reading [preserved_system_tokens] from profiles.toml).
"""
from pathlib import Path

import pytest

from sysforge.primitives.config import load_preserved_system_tokens
from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf
from sysforge.primitives.profile import merge_preserved_tokens

_FIXTURE_CONF = Path(__file__).parent / "data" / "etc" / "sysforge" / "system_makepkg.conf"

CONFLICT_GROUPS = {
    "pic": ["-fPIC", "-fPIE", "-fpic", "-fpie", "-fno-pic", "-fno-pie"],
    "lto": ["-flto", "-flto=thin", "-flto=full", "-fno-lto"],
    "stack": ["-fstack-protector", "-fstack-protector-strong", "-fno-stack-protector"],
}

HARDENING = [
    "-fexceptions",
    "-Wp,-D_FORTIFY_SOURCE=3",
    "-Wformat",
    "-Werror=format-security",
    "-fstack-protector-strong",
]

SYSTEM_CFLAGS = (
    "-march=x86-64 -mtune=generic -O2 -pipe -fno-plt -fexceptions "
    "-Wp,-D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security "
    "-fstack-protector-strong"
)


@pytest.fixture
def sys_conf_path(tmp_path):
    p = tmp_path / "makepkg.conf"
    p.write_text(_FIXTURE_CONF.read_text())
    return p


def read_conf(conf_path):
    out = {}
    for line in Path(conf_path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# merge_preserved_tokens — pure
# ---------------------------------------------------------------------------

def test_restores_hardening_dropped_by_override():
    merged, restored = merge_preserved_tokens(
        "-march=native -O2 -pipe", SYSTEM_CFLAGS, HARDENING,
        conflict_groups=CONFLICT_GROUPS,
    )
    assert restored == [
        "-fexceptions", "-Wp,-D_FORTIFY_SOURCE=3", "-Wformat",
        "-Werror=format-security", "-fstack-protector-strong",
    ]
    # The profile's own tokens stay first and unmodified.
    assert merged.startswith("-march=native -O2 -pipe ")
    # Optimisation tokens from the system value are never dragged along.
    assert "-mtune=generic" not in merged
    assert "-march=x86-64" not in merged


def test_conflicting_profile_token_is_an_explicit_optout():
    merged, restored = merge_preserved_tokens(
        "-march=native -O2 -fno-stack-protector", SYSTEM_CFLAGS, HARDENING,
        conflict_groups=CONFLICT_GROUPS,
    )
    assert "-fstack-protector-strong" not in merged
    assert "-fno-stack-protector" in merged
    assert "-fstack-protector-strong" not in restored
    # The rest of the set is unaffected by opting out of one group.
    assert "-Wp,-D_FORTIFY_SOURCE=3" in merged


def test_profile_wins_within_a_prefix_family():
    merged, restored = merge_preserved_tokens(
        "-march=native -Wp,-D_FORTIFY_SOURCE=2", SYSTEM_CFLAGS, HARDENING,
        conflict_groups=CONFLICT_GROUPS,
    )
    assert "-Wp,-D_FORTIFY_SOURCE=2" in merged
    assert "-Wp,-D_FORTIFY_SOURCE=3" not in merged


def test_token_already_present_is_not_duplicated():
    merged, _ = merge_preserved_tokens(
        "-march=native -Wformat", SYSTEM_CFLAGS, HARDENING,
        conflict_groups=CONFLICT_GROUPS,
    )
    assert merged.split().count("-Wformat") == 1


def test_shell_reference_value_is_left_alone():
    merged, restored = merge_preserved_tokens(
        "$CFLAGS", SYSTEM_CFLAGS, HARDENING, conflict_groups=CONFLICT_GROUPS,
    )
    assert merged == "$CFLAGS"
    assert restored == []


def test_never_invents_a_flag_the_system_conf_lacks():
    merged, restored = merge_preserved_tokens(
        "-march=native -O2", "-march=x86-64 -O2 -pipe", HARDENING,
        conflict_groups=CONFLICT_GROUPS,
    )
    assert restored == []
    assert merged == "-march=native -O2"


def test_empty_preserve_set_is_a_noop():
    merged, restored = merge_preserved_tokens(
        "-march=native", SYSTEM_CFLAGS, [], conflict_groups=CONFLICT_GROUPS,
    )
    assert (merged, restored) == ("-march=native", [])


# ---------------------------------------------------------------------------
# Shipped declaration
# ---------------------------------------------------------------------------

def test_shipped_config_declares_the_hardening_set():
    tokens = load_preserved_system_tokens()
    for key in ("CFLAGS", "CXXFLAGS"):
        assert key in tokens, f"{key} missing from [preserved_system_tokens]"
        for flag in ("-Wp,-D_FORTIFY_SOURCE=3", "-fstack-protector-strong",
                     "-Werror=format-security"):
            assert flag in tokens[key]


# ---------------------------------------------------------------------------
# emit_makepkg_conf seam
# ---------------------------------------------------------------------------

def test_emitted_conf_keeps_hardening_under_a_profile_override(sys_conf_path):
    profile = {"CFLAGS": "-march=native -O2 -pipe", "CXXFLAGS": "$CFLAGS"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf:
        vals = read_conf(conf)
    assert "-march=native" in vals["CFLAGS"]
    assert "-Wp,-D_FORTIFY_SOURCE=3" in vals["CFLAGS"]
    assert "-fstack-protector-strong" in vals["CFLAGS"]
    assert "-Werror=format-security" in vals["CFLAGS"]
    # CXXFLAGS inherits by shell expansion, not by injection.
    assert vals["CXXFLAGS"] == "$CFLAGS"


def test_emitted_conf_honours_profile_optout_key(sys_conf_path):
    profile = {
        "CFLAGS": "-march=native -O2 -pipe",
        "preserve_system_tokens": False,
    }
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf:
        vals = read_conf(conf)
    assert vals["CFLAGS"] == "-march=native -O2 -pipe"


def test_emitted_conf_honours_per_token_optout(sys_conf_path):
    profile = {"CFLAGS": "-march=native -O2 -fno-stack-protector"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path) as conf:
        vals = read_conf(conf)
    assert "-fstack-protector-strong" not in vals["CFLAGS"]
    assert "-Wp,-D_FORTIFY_SOURCE=3" in vals["CFLAGS"]


def test_profile_without_cflags_still_inherits_system_value(sys_conf_path):
    with emit_makepkg_conf({"MAKEFLAGS": "-j4"}, system_conf_path=sys_conf_path) as conf:
        vals = read_conf(conf)
    assert "-fstack-protector-strong" in vals["CFLAGS"]
    assert "-march=x86-64" in vals["CFLAGS"]


def test_kernel_build_leaves_flag_keys_to_the_system_conf(sys_conf_path):
    profile = {"CFLAGS": "-march=native -O2 -pipe"}
    with emit_makepkg_conf(profile, system_conf_path=sys_conf_path,
                           kernel_build=True) as conf:
        vals = read_conf(conf)
    assert "-march=native" not in vals["CFLAGS"]
    assert "-fstack-protector-strong" in vals["CFLAGS"]
