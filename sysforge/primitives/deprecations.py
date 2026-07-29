# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Registry of surfaces pending removal — standards row 24.

Single home for every config key, state token, CLI flag, or path that sysforge
still honours only for backwards compatibility. Each record declares the version
it was deprecated in and the version it is removed in; each compat read path
calls :func:`warn_used`, which builds its message *from the record* so the
removal version in the warning can never drift from the one the release gate
enforces.

Two `function` values, because removal is not uniformly breaking:

``COMPAT``
    The old spelling still works. Removing it breaks a configuration that
    currently succeeds, so ``removed_in`` must be a major (``X.0.0``). Presence
    is proven by this module's ``warn_used`` call sites.

``SHIM``
    The surface already fails (e.g. the flat ``doctor`` flags print their
    replacement and exit 2). Deleting it changes an error message's helpfulness,
    not the contract, so ``removed_in`` may be any ``X.Y.0``. A shim has no warn
    path, so presence is proven by its ``anchor`` (``<repo-relative path>.py::<symbol>``).

Enforcement lives in ``tools/check_standards.py`` (``deprecations`` group):
registry<->call-site bijection, major-only removal for compat surfaces, and an
error when a release ships at or past a declared removal with the surface still
present. This module never raises — a bookkeeping mistake must not break a
build; the static check catches it instead.
"""
from __future__ import annotations

from dataclasses import dataclass

from sysforge import log

# --- kind vocabulary -------------------------------------------------------
CONFIG_KEY = "config_key"
STATE_TOKEN = "state_token"  # noqa: S105 -- not a secret, a kind label
CLI_FLAG = "cli_flag"
# Named PATH_DIR (not PATH) so importing modules can't shadow anything.
PATH_DIR = "path"

# --- function vocabulary ---------------------------------------------------
COMPAT = "compat"
SHIM = "shim"

# Log tag for every deprecation notice. Fixed rather than caller-supplied so the
# notices are greppable as one class.
_TAG = "[DEPRECATED]"


@dataclass(frozen=True)
class Deprecation:
    """One surface pending removal. `anchor` is SHIM-only."""

    surface: str
    kind: str
    function: str
    deprecated_in: str
    removed_in: str
    replacement: str
    anchor: str | None = None


# The registry. `deprecated_in` values are recovered from git history (the commit
# that introduced the replacement, resolved to its first containing tag), not
# guessed.
_REGISTRY: tuple[Deprecation, ...] = (
    Deprecation(
        surface="doctor.flat_flags",
        kind=CLI_FLAG,
        function=SHIM,
        deprecated_in="3.0.0",
        removed_in="3.1.0",
        replacement="`sysforge doctor system` / `sysforge doctor pkg`",
        anchor="sysforge/doctor.py::doctor_migration_hint",
    ),
)

_BY_SURFACE = {d.surface: d for d in _REGISTRY}

# Once-per-run dedup. Load-bearing, not cosmetic: config and state reads repeat
# within a run, so without dedup a single surface would warn on every read.
_warned: set[str] = set()


def all_deprecations() -> tuple[Deprecation, ...]:
    """Every registered surface, in declaration order."""
    return _REGISTRY


def get(surface: str) -> Deprecation | None:
    """The record for `surface`, or None if it is not registered."""
    return _BY_SURFACE.get(surface)


def _message(d: Deprecation) -> str:
    return (f"{d.kind.replace('_', ' ')} `{d.surface}` is deprecated since "
            f"{d.deprecated_in} and is removed in {d.removed_in} — "
            f"use {d.replacement}")


def warn_used(surface: str) -> None:
    """Warn once per run that `surface` is scheduled for removal.

    Fail-soft by design: an unregistered surface logs at debug and returns
    rather than raising. sysforge is a build tool, so a bookkeeping mistake must
    never be why a build dies — `check_standards.py`'s `deprecations` group
    catches an unregistered `warn_used` literal statically instead.
    """
    d = _BY_SURFACE.get(surface)
    if d is None:
        log.debug(_TAG, f"warn_used called for unregistered surface {surface!r}")
        return
    if surface in _warned:
        return
    _warned.add(surface)
    log.warn(_TAG, _message(d))


def _reset_warned() -> None:
    """Test hook: clear the once-per-run dedup set."""
    _warned.clear()
