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
    assert dep.get("doctor.flat_flags").replacement == \
        "`sysforge doctor system` / `sysforge doctor pkg`"
    assert dep.get("no.such.surface") is None


def test_warn_used_message_names_removal_and_replacement(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append((tag, msg)))
    dep.warn_used("doctor.flat_flags")
    assert len(seen) == 1
    tag, msg = seen[0]
    assert tag == "[DEPRECATED]"
    assert "doctor.flat_flags" in msg
    assert "3.1.0" in msg
    assert "sysforge doctor system" in msg


def test_warn_used_fires_once_per_run(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used("doctor.flat_flags")
    dep.warn_used("doctor.flat_flags")
    dep.warn_used("doctor.flat_flags")
    assert len(seen) == 1, "dedup must collapse repeat reads within one run"


def test_warn_used_dedups_per_surface_not_globally(monkeypatch):
    """Dedup keys on surface, not on a single global once-only flag. Only
    doctor.flat_flags ships as of 3.0.0, so a second record is injected here
    purely to exercise the dedup granularity — it is not a real surface."""
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    fake = dep.Deprecation(
        surface="test.fake_surface",
        kind=dep.CLI_FLAG,
        function=dep.SHIM,
        deprecated_in="3.0.0",
        removed_in="3.1.0",
        replacement="nothing",
        anchor="tests/test_deprecations.py::test_warn_used_dedups_per_surface_not_globally",
    )
    monkeypatch.setitem(dep._BY_SURFACE, fake.surface, fake)
    dep.warn_used("doctor.flat_flags")
    dep.warn_used(fake.surface)
    assert len(seen) == 2


def test_warn_used_unknown_surface_fails_soft(monkeypatch):
    warns, debugs = [], []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: warns.append(msg))
    monkeypatch.setattr(dep.log, "debug", lambda tag, msg: debugs.append(msg))
    dep.warn_used("doctor.flat_flagz")     # typo, deliberately — must not raise
    assert warns == [], "an unregistered surface must not warn"
    assert len(debugs) == 1


def test_reset_warned_restores_state(monkeypatch):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used("doctor.flat_flags")
    dep._reset_warned()
    dep.warn_used("doctor.flat_flags")
    assert len(seen) == 2


def test_repo_mode_profiled_is_rejected(monkeypatch):
    """3.0.0 removed the alias. It must not resolve or warn as a known token."""
    from sysforge.primitives import config as cfg
    assert "profiled" not in cfg.REPO_MODE_ACCEPTED_INPUTS


def test_repo_mode_current_token_does_not_warn(monkeypatch):
    from sysforge.primitives import config as cfg
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert cfg.resolve_repo_mode({"repo_mode": "build_from_source"}) \
        == cfg.REPO_MODE_SOURCE
    assert seen == []


def test_build_mode_current_token_does_not_warn(monkeypatch, tmp_path):
    from sysforge.primitives import build_state as bs
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    (tmp_path / "build_state.toml").write_text(
        'foo = { build_mode = "source_built" }\n', encoding="utf-8")
    bs.BuildState(tmp_path)
    assert seen == []
