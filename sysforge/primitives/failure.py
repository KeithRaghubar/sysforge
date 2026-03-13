"""
failure.py — SysForge failure scenario handling

Cross-cutting concern used by makepkg_wrapper, dep_analysis, and any future
modules that need to handle named failure scenarios from [failure_handling] config.

Public API:
    handle_failure(scenario, message, config, fallback=None)
"""
import sysforge.log as _log

_ALWAYS_ABORT = {"profile_missing", "tempfile_write_failed"}

_FAILURE_DEFAULTS = {
    "pkgbuild_unparseable":  "warn_and_fallback",
    "no_rule_matched":       "fallback",
    "profile_missing":       "abort",
    "profile_cycle":         "abort",
    "tempfile_write_failed": "abort",
    "env_conflict":          "warn_and_fallback",
    "abi_mismatch":          "warn_and_fallback",
    "dep_unsatisfied":       "warn_and_fallback",
}

_VALID_BEHAVIOURS = {"abort", "warn_and_fallback", "fallback", "error"}


def handle_failure(scenario, message, config, fallback=None):
    """
    Handle a named failure scenario according to [failure_handling] config.

    Behaviours:
      abort             — log and raise RuntimeError immediately
      error             — log as error, raise RuntimeError
      warn_and_fallback — log as warning, return fallback value
      fallback          — return fallback value silently

    profile_missing and tempfile_write_failed always abort regardless of config.
    """
    failure_cfg = config.get("failure_handling", {})
    behaviour = failure_cfg.get(scenario, _FAILURE_DEFAULTS.get(scenario, "abort"))

    if scenario in _ALWAYS_ABORT:
        behaviour = "abort"

    if behaviour not in _VALID_BEHAVIOURS:
        _log.error("[FAILURE]", f"Unknown behaviour {behaviour!r} for scenario {scenario!r} — defaulting to abort")
        behaviour = "abort"

    if behaviour == "abort":
        _log.error("[FAILURE]", f"[{scenario}] ABORT: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "error":
        _log.error("[FAILURE]", f"[{scenario}] ERROR: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "warn_and_fallback":
        _log.warn("[FAILURE]", f"[{scenario}] WARNING: {message} — falling back")
        return fallback
    elif behaviour == "fallback":
        return fallback
