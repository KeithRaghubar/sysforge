"""
test_wrapper.py — parametrized rule matching tests driven by _expect_* keys
in tests/data/test_profiles.toml.

Each rule in that file carries _expect_htop, _expect_lib32, and _expect_llvm
boolean annotations declaring whether that rule should match each PKGBUILD.
This test file reads those annotations and asserts accordingly, so adding a
new rule to the TOML automatically adds coverage here with no code changes.
"""
import tomllib
from pathlib import Path

import pytest

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.profile import match_rules

TESTS_DIR = Path(__file__).parent
TEST_DATA = TESTS_DIR / "data"
PROFILES_TOML = TEST_DATA / "test_profiles.toml"

PKGBUILD_PATHS = {
    "htop":  TEST_DATA / "PKGBUILDs/htop.PKGBUILD",
    "lib32": TEST_DATA / "PKGBUILDs/lib32-llvm.PKGBUILD",
    "llvm":  TEST_DATA / "PKGBUILDs/llvm.PKGBUILD",
}

# Load once at module level — TOML is static test data
with open(PROFILES_TOML, "rb") as _f:
    _config = tomllib.load(_f)

_raw_rules = _config.get("rules", [])

# Strip _expect_* keys out before passing rules to match_rules
_clean_rules = [
    {k: v for k, v in r.items() if not k.startswith("_")}
    for r in _raw_rules
]

# Parse PKGBUILDs once
_metas = {alias: parse_pkgbuild(path) for alias, path in PKGBUILD_PATHS.items()}

# Pre-compute matched rule sets for each alias
_matched_ids = {
    alias: {id(r) for r in match_rules(_metas[alias], _clean_rules)}
    for alias in _metas
}


def _build_params():
    """
    Yield (rule_index, alias, expected_match) tuples for all rules that carry
    an _expect_<alias> annotation.
    """
    params = []
    for i, rule in enumerate(_raw_rules):
        for alias in PKGBUILD_PATHS:
            key = f"_expect_{alias}"
            if key in rule:
                expected = rule[key]
                label = f"rule[{i}]({rule.get('profile','?')})__{alias}"
                params.append(pytest.param(i, alias, expected, id=label))
    return params


@pytest.mark.parametrize("rule_index,alias,expected", _build_params())
def test_rule_match(rule_index, alias, expected):
    """Assert that rule at rule_index matches/skips alias as annotated."""
    clean_rule = _clean_rules[rule_index]
    did_match = id(clean_rule) in _matched_ids[alias]
    assert did_match == expected, (
        f"Rule [{rule_index}] profile={_raw_rules[rule_index].get('profile')!r}: "
        f"expected {'MATCH' if expected else 'SKIP'} for {alias!r}, "
        f"got {'MATCH' if did_match else 'SKIP'}\n"
        f"Rule: {clean_rule}"
    )


# ---------------------------------------------------------------------------
# Interactive-failure diagnosis helpers (_effective_build_dir, _build_failed_error)
# ---------------------------------------------------------------------------

def test_effective_build_dir_uses_builddir(tmp_path):
    """With BUILDDIR set, the diagnosis dir is $BUILDDIR/<pkgbase> (where the
    meson/cmake side-car logs live), not the in-place PKGBUILD dir."""
    from sysforge.primitives.makepkg_env import _effective_build_dir

    builddir = tmp_path / "builds"
    pkgdir = tmp_path / "src" / "wayland-protocols-git"
    pkgdir.mkdir(parents=True)
    (pkgdir / "PKGBUILD").write_text("# pkgbuild\n")
    real = builddir / "wayland-protocols-git"
    (real / "src").mkdir(parents=True)

    got = _effective_build_dir(
        pkgdir / "PKGBUILD", {"BUILDDIR": str(builddir)}
    )
    assert got == real


def test_effective_build_dir_falls_back_to_pkgbuild_dir(tmp_path):
    """When the BUILDDIR candidate has no src/, fall back to the PKGBUILD dir."""
    from sysforge.primitives.makepkg_env import _effective_build_dir

    pkgdir = tmp_path / "src" / "foo-git"
    pkgdir.mkdir(parents=True)
    got = _effective_build_dir(
        pkgdir / "PKGBUILD", {"BUILDDIR": str(tmp_path / "builds")}
    )
    assert got == pkgdir


def test_build_failed_error_carries_diagnosis():
    """The abort RuntimeError preserves .diagnosis/.captured_output recovered
    from the failed build's side-car logs."""
    from sysforge.primitives.build_diag import FixSuggestion
    from sysforge.primitives.makepkg_wrapper import _build_failed_error

    import subprocess as _sp
    cause = _sp.CalledProcessError(4, "makepkg")
    cause.diagnosis = [FixSuggestion("toolchain:llvm-broken", "broken", "fix")]
    cause.captured_output = []
    err = _build_failed_error(cause, "[build_failed] Aborted by user after build failure")
    assert "Aborted by user" in str(err)
    assert err.diagnosis[0].signature == "toolchain:llvm-broken"
    assert err.captured_output == []
