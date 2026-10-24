"""
test_failure.py — unit tests for sysforge.primitives.failure.

Covers:
    handle_failure behaviours    — abort, error, warn_and_fallback, fallback
    _ALWAYS_ABORT scenarios      — tempfile_write_failed aborts regardless of
                                   config override
    config override              — [failure_handling] table changes behaviour
    fallback return value        — correct value returned on fallback paths
    invalid behaviour            — a value outside the scenario's own set takes
                                   that scenario's default (3.5.0-B140)
    all default scenarios        — correct default for every raised scenario
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.primitives.failure import handle_failure, _ALWAYS_ABORT


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
        handle_failure("abi_mismatch", "the detail", cfg(abi_mismatch="abort"))


def test_error_raises_runtime_error():
    with pytest.raises(RuntimeError, match="makedep_probe_failed"):
        handle_failure("makedep_probe_failed", "oops", cfg(makedep_probe_failed="error"))


def test_abort_and_error_both_raise():
    for behaviour in ("abort", "error"):
        with pytest.raises(RuntimeError):
            handle_failure("abi_mismatch", "x", cfg(abi_mismatch=behaviour))


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
    result = handle_failure("abi_mismatch", "no match",
                            cfg(abi_mismatch="fallback"), fallback=42)
    assert result == 42


def test_fallback_default_is_none():
    result = handle_failure("abi_mismatch", "no match", cfg(abi_mismatch="fallback"))
    assert result is None


# ---------------------------------------------------------------------------
# _ALWAYS_ABORT — override any config setting
# ---------------------------------------------------------------------------

def test_tempfile_write_failed_always_aborts_despite_config():
    with pytest.raises(RuntimeError):
        handle_failure("tempfile_write_failed", "disk full",
                       cfg(tempfile_write_failed="warn_and_fallback"))


def test_always_abort_scenarios():
    assert {"tempfile_write_failed"} == _ALWAYS_ABORT


# ---------------------------------------------------------------------------
# A value outside the scenario's own set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["invalid_value", "auto_repair", "prompt_user", 3])
def test_invalid_behaviour_takes_the_scenario_default(value):
    # abi_mismatch defaults to warn_and_fallback: the fallback comes back.
    result = handle_failure("abi_mismatch", "x", cfg(abi_mismatch=value), fallback="fb")
    assert result == "fb"


# ---------------------------------------------------------------------------
# Config override changes default behaviour
# ---------------------------------------------------------------------------

def test_config_override_changes_warn_to_abort():
    # abi_mismatch defaults to warn_and_fallback; override to abort
    with pytest.raises(RuntimeError):
        handle_failure("abi_mismatch", "mismatch", cfg(abi_mismatch="abort"))


def test_empty_config_uses_defaults():
    result = handle_failure("makedep_probe_failed", "x", {})
    assert result is None


# ---------------------------------------------------------------------------
# Default behaviour for every raised scenario
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario",
                         ["pkgbuild_unparseable", "abi_mismatch", "makedep_probe_failed"])
def test_default_is_warn_and_fallback(scenario):
    assert handle_failure(scenario, "x", {}, fallback="fb") == "fb"


def test_default_tempfile_write_failed_aborts():
    with pytest.raises(RuntimeError):
        handle_failure("tempfile_write_failed", "x", {})


# ---------------------------------------------------------------------------
# Unknown scenario uses abort as ultimate default
# ---------------------------------------------------------------------------

def test_unknown_scenario_aborts():
    with pytest.raises(RuntimeError, match="totally_unknown"):
        handle_failure("totally_unknown", "surprise", {})
