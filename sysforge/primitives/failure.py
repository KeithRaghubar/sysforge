# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
failure.py — SysForge failure scenario handling

Cross-cutting concern used by makepkg_wrapper, dep_analysis and auto_repair to
handle the named failure scenarios of sysforge.toml [failure_handling].

Public API:
    SCENARIOS                              every configurable key: (values, default)
    resolve_failure_handling(raw)          -> dict   (validated, defaults filled)
    handle_failure(scenario, message, config, fallback=None)
"""
from sysforge import log
_log = log.get_logger("FAILURE")

# The one home for the [failure_handling] keys (3.5.0-B140). Each key accepts
# only the values its reader acts on, so a value from the other family, or a
# typo, takes the default rather than a reader's catch-all.
_REPAIR = ("auto_repair", "auto_repair_with_warning", "abort")
_FALLBACK = ("abort", "error", "warn_and_fallback", "fallback")
SCENARIOS: dict[str, tuple[tuple[str, ...], str]] = {
    # auto_repair: matched in a failed build's output, repaired, retried.
    "vendored_deps_missing": (_REPAIR, "auto_repair"),
    "pgp_key_missing":       (_REPAIR, "auto_repair"),
    # Updating sums unasked would mask a tampered source: a person decides.
    "checksum_mismatch":     (("prompt_user", "abort"), "prompt_user"),
    # auto_repair preflight: regenerate, warn and go on, or stop.
    "srcinfo_drift":         (_REPAIR + ("warn_and_fallback",), "auto_repair_with_warning"),
    # handle_failure below.
    "pkgbuild_unparseable":  (_FALLBACK, "warn_and_fallback"),
    "abi_mismatch":          (_FALLBACK, "warn_and_fallback"),
    "makedep_probe_failed":  (_FALLBACK, "warn_and_fallback"),
}

# Not configurable: there is no sensible way to go on.
_ALWAYS_ABORT = {"tempfile_write_failed"}

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    # A batch resolves the table once per build; say each problem once.
    if message not in _warned:
        _warned.add(message)
        _log.warn(message)


def resolve_failure_handling(raw) -> dict[str, str]:
    """Validate a ``[failure_handling]`` table: one value for every key.

    An absent key, or a value outside that key's set, takes its default (an
    invalid one with a warning); an unknown key is ignored with a warning.
    """
    table = raw if isinstance(raw, dict) else {}
    for key in sorted(set(table) - set(SCENARIOS)):
        _warn_once(f"[failure_handling] {key} is not a known scenario: ignored")
    resolved = {}
    for key, (values, default) in SCENARIOS.items():
        value = table.get(key, default)
        if value not in values:
            _warn_once(f"[failure_handling] {key} = {value!r} is not one of "
                       f"{', '.join(values)}: using {default!r}")
            value = default
        resolved[key] = value
    return resolved


def handle_failure(scenario, message, config, fallback=None):
    """
    Handle a named failure scenario according to [failure_handling] config.

    Behaviours:
      abort             — log and raise RuntimeError immediately
      error             — log as error, raise RuntimeError
      warn_and_fallback — log as warning, return fallback value
      fallback          — return fallback value silently

    tempfile_write_failed, and any scenario not in SCENARIOS, always abort.
    """
    if scenario in _ALWAYS_ABORT or scenario not in SCENARIOS:
        behaviour = "abort"
    else:
        raw = (config.get("failure_handling") or {}).get(scenario)
        behaviour = resolve_failure_handling({scenario: raw} if raw is not None else {})[scenario]

    if behaviour == "abort":
        _log.error(f"[{scenario}] ABORT: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "error":
        _log.error(f"[{scenario}] ERROR: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "warn_and_fallback":
        _log.warn(f"[{scenario}] WARNING: {message}; falling back")
        return fallback
    elif behaviour == "fallback":
        return fallback
