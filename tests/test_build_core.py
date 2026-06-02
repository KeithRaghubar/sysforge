#!/usr/bin/env python3
"""
Tests for sysforge.build_core — the shared build engine behind ``build`` and
``update``.

Focus areas:
  - prepare_deps: pre-installs missing repo makedeps and builds AUR/local deps,
    excluding the packages we are about to build ourselves.
  - build_and_install: invokes makepkg with -s/-i stripped (BATCH_STRIP_FLAGS)
    and force_batch so makepkg never resolves deps via pacman; snapshots built
    artifacts; bulk-installs; records failures.
  - target_from_pkgbuild: derives pkgbase/pkgnames (incl. split packages).
"""
import contextlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge import build_core
from sysforge.build_core import BuildTarget
from sysforge.primitives.pacman import BATCH_STRIP_FLAGS


def _touch_future(path: Path) -> None:
    """Create an artifact whose mtime is safely after the loop's build_start,
    so the ``snapshot_pkg_dir`` mtime filter keeps it (no wall-clock race)."""
    path.touch()
    future = time.time() + 100
    os.utime(path, (future, future))


def _make_target(tmp_path, pkgbase="foo", pkgnames=None):
    pkgdir = tmp_path / pkgbase
    pkgdir.mkdir(exist_ok=True)
    (pkgdir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    return BuildTarget(
        pkgbase=pkgbase,
        pkgnames=pkgnames or [pkgbase],
        pkgbuild_path=pkgdir / "PKGBUILD",
    )


# ---------------------------------------------------------------------------
# target_from_pkgbuild
# ---------------------------------------------------------------------------

def test_target_from_pkgbuild_single(tmp_path):
    pkgdir = tmp_path / "htop"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=htop\npkgver=3.4.1\n")
    t = build_core.target_from_pkgbuild(pkgdir / "PKGBUILD")
    assert t.pkgbase == "htop"
    assert t.pkgnames == ["htop"]


def test_target_from_pkgbuild_split(tmp_path):
    pkgdir = tmp_path / "pipewire-full-git"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text(
        "pkgbase=pipewire-full-git\n"
        "pkgname=('pipewire-full-git' 'libpipewire-full-git')\n"
    )
    t = build_core.target_from_pkgbuild(pkgdir / "PKGBUILD")
    assert t.pkgbase == "pipewire-full-git"
    assert set(t.pkgnames) == {"pipewire-full-git", "libpipewire-full-git"}


# ---------------------------------------------------------------------------
# prepare_deps
# ---------------------------------------------------------------------------

def test_prepare_deps_preinstalls_makedeps_and_builds_aur(tmp_path):
    target = _make_target(tmp_path, "proton-cachyos")
    aur_deps = [
        SimpleNamespace(name="proton-cachyos", source="aur"),  # excluded (self)
        SimpleNamespace(name="python-ufonormalizer", source="aur"),
    ]
    with (
        patch("sysforge.build_core.collect_makedeps",
              return_value=["lib32-foo", "python-ufonormalizer"]),
        patch("sysforge.build_core.filter_missing_deps",
              return_value=["lib32-foo", "python-ufonormalizer"]),
        # Only lib32-foo is in a sync repo; python-ufonormalizer is AUR-only.
        patch("sysforge.build_core.repo_packages", return_value={"lib32-foo"}),
        patch("sysforge.build_core.batch_install_makedeps") as mk_install,
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=aur_deps),
        patch("sysforge.primitives.aur_resolve.build_resolved_deps") as build_aur,
    ):
        build_core.prepare_deps(
            [target.pkgbuild_path], {},
            building_names={"proton-cachyos"},
        )

    # The AUR-only makedep must NOT reach pacman -S — only the repo subset does.
    mk_install.assert_called_once_with(["lib32-foo"])
    build_aur.assert_called_once()
    # The package we are building is excluded; the AUR-only dep is built.
    passed = build_aur.call_args.args[0]
    assert [d.name for d in passed] == ["python-ufonormalizer"]


def test_prepare_deps_excludes_aur_makedeps_from_pacman(tmp_path):
    """Regression for the proton-cachyos exit-8 failure: AUR-only makedeps must
    never be passed to ``pacman -S`` — mixing them in makes pacman abort the
    whole transaction with "target not found", installing none of the repo
    makedeps either. Only sync-repo packages reach batch_install_makedeps."""
    target = _make_target(tmp_path, "proton-cachyos")
    # As seen in the real failure: 4 repo makedeps + 2 AUR makedeps.
    missing = [
        "afdko", "fontforge", "mingw-w64-tools",
        "python-pefile", "python-setuptools-scm", "xorg-util-macros",
    ]
    repo_subset = {
        "fontforge", "python-pefile", "python-setuptools-scm", "xorg-util-macros",
    }
    with (
        patch("sysforge.build_core.collect_makedeps", return_value=missing),
        patch("sysforge.build_core.filter_missing_deps", return_value=missing),
        patch("sysforge.build_core.repo_packages", return_value=repo_subset),
        patch("sysforge.build_core.batch_install_makedeps") as mk_install,
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=[]),
        patch("sysforge.primitives.aur_resolve.build_resolved_deps"),
    ):
        build_core.prepare_deps([target.pkgbuild_path], {})

    mk_install.assert_called_once_with(sorted(repo_subset))
    # No AUR name leaked into the pacman -S transaction.
    passed = mk_install.call_args.args[0]
    assert "afdko" not in passed and "mingw-w64-tools" not in passed


def test_prepare_deps_makedep_failure_is_nonfatal(tmp_path):
    """A failed makedep pre-install warns and still resolves AUR deps —
    it must not abort the batch."""
    target = _make_target(tmp_path)
    with (
        patch("sysforge.build_core.collect_makedeps", return_value=["x"]),
        patch("sysforge.build_core.filter_missing_deps", return_value=["x"]),
        patch("sysforge.build_core.repo_packages", return_value={"x"}),
        patch("sysforge.build_core.batch_install_makedeps",
              side_effect=RuntimeError("boom")),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=[]) as resolve,
        patch("sysforge.primitives.aur_resolve.build_resolved_deps"),
    ):
        build_core.prepare_deps([target.pkgbuild_path], {})
    resolve.assert_called_once()  # reached despite the makedep failure


def test_prepare_deps_noop_on_empty():
    with patch("sysforge.build_core.collect_makedeps") as cm:
        build_core.prepare_deps([], {})
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# build_and_install
# ---------------------------------------------------------------------------

def _patch_build_env(*, run_side_effect, snapshot_return, install_capture):
    """Common patch stack for build_and_install. Returns a context manager
    list to splat into ``with``."""
    return [
        patch("sysforge.build_core.prepare_deps"),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=run_side_effect),
        patch("sysforge.build_core.snapshot_pkg_dir", return_value=snapshot_return),
        patch("sysforge.build_core.get_all_installed_packages", return_value={}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: (list(files), [])),
        patch("sysforge.build_core.batch_install_pkgs", side_effect=install_capture),
        patch("sysforge.primitives.cache_probe.reset_session"),
        patch("sysforge.primitives.cache_probe.emit_session_report"),
    ]


def test_build_and_install_strips_syncdeps_and_install_flags(tmp_path):
    """The whole point: makepkg runs with -s/--syncdeps/-i stripped and
    force_batch on, so makepkg never resolves deps via pacman itself."""
    target = _make_target(tmp_path)
    artifact = target.pkgbuild_path.parent / "foo-1-1-x86_64.pkg.tar.zst"
    seen = {}

    def fake_run(pkgbuild_path, options=None):
        seen["options"] = options
        _touch_future(artifact)

    installs = []
    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset({artifact}),
        install_capture=lambda paths: installs.append(list(paths)) or True,
    )):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=True,
        )

    opts = seen["options"]
    assert opts.strip_flags == BATCH_STRIP_FLAGS
    assert "-s" in opts.strip_flags and "-i" in opts.strip_flags
    assert opts.force_batch is True       # non-interactive
    assert opts.update is True            # sync_source threaded through
    assert outcome.built_pkgs == ["foo"]
    assert installs == [[artifact]]       # bulk install of the built artifact


def test_build_and_install_interactive_disables_force_batch(tmp_path):
    target = _make_target(tmp_path)
    artifact = target.pkgbuild_path.parent / "foo-1-1-x86_64.pkg.tar.zst"
    seen = {}

    def fake_run(pkgbuild_path, options=None):
        seen["options"] = options
        _touch_future(artifact)

    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset({artifact}),
        install_capture=lambda paths: True,
    )):
        build_core.build_and_install(
            [target], config={}, sync_source=False, interactive=True,
        )
    assert seen["options"].force_batch is False
    assert seen["options"].update is False


def test_build_and_install_records_failure(tmp_path):
    target = _make_target(tmp_path)

    def fake_run(pkgbuild_path, options=None):
        raise RuntimeError("compile blew up")

    recorded = {}
    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset(),
        install_capture=lambda paths: True,
    ) + [
        patch("sysforge.build_core._record_build_failure",
              side_effect=lambda sd, t, e: recorded.update(pkgbase=t.pkgbase)),
    ]):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False,
        )
    assert outcome.failed_pkgs == ["foo"]
    assert outcome.built_pkgs == []
    assert recorded["pkgbase"] == "foo"


def test_build_and_install_pgo_skip(tmp_path):
    from sysforge.primitives.makepkg_wrapper import PGOBuildSkipped
    target = _make_target(tmp_path)

    def fake_run(pkgbuild_path, options=None):
        raise PGOBuildSkipped("no profdata")

    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset(),
        install_capture=lambda paths: True,
    )):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False,
        )
    assert outcome.pgo_skipped_pkgs == ["foo"]
    assert outcome.failed_pkgs == []


def test_build_and_install_emits_cache_report_when_requested(tmp_path):
    target = _make_target(tmp_path)
    artifact = target.pkgbuild_path.parent / "foo-1-1-x86_64.pkg.tar.zst"

    def fake_run(pkgbuild_path, options=None):
        _touch_future(artifact)

    with (
        patch("sysforge.build_core.prepare_deps"),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_run),
        patch("sysforge.build_core.snapshot_pkg_dir", return_value=frozenset({artifact})),
        patch("sysforge.build_core.get_all_installed_packages", return_value={}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: (list(files), [])),
        patch("sysforge.build_core.batch_install_pkgs", return_value=True),
        patch("sysforge.primitives.cache_probe.reset_session"),
        patch("sysforge.primitives.cache_probe.emit_session_report") as emit,
    ):
        build_core.build_and_install(
            [target], config={}, sync_source=False, cache_report=True,
        )
    emit.assert_called_once()


def test_build_and_install_empty_targets_is_noop():
    with patch("sysforge.build_core.prepare_deps") as pd:
        outcome = build_core.build_and_install([], config={}, sync_source=False)
    pd.assert_not_called()
    assert outcome.built_pkgs == []


# ---------------------------------------------------------------------------
# install_built
# ---------------------------------------------------------------------------

def test_install_built_dedupes_and_filters(tmp_path):
    a = tmp_path / "a-1-1-x86_64.pkg.tar.zst"
    b = tmp_path / "b-1-1-x86_64.pkg.tar.zst"
    installs = []
    with (
        patch("sysforge.build_core.get_all_installed_packages", return_value={"a": "1-1"}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: ([a], [(b, "b")])),
        patch("sysforge.build_core.batch_install_pkgs",
              side_effect=lambda paths: installs.append(list(paths)) or True),
    ):
        kept, failed = build_core.install_built([a, b, a])  # duplicate a
    assert kept == [a]               # b filtered out, a deduped
    assert failed is False
    assert installs == [[a]]


def test_install_built_reports_install_failure(tmp_path):
    a = tmp_path / "a-1-1-x86_64.pkg.tar.zst"
    with (
        patch("sysforge.build_core.get_all_installed_packages", return_value={"a": "1-1"}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: (list(files), [])),
        patch("sysforge.build_core.batch_install_pkgs", return_value=False),
    ):
        _, failed = build_core.install_built([a])
    assert failed is True


# ---------------------------------------------------------------------------
# Helper: context-manager fan-out (splat a list of patches into one `with`)
# ---------------------------------------------------------------------------



@contextlib.contextmanager
def _ctx(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield
