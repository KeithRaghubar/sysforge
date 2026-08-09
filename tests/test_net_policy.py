# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Unit tests for the source-freeze policy object (3.0.0-F2)."""
import argparse

import pytest

from sysforge.primitives.net_policy import (
    KIND_AUR_CLONE,
    KIND_VCS_RESOLVE,
    NetPolicy,
    NetworkFrozen,
    get_policy,
    reset_policy,
    resolve_net_policy,
    set_policy,
)


def _args(**kw):
    """Namespace with the three freeze flags defaulted to their argparse values."""
    base = {"frozen": False, "no_frozen": False, "thaw": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_permissive_policy_allows_everything():
    pol = NetPolicy(frozen=False, thawed=frozenset())
    pol.check(KIND_AUR_CLONE, "mesa")  # must not raise


def test_frozen_policy_denies():
    pol = NetPolicy(frozen=True, thawed=frozenset())
    with pytest.raises(NetworkFrozen) as exc:
        pol.check(KIND_AUR_CLONE, "mesa")
    # The message must name both the package and the egress kind: the operator
    # needs to know what to --thaw, and which seam to reason about.
    assert "mesa" in str(exc.value)
    assert KIND_AUR_CLONE in str(exc.value)


def test_thawed_package_is_exempt():
    pol = NetPolicy(frozen=True, thawed=frozenset({"mesa"}))
    pol.check(KIND_AUR_CLONE, "mesa")  # must not raise
    with pytest.raises(NetworkFrozen):
        pol.check(KIND_AUR_CLONE, "cosmic-comp")


def test_unknown_pkgbase_is_denied_under_freeze():
    """A seam that cannot name its package must not get a free pass.

    ``--thaw`` is per-pkgbase, so a None pkgbase can never match a thaw entry.
    Denying is the fail-closed reading; allowing would make any future seam
    that forgets to thread its pkgbase a silent bypass.
    """
    pol = NetPolicy(frozen=True, thawed=frozenset({"mesa"}))
    with pytest.raises(NetworkFrozen):
        pol.check(KIND_VCS_RESOLVE, None)


def test_policy_is_immutable():
    pol = NetPolicy(frozen=True, thawed=frozenset())
    with pytest.raises(Exception):  # noqa: B017 — dataclasses raise FrozenInstanceError
        pol.frozen = False


# --- resolve_net_policy: the four precedence rows -------------------------

def test_precedence_default_is_permissive():
    pol = resolve_net_policy(_args(), {})
    assert pol.frozen is False


def test_precedence_config_enables():
    pol = resolve_net_policy(_args(), {"freeze_sources": True})
    assert pol.frozen is True


def test_precedence_flag_enables_over_config_false():
    pol = resolve_net_policy(_args(frozen=True), {"freeze_sources": False})
    assert pol.frozen is True


def test_precedence_no_frozen_beats_config_true():
    """--no-frozen must win over `freeze_sources = true`.

    This is the row resolve_flag_default cannot express on its own — it has no
    explicit-false concept — so it is the row most likely to regress.
    """
    pol = resolve_net_policy(_args(no_frozen=True), {"freeze_sources": True})
    assert pol.frozen is False


def test_precedence_no_frozen_beats_frozen_flag():
    pol = resolve_net_policy(_args(frozen=True, no_frozen=True), {})
    assert pol.frozen is False


# --- --thaw parsing --------------------------------------------------------

def test_thaw_comma_separated():
    pol = resolve_net_policy(_args(frozen=True, thaw=["mesa,cosmic-comp"]), {})
    assert pol.thawed == frozenset({"mesa", "cosmic-comp"})


def test_thaw_repeatable_and_whitespace_tolerant():
    pol = resolve_net_policy(_args(frozen=True, thaw=["mesa", " cosmic-comp , llvm "]), {})
    assert pol.thawed == frozenset({"mesa", "cosmic-comp", "llvm"})


def test_thaw_alone_does_not_enable_the_freeze():
    """--thaw is a lift, not a switch. Without --frozen or config it is inert.

    Treating a bare --thaw as "freeze everything else" would turn a narrowing
    flag into a widening one — the opposite of what the name says.
    """
    pol = resolve_net_policy(_args(thaw=["mesa"]), {})
    assert pol.frozen is False


# --- module global ---------------------------------------------------------

def test_get_policy_defaults_permissive_when_unset():
    reset_policy()
    assert get_policy().frozen is False


def test_set_and_reset_policy():
    reset_policy()
    set_policy(NetPolicy(frozen=True, thawed=frozenset()))
    assert get_policy().frozen is True
    reset_policy()
    assert get_policy().frozen is False
