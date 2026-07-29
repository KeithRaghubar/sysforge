# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Phase 2 of `update`: source synchronisation (`sysforge/update_sync.py`)."""


def test_git_fetch_timeout_does_not_warn(monkeypatch):
    from sysforge.primitives import deprecations as dep
    from sysforge.update_sync import _resolve_fetch_timeout

    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert _resolve_fetch_timeout({"fetch_timeout": 99}) == 99
    assert seen == []


def test_resolve_fetch_timeout_ignores_removed_pull_timeout_alias():
    """3.0.0 removed the alias: the legacy key falls through to the default."""
    from sysforge.update_sync import _resolve_fetch_timeout

    assert _resolve_fetch_timeout({"pull_timeout": 99}) == 30
    assert _resolve_fetch_timeout({"fetch_timeout": 99}) == 99
    assert _resolve_fetch_timeout({}) == 30
