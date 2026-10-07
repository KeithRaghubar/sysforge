# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
test_tracker_guards.py — functions that open their own tracker refuse to run
inside one (3.3.0-F2).

``build_and_install`` opens the ``building`` tracker, ``prepare_deps`` reaches
``build_resolved_deps``, which opens ``AUR dep``. Called from inside another
tracker, the inner one would be dropped. Each guards at entry, so the refusal
lands before any sync or resolution side effect. Every call below passes
arguments that would fail or do work further in; the guard has to fire first.
The suite runs with strict nesting (tests/conftest.py), so the guard raises.
"""
from pathlib import Path

import pytest

from sysforge import build_core
from sysforge.build import aur_deps
from sysforge.ui import progress


def test_build_and_install_refuses_inside_a_tracker():
    with progress.tracker(2, "packages"), \
            pytest.raises(RuntimeError, match="build_and_install called inside tracker 'packages'"):
        build_core.build_and_install([object()], config={}, sync_source=True)


def test_prepare_deps_refuses_inside_a_tracker():
    with progress.tracker(2, "scanning"), \
            pytest.raises(RuntimeError, match="prepare_deps called inside tracker 'scanning'"):
        build_core.prepare_deps([Path("/nonexistent/PKGBUILD")], {})


def test_build_resolved_deps_refuses_inside_a_tracker():
    expected = "build_resolved_deps called inside tracker 'building'"
    with progress.tracker(2, "building"), pytest.raises(RuntimeError, match=expected):
        aur_deps.build_resolved_deps([object()])  # type: ignore[list-item]
