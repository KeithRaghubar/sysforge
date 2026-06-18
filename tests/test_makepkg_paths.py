"""
test_makepkg_paths.py — makepkg path-variable resolution.

Covers the unified resolvers in pacman.py (env-first, then layered
makepkg.conf) and the two BUILDDIR consumers that must respect a BUILDDIR
set only in /etc/makepkg.conf.
"""
from pathlib import Path

import pytest

import sysforge.primitives.config as config
import sysforge.primitives.pacman as pacman


# ---------------------------------------------------------------------------
# Resolvers: env-first, then layered conf, else None
# ---------------------------------------------------------------------------

@pytest.fixture
def no_conf(monkeypatch):
    """Force the layered system makepkg.conf to look empty."""
    monkeypatch.setattr(config, "parse_system_makepkg_conf", lambda: {})


def test_builddir_from_env(monkeypatch, no_conf):
    monkeypatch.setenv("BUILDDIR", "/tmp/bd")
    assert pacman.get_builddir() == Path("/tmp/bd")


def test_builddir_from_conf_when_env_unset(monkeypatch):
    monkeypatch.delenv("BUILDDIR", raising=False)
    monkeypatch.setattr(
        config, "parse_system_makepkg_conf", lambda: {"BUILDDIR": '"/var/bd"'}
    )
    assert pacman.get_builddir() == Path("/var/bd")


def test_env_overrides_conf(monkeypatch):
    monkeypatch.setenv("BUILDDIR", "/tmp/env")
    monkeypatch.setattr(
        config, "parse_system_makepkg_conf", lambda: {"BUILDDIR": "/var/conf"}
    )
    assert pacman.get_builddir() == Path("/tmp/env")


def test_unset_everywhere_returns_none(monkeypatch, no_conf):
    monkeypatch.delenv("BUILDDIR", raising=False)
    assert pacman.get_builddir() is None


def test_resolver_strips_quotes_and_expands(monkeypatch, no_conf):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("SRCDEST", "'~/sources'")
    assert pacman.get_srcdest() == Path("/home/tester/sources")


def test_pkgdest_and_logdest_share_resolver(monkeypatch):
    monkeypatch.delenv("PKGDEST", raising=False)
    monkeypatch.delenv("LOGDEST", raising=False)
    monkeypatch.setattr(
        config,
        "parse_system_makepkg_conf",
        lambda: {"PKGDEST": "/pkgs", "LOGDEST": "/logs"},
    )
    assert pacman.get_pkgdest() == Path("/pkgs")
    assert pacman.get_logdest() == Path("/logs")


def test_pkgdest_now_honours_env(monkeypatch, no_conf):
    # Regression: get_pkgdest gained env-first resolution.
    monkeypatch.setenv("PKGDEST", "/tmp/pkgs")
    assert pacman.get_pkgdest() == Path("/tmp/pkgs")


# ---------------------------------------------------------------------------
# Consumers respect a BUILDDIR set only in /etc/makepkg.conf
# ---------------------------------------------------------------------------

def test_kernel_resolve_built_config_uses_conf_builddir(tmp_path, monkeypatch):
    from sysforge.pipeline.stages import kernel

    pkgbuild_dir = tmp_path / "linux-custom"
    pkgbuild_dir.mkdir()
    builddir = tmp_path / "builds"
    src = builddir / "linux-custom" / "src" / "linux"
    src.mkdir(parents=True)
    cfg = src / ".config"
    cfg.write_text("CONFIG_FOO=y\n")

    monkeypatch.delenv("BUILDDIR", raising=False)
    monkeypatch.setattr(
        config, "parse_system_makepkg_conf", lambda: {"BUILDDIR": str(builddir)}
    )
    found = kernel._resolve_built_config(pkgbuild_dir)
    assert found == cfg


def test_effective_build_dir_uses_conf_builddir(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_env

    pkgbuild_dir = tmp_path / "somepkg"
    pkgbuild_dir.mkdir()
    pkgbuild = pkgbuild_dir / "PKGBUILD"
    pkgbuild.write_text("# pkgbuild\n")
    builddir = tmp_path / "builds"
    (builddir / "somepkg" / "src").mkdir(parents=True)

    monkeypatch.delenv("BUILDDIR", raising=False)
    monkeypatch.setattr(
        config, "parse_system_makepkg_conf", lambda: {"BUILDDIR": str(builddir)}
    )
    # Profile is silent on BUILDDIR → resolver must consult the system conf.
    result = makepkg_env._effective_build_dir(pkgbuild, {})
    assert result == builddir / "somepkg"


# ---------------------------------------------------------------------------
# Built-artifact discovery honours PKGDEST (the install-from-wrong-dir bug)
# ---------------------------------------------------------------------------

def _touch_pkg(d, name):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("pkg")
    return p


def test_find_artifacts_uses_pkgdest_when_set(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "linux-custom"
    pkgbuild_dir.mkdir()
    pkgdest = tmp_path / "pkgs"
    art = _touch_pkg(pkgdest, "linux-custom-6.10-1-x86_64.pkg.tar.zst")

    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest
    )
    found = makepkg_wrapper._find_artifacts(pkgbuild_dir)
    assert found == [art]  # found in PKGDEST, not the (empty) PKGBUILD dir


def test_find_artifacts_falls_back_to_pkgbuild_dir(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "linux-custom"
    art = _touch_pkg(pkgbuild_dir, "linux-custom-6.10-1-x86_64.pkg.tar.zst")

    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: None)
    found = makepkg_wrapper._find_artifacts(pkgbuild_dir)
    assert found == [art]


def test_find_artifacts_unions_and_dedups(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "linux-custom"
    pkgdest = tmp_path / "pkgs"
    a = _touch_pkg(pkgdest, "linux-custom-6.10-1-x86_64.pkg.tar.zst")
    b = _touch_pkg(pkgbuild_dir, "linux-custom-headers-6.10-1-x86_64.pkg.tar.zst")

    monkeypatch.setattr(
        "sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest
    )
    found = set(makepkg_wrapper._find_artifacts(pkgbuild_dir))
    assert found == {a, b}


def test_effective_build_dir_falls_back_to_pkgbuild_dir(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_env

    pkgbuild_dir = tmp_path / "somepkg"
    pkgbuild_dir.mkdir()
    pkgbuild = pkgbuild_dir / "PKGBUILD"
    pkgbuild.write_text("# pkgbuild\n")

    monkeypatch.delenv("BUILDDIR", raising=False)
    monkeypatch.setattr(config, "parse_system_makepkg_conf", lambda: {})
    assert makepkg_env._effective_build_dir(pkgbuild, {}) == pkgbuild_dir
