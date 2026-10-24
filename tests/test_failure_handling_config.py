# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""`sysforge.toml [failure_handling]` reaches the builds it governs (3.5.0-B140).

Every reader took the table from the profiles.toml dict `makepkg_wrapper.run`
loads, so the documented section was never read. Each key now has its own set
of values; a value outside it takes that key's default, and `checksum_mismatch`
accepts only the two values that keep a person in the loop.
"""
from unittest.mock import patch

import pytest

from sysforge.build import makepkg_wrapper as mw
from sysforge.build.makepkg_wrapper import BuildOptions
from sysforge.primitives import auto_repair as ar
from sysforge.primitives import config as cfgmod
from sysforge.primitives.failure import SCENARIOS, resolve_failure_handling

DEFAULTS = {
    "vendored_deps_missing": "auto_repair",
    "pgp_key_missing": "auto_repair",
    "srcinfo_drift": "auto_repair_with_warning",
    "checksum_mismatch": "prompt_user",
    "pkgbuild_unparseable": "warn_and_fallback",
    "abi_mismatch": "warn_and_fallback",
    "makedep_probe_failed": "warn_and_fallback",
}


def test_every_key_has_its_documented_default():
    assert resolve_failure_handling(None) == DEFAULTS
    assert {k: d for k, (_, d) in SCENARIOS.items()} == DEFAULTS


@pytest.mark.parametrize("key, value", [
    ("vendored_deps_missing", "abort"),
    ("pgp_key_missing", "auto_repair_with_warning"),
    ("srcinfo_drift", "warn_and_fallback"),
    ("checksum_mismatch", "abort"),
    ("pkgbuild_unparseable", "abort"),
    ("abi_mismatch", "fallback"),
    ("makedep_probe_failed", "error"),
])
def test_a_valid_value_is_kept(key, value):
    assert resolve_failure_handling({key: value})[key] == value


@pytest.mark.parametrize("key, value", [
    ("checksum_mismatch", "auto_repair"),        # would mask a tampered source
    ("checksum_mismatch", "promt_user"),         # a typo never relaxes the guard
    ("vendored_deps_missing", "prompt_user"),    # never prompts; not offered
    ("srcinfo_drift", "error"),
    ("abi_mismatch", "auto_repair"),             # the other family's vocabulary
    ("pgp_key_missing", True),
])
def test_an_invalid_value_takes_the_default(key, value):
    assert resolve_failure_handling({key: value})[key] == DEFAULTS[key]


def test_an_unknown_key_is_dropped():
    assert resolve_failure_handling({"no_rule_matched": "abort"}) == DEFAULTS


def test_a_non_table_is_ignored():
    assert resolve_failure_handling("abort") == DEFAULTS


def test_an_unknown_auto_repair_behaviour_aborts():
    # Belt and braces behind the resolver: never the silent repair.
    assert ar._normalise_behaviour("bogus") == "abort"
    assert ar._normalise_behaviour(None) == "abort"


@pytest.fixture
def sysforge_toml(tmp_path, monkeypatch):
    path = tmp_path / "sysforge.toml"
    monkeypatch.setattr(cfgmod, "SYSFORGE_TOML_PATH", path)
    return path


@pytest.fixture
def checkout(tmp_path):
    d = tmp_path / "foo"
    d.mkdir()
    (d / "PKGBUILD").write_text("pkgname=foo\npkgver=1\npkgrel=1\n")
    return d


def _build_config(pkgbuild):
    seen = {}
    with patch.object(mw, "_run_build", side_effect=lambda *a, **k: seen.update(config=a[2])):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override="standard"))
    return seen["config"]


def test_run_reads_the_table_from_sysforge_toml(sysforge_toml, checkout):
    sysforge_toml.write_text('[failure_handling]\nabi_mismatch = "abort"\n')
    assert _build_config(checkout / "PKGBUILD")["failure_handling"]["abi_mismatch"] == "abort"


def test_run_without_the_table_uses_the_defaults(sysforge_toml, checkout):
    sysforge_toml.write_text("[ui]\n")
    assert _build_config(checkout / "PKGBUILD")["failure_handling"] == DEFAULTS


def test_dep_analysis_sees_the_sysforge_toml_value(sysforge_toml, checkout):
    sysforge_toml.write_text('[failure_handling]\nabi_mismatch = "abort"\n')
    seen = {}
    with patch.object(mw, "run_dep_analysis",
                      side_effect=lambda meta, config: seen.update(config)), \
         patch.object(mw, "_run_build"):
        mw.run(checkout / "PKGBUILD", options=BuildOptions(
            update=False, pkg_log=False, profile_override="standard"))
    assert seen["failure_handling"]["abi_mismatch"] == "abort"
