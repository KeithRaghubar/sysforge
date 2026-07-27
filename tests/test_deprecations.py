# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Standards row 24 — the deprecation registry and its warn-on-use seam."""

import pytest

from sysforge.primitives import deprecations as dep


@pytest.fixture(autouse=True)
def _clean_dedup():
    """Every test starts with an empty once-per-run dedup set."""
    dep._reset_warned()
    yield
    dep._reset_warned()


def test_registry_is_not_empty():
    assert dep.all_deprecations(), "registry must not be empty"


def test_surfaces_are_unique():
    surfaces = [d.surface for d in dep.all_deprecations()]
    assert len(surfaces) == len(set(surfaces))


def test_compat_removals_are_major_only():
    for d in dep.all_deprecations():
        if d.function == dep.COMPAT:
            assert d.removed_in.endswith(".0.0"), \
                f"{d.surface}: compat removal {d.removed_in} is not a major"


def test_shim_has_anchor_and_compat_does_not():
    for d in dep.all_deprecations():
        if d.function == dep.SHIM:
            assert d.anchor, f"{d.surface}: shim needs an anchor"
        else:
            assert d.anchor is None, f"{d.surface}: compat must not carry an anchor"


def test_kinds_and_functions_are_in_vocabulary():
    kinds = {dep.CONFIG_KEY, dep.STATE_TOKEN, dep.CLI_FLAG, dep.PATH_DIR}
    for d in dep.all_deprecations():
        assert d.kind in kinds, f"{d.surface}: bad kind {d.kind!r}"
        assert d.function in {dep.COMPAT, dep.SHIM}, \
            f"{d.surface}: bad function {d.function!r}"


def test_get_returns_record_and_none():
    assert dep.get("git.pull_timeout").replacement == "git.fetch_timeout"
    assert dep.get("no.such.surface") is None


def test_warn_used_message_names_removal_and_replacement(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append((tag, msg)))
    dep.warn_used("git.pull_timeout")
    assert len(seen) == 1
    tag, msg = seen[0]
    assert tag == "[DEPRECATED]"
    assert "git.pull_timeout" in msg
    assert "3.0.0" in msg
    assert "git.fetch_timeout" in msg


def test_warn_used_fires_once_per_run(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used("git.pull_timeout")
    dep.warn_used("git.pull_timeout")
    dep.warn_used("git.pull_timeout")
    assert len(seen) == 1, "dedup must collapse repeat reads within one run"


def test_warn_used_dedups_per_surface_not_globally(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used("git.pull_timeout")
    dep.warn_used("build_state.build_mode=profiled")
    assert len(seen) == 2


def test_warn_used_unknown_surface_fails_soft(monkeypatch):
    warns, debugs = [], []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: warns.append(msg))
    monkeypatch.setattr(dep.log, "debug", lambda tag, msg: debugs.append(msg))
    dep.warn_used("git.pull_timout")       # typo, deliberately — must not raise
    assert warns == [], "an unregistered surface must not warn"
    assert len(debugs) == 1


def test_reset_warned_restores_state(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used("git.pull_timeout")
    dep._reset_warned()
    dep.warn_used("git.pull_timeout")
    assert len(seen) == 2


# --- per-surface resolution: the alias still works AND it warns --------------
# 2.6.1-F5 will INVERT these (alias gone => no longer resolves). Keeping them
# behavioural here is what makes that a small change.

def test_pull_timeout_alias_resolves_and_warns(monkeypatch):
    from sysforge.primitives import config as cfg
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    monkeypatch.setattr(cfg, "load_sysforge_toml",
                        lambda *a, **k: {"git": {"pull_timeout": 99}})
    from sysforge import update_sync
    monkeypatch.setattr(update_sync, "load_sysforge_toml",
                        lambda *a, **k: {"git": {"pull_timeout": 99}})
    git_cfg = update_sync.load_sysforge_toml().get("git", {})
    assert update_sync._resolve_fetch_timeout(git_cfg) == 99
    assert any("git.pull_timeout" in m for m in seen)


def test_fetch_timeout_does_not_warn(monkeypatch):
    from sysforge import update_sync
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert update_sync._resolve_fetch_timeout({"fetch_timeout": 12}) == 12
    assert seen == []


def test_fetch_timeout_default_does_not_warn(monkeypatch):
    from sysforge import update_sync
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert update_sync._resolve_fetch_timeout({}) == 30
    assert seen == []


def test_repo_mode_profiled_resolves_and_warns(monkeypatch):
    from sysforge.primitives import config as cfg
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert cfg.resolve_repo_mode({"repo_mode": "profiled"}) == cfg.REPO_MODE_SOURCE
    assert any("packages.repo_mode=profiled" in m for m in seen)


def test_repo_mode_current_token_does_not_warn(monkeypatch):
    from sysforge.primitives import config as cfg
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert cfg.resolve_repo_mode({"repo_mode": "build_from_source"}) \
        == cfg.REPO_MODE_SOURCE
    assert seen == []


def test_build_mode_profiled_normalizes_and_warns(monkeypatch, tmp_path):
    from sysforge.primitives import build_state as bs
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    (tmp_path / "build_state.toml").write_text(
        'foo = { build_mode = "profiled" }\n', encoding="utf-8")
    state = bs.BuildState(tmp_path)
    assert state._data["foo"]["build_mode"] == bs.BUILD_MODE_SOURCE
    assert any("build_state.build_mode=profiled" in m for m in seen)


def test_build_mode_current_token_does_not_warn(monkeypatch, tmp_path):
    from sysforge.primitives import build_state as bs
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    (tmp_path / "build_state.toml").write_text(
        'foo = { build_mode = "source_built" }\n', encoding="utf-8")
    bs.BuildState(tmp_path)
    assert seen == []
