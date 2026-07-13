# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_resource_guard.py — characterization coverage for the controller's
RLIMIT_AS cap.

``install()`` and ``lift_for_child()`` call the real ``resource`` syscalls,
and ``lift_for_child()`` is a subprocess ``preexec_fn`` (runs post-fork,
pre-exec) so it is invisible to line coverage from a real spawn. These tests
call the functions directly with ``resource`` patched, asserting the values
they apply and pinning the best-effort silent-``except`` arms — i.e. that a
raising syscall never propagates out to fail a build.
"""
import resource
from unittest.mock import patch

from sysforge.primitives import resource_guard as rg


class TestInstall:
    def test_caps_to_controller_limit_when_hard_is_infinite(self):
        # Hard limit unbounded → soft is clamped to the 2 GiB controller cap,
        # hard is preserved (and saved for later lift).
        fake = (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
        with patch.object(rg.resource, "getrlimit", return_value=fake), \
             patch.object(rg.resource, "setrlimit") as setr:
            rg.install()
        setr.assert_called_once_with(
            resource.RLIMIT_AS,
            (rg._CONTROLLER_LIMIT_BYTES, resource.RLIM_INFINITY),
        )
        assert rg._original_as_hard == resource.RLIM_INFINITY

    def test_soft_clamped_down_when_hard_below_controller_limit(self):
        # A pre-existing hard limit lower than 2 GiB must not be raised: the
        # soft request is clamped down to the hard limit (line 34).
        low_hard = 1024 * 1024 * 1024  # 1 GiB < 2 GiB controller cap
        with patch.object(rg.resource, "getrlimit",
                          return_value=(resource.RLIM_INFINITY, low_hard)), \
             patch.object(rg.resource, "setrlimit") as setr:
            rg.install()
        setr.assert_called_once_with(resource.RLIMIT_AS, (low_hard, low_hard))
        assert rg._original_as_hard == low_hard

    def test_hard_above_controller_limit_keeps_controller_soft(self):
        high_hard = 8 * 1024 * 1024 * 1024  # 8 GiB > 2 GiB cap
        with patch.object(rg.resource, "getrlimit",
                          return_value=(resource.RLIM_INFINITY, high_hard)), \
             patch.object(rg.resource, "setrlimit") as setr:
            rg.install()
        setr.assert_called_once_with(
            resource.RLIMIT_AS, (rg._CONTROLLER_LIMIT_BYTES, high_hard),
        )

    def test_setrlimit_failure_is_swallowed(self):
        # A raising setrlimit (OSError) must not propagate — install() is
        # best-effort (lines 36-37).
        with patch.object(rg.resource, "getrlimit",
                          return_value=(resource.RLIM_INFINITY,
                                        resource.RLIM_INFINITY)), \
             patch.object(rg.resource, "setrlimit",
                          side_effect=OSError("denied")):
            rg.install()  # no exception

    def test_getrlimit_failure_is_swallowed(self):
        with patch.object(rg.resource, "getrlimit",
                          side_effect=ValueError("bad resource")):
            rg.install()  # no exception


class TestLiftForChild:
    def test_restores_saved_hard_limit(self):
        # lift_for_child restores RLIMIT_AS to the hard limit install() saved,
        # as both soft and hard (lines 47-48).
        saved = 4 * 1024 * 1024 * 1024
        with patch.object(rg, "_original_as_hard", saved), \
             patch.object(rg.resource, "setrlimit") as setr:
            rg.lift_for_child()
        setr.assert_called_once_with(resource.RLIMIT_AS, (saved, saved))

    def test_defaults_to_infinity_when_never_installed(self):
        # Module default for the saved hard limit is RLIM_INFINITY.
        with patch.object(rg, "_original_as_hard", resource.RLIM_INFINITY), \
             patch.object(rg.resource, "setrlimit") as setr:
            rg.lift_for_child()
        setr.assert_called_once_with(
            resource.RLIMIT_AS,
            (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        )

    def test_setrlimit_failure_is_swallowed(self):
        # The lift runs in a child's preexec_fn; a raising syscall must never
        # propagate and abort the exec (lines 49-50).
        with patch.object(rg, "_original_as_hard", resource.RLIM_INFINITY), \
             patch.object(rg.resource, "setrlimit",
                          side_effect=OSError("EPERM")):
            rg.lift_for_child()  # no exception


class TestMakeChildPreexec:
    def test_none_cap_is_lift_only(self):
        # A None cap composes to exactly today's lift_for_child: restore the hard
        # limit, no extra RLIMIT_AS clamp.
        saved = 4 * 1024 * 1024 * 1024
        preexec = rg.make_child_preexec(None)
        with patch.object(rg, "_original_as_hard", saved), \
             patch.object(rg.resource, "setrlimit") as setr:
            preexec()
        setr.assert_called_once_with(resource.RLIMIT_AS, (saved, saved))

    def test_cap_composes_lift_then_setrlimit(self):
        # A byte cap runs the lift first, then clamps RLIMIT_AS to (cap, cap).
        saved = 32 * 1024 * 1024 * 1024
        cap = 24 * 1024 * 1024 * 1024
        preexec = rg.make_child_preexec(cap)
        with patch.object(rg, "_original_as_hard", saved), \
             patch.object(rg.resource, "setrlimit") as setr:
            preexec()
        assert setr.call_args_list[0][0] == (resource.RLIMIT_AS, (saved, saved))
        assert setr.call_args_list[1][0] == (resource.RLIMIT_AS, (cap, cap))

    def test_cap_clamped_to_hard_limit(self):
        # A cap above the current hard limit is clamped down to it — never raise
        # the ceiling above what the kernel allows.
        hard = 8 * 1024 * 1024 * 1024
        cap = 64 * 1024 * 1024 * 1024
        preexec = rg.make_child_preexec(cap)
        with patch.object(rg, "_original_as_hard", hard), \
             patch.object(rg.resource, "setrlimit") as setr:
            preexec()
        assert setr.call_args_list[1][0] == (resource.RLIMIT_AS, (hard, hard))

    def test_cap_setrlimit_failure_is_swallowed(self):
        # Best-effort: a raising clamp must not propagate into the child exec.
        with patch.object(rg, "_original_as_hard", resource.RLIM_INFINITY), \
             patch.object(rg.resource, "setrlimit",
                          side_effect=OSError("EPERM")):
            rg.make_child_preexec(24 * 1024 * 1024 * 1024)()  # no exception
