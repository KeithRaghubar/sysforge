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


def test_artifacts_scoped_to_built_manifest(tmp_path, monkeypatch):
    """2.1.0-B9: a build-time manifest of the exact emitted basenames scopes
    the install set precisely — no stale same-name versions, no unrelated
    linux-* kernels, even though the PKGBUILD in the dir is the un-renamed
    upstream one (linux) whose pkgname prefix-matches everything."""
    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "linux"
    pkgbuild_dir.mkdir()
    # The dir holds the upstream PKGBUILD (pkgbase 'linux') after the patched
    # PKGBUILD.sysforge was cleaned up post-build.
    (pkgbuild_dir / "PKGBUILD").write_text("pkgname=linux\npkgver=7.1.2\n")
    pkgdest = tmp_path / "pkgs"
    # This build's real output (renamed -sysforge, current version):
    ours = [
        _touch_pkg(pkgdest, "linux-sysforge-7.1.2.arch3-1-x86_64.pkg.tar.zst"),
        _touch_pkg(pkgdest, "linux-sysforge-headers-7.1.2.arch3-1-x86_64.pkg.tar.zst"),
    ]
    # Stale same-name version + unrelated linux-* kernels that must NOT be swept:
    _touch_pkg(pkgdest, "linux-sysforge-7.0.12.arch1-1-x86_64.pkg.tar.zst")
    _touch_pkg(pkgdest, "linux-custom-6.10-1-x86_64.pkg.tar.zst")
    _touch_pkg(pkgdest, "linux-steam-integration-1.9-1-x86_64.pkg.tar.zst")

    (pkgbuild_dir / makepkg_wrapper._BUILT_MANIFEST_NAME).write_text(
        "\n".join(p.name for p in ours) + "\n"
    )
    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest)

    got = set(makepkg_wrapper._artifacts_for_pkgbuild(pkgbuild_dir))
    assert got == set(ours)


def test_artifacts_without_manifest_falls_back_to_pkgname_scope(tmp_path, monkeypatch):
    """No manifest (non-kernel build, or capture failed) → the existing
    pkgname scoping still applies; fall back never installs nothing."""
    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "htop"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text("pkgname=htop\npkgver=3.3\n")
    pkgdest = tmp_path / "pkgs"
    mine = _touch_pkg(pkgdest, "htop-3.3-1-x86_64.pkg.tar.zst")
    _touch_pkg(pkgdest, "nano-7.2-1-x86_64.pkg.tar.zst")

    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest)
    got = set(makepkg_wrapper._artifacts_for_pkgbuild(pkgbuild_dir))
    assert got == {mine}


def test_capture_built_manifest_writes_basenames(tmp_path, monkeypatch):
    """2.1.0-B9: capture records the basenames makepkg --packagelist prints
    (full PKGDEST paths → basenames) against the patched PKGBUILD."""
    import subprocess as _sp

    from sysforge.primitives import makepkg_wrapper

    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text("pkgname=linux-sysforge\npkgver=7.1.2\n")

    def fake_run(cmd, **kw):
        assert cmd == ["makepkg", "-p", "PKGBUILD.sysforge", "--packagelist"]
        out = (
            "/pkgs/linux-sysforge-7.1.2.arch3-1-x86_64.pkg.tar.zst\n"
            "/pkgs/linux-sysforge-headers-7.1.2.arch3-1-x86_64.pkg.tar.zst\n"
        )
        return _sp.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(makepkg_wrapper.subprocess, "run", fake_run)
    makepkg_wrapper._capture_built_manifest(patched)

    manifest = tmp_path / makepkg_wrapper._BUILT_MANIFEST_NAME
    assert manifest.read_text().split() == [
        "linux-sysforge-7.1.2.arch3-1-x86_64.pkg.tar.zst",
        "linux-sysforge-headers-7.1.2.arch3-1-x86_64.pkg.tar.zst",
    ]


def test_capture_built_manifest_best_effort_on_makepkg_failure(tmp_path, monkeypatch):
    """A non-zero makepkg leaves no sidecar (install falls back to scoping)."""
    import subprocess as _sp

    from sysforge.primitives import makepkg_wrapper

    patched = tmp_path / "PKGBUILD.sysforge"
    patched.write_text("pkgname=x\n")
    monkeypatch.setattr(
        makepkg_wrapper.subprocess, "run",
        lambda cmd, **kw: _sp.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    makepkg_wrapper._capture_built_manifest(patched)
    assert not (tmp_path / makepkg_wrapper._BUILT_MANIFEST_NAME).exists()


def test_install_built_packages_removes_manifest(tmp_path, monkeypatch):
    """2.1.0-B9: the sidecar is dropped after a successful install."""
    import subprocess as _sp

    from sysforge.primitives import makepkg_wrapper

    pkgbuild_dir = tmp_path / "linux"
    pkgbuild_dir.mkdir()
    (pkgbuild_dir / "PKGBUILD").write_text("pkgname=linux\npkgver=7.1.2\n")
    pkgdest = tmp_path / "pkgs"
    art = _touch_pkg(pkgdest, "linux-sysforge-7.1.2.arch3-1-x86_64.pkg.tar.zst")
    manifest = pkgbuild_dir / makepkg_wrapper._BUILT_MANIFEST_NAME
    manifest.write_text(art.name + "\n")

    monkeypatch.setattr("sysforge.primitives.pacman.get_pkgdest", lambda: pkgdest)
    monkeypatch.setattr(
        makepkg_wrapper.subprocess, "run",
        lambda cmd, **kw: _sp.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    installed = makepkg_wrapper.install_built_packages(pkgbuild_dir)
    assert installed == [art]
    assert not manifest.exists()


def test_effective_build_dir_falls_back_to_pkgbuild_dir(tmp_path, monkeypatch):
    from sysforge.primitives import makepkg_env

    pkgbuild_dir = tmp_path / "somepkg"
    pkgbuild_dir.mkdir()
    pkgbuild = pkgbuild_dir / "PKGBUILD"
    pkgbuild.write_text("# pkgbuild\n")

    monkeypatch.delenv("BUILDDIR", raising=False)
    monkeypatch.setattr(config, "parse_system_makepkg_conf", lambda: {})
    assert makepkg_env._effective_build_dir(pkgbuild, {}) == pkgbuild_dir
