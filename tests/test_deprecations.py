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


# A stand-in compat record. The registry is empty after 4.0.0 retired its last
# surface (3.5.0-F16), so the warn seam is exercised against this one.
_FAKE = dep.Deprecation(
    surface="test.old_key",
    kind=dep.CONFIG_KEY,
    function=dep.COMPAT,
    deprecated_in="3.0.0",
    removed_in="9.0.0",
    replacement="new_key",
)


@pytest.fixture
def fake_record(monkeypatch):
    monkeypatch.setitem(dep._BY_SURFACE, _FAKE.surface, _FAKE)
    return _FAKE


def test_surfaces_are_unique():
    surfaces = [d.surface for d in dep._REGISTRY]
    assert len(surfaces) == len(set(surfaces))


def test_compat_removals_are_major_only():
    for d in dep._REGISTRY:
        if d.function == dep.COMPAT:
            assert d.removed_in.endswith(".0.0"), \
                f"{d.surface}: compat removal {d.removed_in} is not a major"


def test_shim_has_anchor_and_compat_does_not():
    for d in dep._REGISTRY:
        if d.function == dep.SHIM:
            assert d.anchor, f"{d.surface}: shim needs an anchor"
        else:
            assert d.anchor is None, f"{d.surface}: compat must not carry an anchor"


def test_kinds_and_functions_are_in_vocabulary():
    kinds = {dep.CONFIG_KEY, dep.STATE_TOKEN, dep.CLI_FLAG, dep.PATH_DIR}
    for d in dep._REGISTRY:
        assert d.kind in kinds, f"{d.surface}: bad kind {d.kind!r}"
        assert d.function in {dep.COMPAT, dep.SHIM}, \
            f"{d.surface}: bad function {d.function!r}"


def test_get_returns_record_and_none(fake_record):
    assert dep.get(fake_record.surface).replacement == "new_key"
    assert dep.get("no.such.surface") is None


def test_warn_used_message_names_removal_and_replacement(monkeypatch, fake_record):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append((tag, msg)))
    dep.warn_used(fake_record.surface)
    assert len(seen) == 1
    tag, msg = seen[0]
    assert tag == "[DEPRECATED]"
    assert fake_record.surface in msg
    assert "9.0.0" in msg
    assert "new_key" in msg


def test_warn_used_fires_once_per_run(monkeypatch, fake_record):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    for _ in range(3):
        dep.warn_used(fake_record.surface)
    assert len(seen) == 1, "dedup must collapse repeat reads within one run"


def test_warn_used_dedups_per_surface_not_globally(monkeypatch, fake_record):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    other = dep.Deprecation(
        surface="test.fake_surface",
        kind=dep.CLI_FLAG,
        function=dep.SHIM,
        deprecated_in="3.0.0",
        removed_in="3.1.0",
        replacement="nothing",
        anchor="tests/test_deprecations.py::test_warn_used_dedups_per_surface_not_globally",
    )
    monkeypatch.setitem(dep._BY_SURFACE, other.surface, other)
    dep.warn_used(fake_record.surface)
    dep.warn_used(other.surface)
    assert len(seen) == 2


def test_warn_used_unknown_surface_fails_soft(monkeypatch):
    warns, debugs = [], []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: warns.append(msg))
    monkeypatch.setattr(dep.log, "debug", lambda tag, msg: debugs.append(msg))
    dep.warn_used("test.old_keyz")     # typo, deliberately: must not raise
    assert warns == [], "an unregistered surface must not warn"
    assert len(debugs) == 1


def test_reset_warned_restores_state(monkeypatch, fake_record):
    seen = []
    monkeypatch.setattr(dep.log, "warn", lambda tag, msg: seen.append(msg))
    dep.warn_used(fake_record.surface)
    dep._reset_warned()
    dep.warn_used(fake_record.surface)
    assert len(seen) == 2


def test_build_mode_patched_pkgbuild_is_rejected():
    """3.5.0-F16: the alias was removed in 4.0.0. It must raise rather than
    fall through as an unknown mode, which would silently stop extracting the
    PKGBUILD's embedded profile."""
    from sysforge.primitives import profile
    from sysforge.primitives.config import ConfigError
    with pytest.raises(ConfigError, match="source_built"):
        profile.normalize_build_mode("patched_pkgbuild")
    assert dep.get("profiles.build_mode=patched_pkgbuild") is None


def test_normalize_build_mode_passes_current_tokens(monkeypatch):
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
