"""
test_failure.py — unit tests for sysforge.primitives.failure.

Covers:
    handle_failure behaviours    — abort, error, warn_and_fallback, fallback
    _ALWAYS_ABORT scenarios      — profile_missing and tempfile_write_failed
                                   abort regardless of config override
    config override              — [failure_handling] table changes behaviour
    fallback return value        — correct value returned on fallback paths
    unknown behaviour            — invalid config value falls back to abort
    all default scenarios        — correct default for every named scenario
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.failure import handle_failure, _FAILURE_DEFAULTS, _ALWAYS_ABORT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cfg(**kwargs):
    """Build a minimal config dict with failure_handling overrides."""
    return {"failure_handling": kwargs}


# ---------------------------------------------------------------------------
# abort / error — both raise RuntimeError
# ---------------------------------------------------------------------------

def test_abort_raises_runtime_error():
    with pytest.raises(RuntimeError, match="pkgbuild_unparseable"):
        handle_failure("pkgbuild_unparseable", "bad file", cfg(pkgbuild_unparseable="abort"))


def test_abort_message_contains_message():
    with pytest.raises(RuntimeError, match="the detail"):
        handle_failure("no_rule_matched", "the detail", cfg(no_rule_matched="abort"))


def test_error_raises_runtime_error():
    with pytest.raises(RuntimeError, match="no_rule_matched"):
        handle_failure("no_rule_matched", "oops", cfg(no_rule_matched="error"))


def test_abort_and_error_both_raise():
    for behaviour in ("abort", "error"):
        with pytest.raises(RuntimeError):
            handle_failure("no_rule_matched", "x", cfg(no_rule_matched=behaviour))


# ---------------------------------------------------------------------------
# warn_and_fallback
# ---------------------------------------------------------------------------

def test_warn_and_fallback_returns_fallback():
    result = handle_failure("abi_mismatch", "mismatch", {}, fallback="safe")
    assert result == "safe"


def test_warn_and_fallback_default_fallback_is_none():
    result = handle_failure("abi_mismatch", "mismatch", {})
    assert result is None


def test_warn_and_fallback_does_not_raise():
    # Should complete without raising
    handle_failure("pkgbuild_unparseable", "bad", cfg(pkgbuild_unparseable="warn_and_fallback"))


# ---------------------------------------------------------------------------
# fallback (silent)
# ---------------------------------------------------------------------------

def test_fallback_returns_fallback_value():
    result = handle_failure("no_rule_matched", "no match", cfg(no_rule_matched="fallback"), fallback=42)
    assert result == 42


def test_fallback_default_is_none():
    result = handle_failure("no_rule_matched", "no match", cfg(no_rule_matched="fallback"))
    assert result is None


def test_fallback_does_not_raise():
    handle_failure("no_rule_matched", "x", cfg(no_rule_matched="fallback"))


# ---------------------------------------------------------------------------
# _ALWAYS_ABORT — override any config setting
# ---------------------------------------------------------------------------

def test_profile_missing_always_aborts_despite_config():
    with pytest.raises(RuntimeError):
        handle_failure("profile_missing", "gone", cfg(profile_missing="fallback"))


def test_tempfile_write_failed_always_aborts_despite_config():
    with pytest.raises(RuntimeError):
        handle_failure("tempfile_write_failed", "disk full", cfg(tempfile_write_failed="warn_and_fallback"))


def test_all_always_abort_scenarios_covered():
    # Verify _ALWAYS_ABORT set is what we expect
    assert "profile_missing" in _ALWAYS_ABORT
    assert "tempfile_write_failed" in _ALWAYS_ABORT


# ---------------------------------------------------------------------------
# Unknown behaviour value in config
# ---------------------------------------------------------------------------

def test_unknown_behaviour_defaults_to_abort():
    with pytest.raises(RuntimeError):
        handle_failure("no_rule_matched", "x", cfg(no_rule_matched="invalid_value"))


# ---------------------------------------------------------------------------
# Config override changes default behaviour
# ---------------------------------------------------------------------------

def test_config_override_changes_abort_to_warn():
    # abi_mismatch defaults to warn_and_fallback; override to abort
    with pytest.raises(RuntimeError):
        handle_failure("abi_mismatch", "mismatch", cfg(abi_mismatch="abort"))


def test_config_override_changes_fallback_to_abort():
    with pytest.raises(RuntimeError):
        handle_failure("no_rule_matched", "x", cfg(no_rule_matched="abort"))


def test_empty_config_uses_defaults():
    # no_rule_matched default is "fallback" — should return None without raising
    result = handle_failure("no_rule_matched", "x", {})
    assert result is None


# ---------------------------------------------------------------------------
# Default behaviour for every named scenario
# ---------------------------------------------------------------------------

def test_default_pkgbuild_unparseable_is_warn_and_fallback():
    result = handle_failure("pkgbuild_unparseable", "bad", {}, fallback="fb")
    assert result == "fb"


def test_default_no_rule_matched_is_fallback():
    result = handle_failure("no_rule_matched", "x", {}, fallback="fb")
    assert result == "fb"


def test_default_profile_missing_aborts():
    with pytest.raises(RuntimeError):
        handle_failure("profile_missing", "x", {})


def test_default_profile_cycle_aborts():
    with pytest.raises(RuntimeError):
        handle_failure("profile_cycle", "x", {})


def test_default_tempfile_write_failed_aborts():
    with pytest.raises(RuntimeError):
        handle_failure("tempfile_write_failed", "x", {})


def test_default_env_conflict_is_warn_and_fallback():
    result = handle_failure("env_conflict", "x", {}, fallback="fb")
    assert result == "fb"


def test_default_abi_mismatch_is_warn_and_fallback():
    result = handle_failure("abi_mismatch", "x", {}, fallback="fb")
    assert result == "fb"


def test_default_dep_unsatisfied_is_warn_and_fallback():
    result = handle_failure("dep_unsatisfied", "x", {}, fallback="fb")
    assert result == "fb"


# ---------------------------------------------------------------------------
# Unknown scenario uses abort as ultimate default
# ---------------------------------------------------------------------------

def test_unknown_scenario_aborts():
    with pytest.raises(RuntimeError, match="totally_unknown"):
        handle_failure("totally_unknown", "surprise", {})
