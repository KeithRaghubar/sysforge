"""F12 — build_mode vocabulary reconciliation.

The profile-layer ``build_mode`` token space was renamed so it stops diverging
from the build_state-layer token space: the profile value ``patched_pkgbuild``
(historically meaning "a plain source build whose embedded PKGBUILD profile
should be extracted") is the same concept build_state stamps as
``source_built``. The read alias was removed in 4.0.0 (3.5.0-F16): the old
token now raises. These also pin the single shared extraction predicate.
"""

import pytest

from sysforge.primitives.config import ConfigError
from sysforge.primitives.profile import (
    build_mode_uses_extracted_profile,
    get_build_mode,
    normalize_build_mode,
)


class TestNormalizeBuildMode:
    def test_removed_patched_pkgbuild_raises(self):
        with pytest.raises(ConfigError, match="removed in 4.0.0"):
            normalize_build_mode("patched_pkgbuild")

    def test_source_built_is_unchanged(self):
        assert normalize_build_mode("source_built") == "source_built"

    def test_kernel_is_unchanged(self):
        assert normalize_build_mode("kernel") == "kernel"

    def test_optimization_token_is_unchanged(self):
        assert normalize_build_mode("pgo_llvm_toolchain") == "pgo_llvm_toolchain"

    def test_none_is_passed_through(self):
        assert normalize_build_mode(None) is None


class TestGetBuildModeNormalizes:
    def _config(self, profile_build_mode):
        return {
            "defaults": {"profile": "p"},
            "profiles": {"p": {"build_mode": profile_build_mode}},
            "rules": [],
        }

    def test_removed_profile_token_raises_on_read(self):
        # A stale profiles.toml fails loudly instead of quietly building the
        # package without its embedded profile.
        with pytest.raises(ConfigError, match="source_built"):
            get_build_mode([], self._config("patched_pkgbuild"))

    def test_canonical_token_reads_back_unchanged(self):
        assert get_build_mode([], self._config("source_built")) == "source_built"

    def test_no_build_mode_returns_none(self):
        cfg = {"defaults": {"profile": "p"}, "profiles": {"p": {}}, "rules": []}
        assert get_build_mode([], cfg) is None


class TestUsesExtractedProfilePredicate:
    def test_canonical_source_built_uses_extracted_profile(self):
        assert build_mode_uses_extracted_profile("source_built") is True

    def test_kernel_uses_extracted_profile(self):
        assert build_mode_uses_extracted_profile("kernel") is True

    def test_optimization_mode_does_not(self):
        assert build_mode_uses_extracted_profile("pgo_llvm_toolchain") is False

    def test_none_does_not(self):
        assert build_mode_uses_extracted_profile(None) is False
