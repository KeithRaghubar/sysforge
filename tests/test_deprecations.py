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


def test_profiles_patched_pkgbuild_is_registered():
    """2.6.1-STD3: the profile-layer build_mode alias was the one compat
    surface 2.6.1-STD2 missed — the bijection check walks registry→call-site,
    never code→registry, so an unregistered alias is invisible to it. It is a
    COMPAT surface (the old spelling still works), so its removal must land on
    a major; 3.0.0 is already shipping with it present, hence 4.0.0."""
    d = dep.get("profiles.build_mode=patched_pkgbuild")
    assert d is not None, "the alias must carry a registry record"
    assert d.function == dep.COMPAT
    assert d.kind == dep.CONFIG_KEY
    assert d.removed_in == "4.0.0"
    assert d.anchor is None


def test_normalize_build_mode_warns_on_legacy_token(monkeypatch):
    """The alias branch of the read chokepoint must route through warn_used,
    so the removal version in the notice is built from the record."""
    from sysforge.primitives import profile
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert profile.normalize_build_mode("patched_pkgbuild") == "source_built"
    assert len(seen) == 1
    assert "patched_pkgbuild" in seen[0]
    assert "4.0.0" in seen[0]


def test_normalize_build_mode_current_token_does_not_warn(monkeypatch):
    """Only the alias branch warns — the canonical token and None are silent,
    and normalize_build_mode is called on every profile read."""
    from sysforge.primitives import profile
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    assert profile.normalize_build_mode("source_built") == "source_built"
    assert profile.normalize_build_mode("kernel") == "kernel"
    assert profile.normalize_build_mode(None) is None
    assert seen == []


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
