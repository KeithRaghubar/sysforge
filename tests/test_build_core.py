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
        patch("sysforge.build_core.collect_builddeps",
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


def test_prepare_deps_threads_interactive_to_aur_dep_build(tmp_path):
    """``--interactive`` must reach AUR *dependency* builds, not just the main
    target. A package like ``gamescope-nvidia`` that pulls AUR deps otherwise
    appears non-interactive even when the user asked for interactive output."""
    target = _make_target(tmp_path, "gamescope-nvidia")
    aur_deps = [SimpleNamespace(name="some-aur-lib", source="aur")]
    with (
        patch("sysforge.build_core.collect_builddeps", return_value=["some-aur-lib"]),
        patch("sysforge.build_core.filter_missing_deps", return_value=["some-aur-lib"]),
        patch("sysforge.build_core.repo_packages", return_value=set()),
        patch("sysforge.build_core.batch_install_makedeps"),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=aur_deps),
        patch("sysforge.primitives.aur_resolve.build_resolved_deps") as build_aur,
    ):
        build_core.prepare_deps(
            [target.pkgbuild_path], {},
            building_names={"gamescope-nvidia"},
            interactive=True,
        )
    assert build_aur.call_args.kwargs.get("interactive") is True


def test_prepare_deps_defaults_dep_build_noninteractive(tmp_path):
    """Default (no ``--interactive``) keeps AUR dep builds non-interactive."""
    target = _make_target(tmp_path, "gamescope-nvidia")
    aur_deps = [SimpleNamespace(name="some-aur-lib", source="aur")]
    with (
        patch("sysforge.build_core.collect_builddeps", return_value=["some-aur-lib"]),
        patch("sysforge.build_core.filter_missing_deps", return_value=["some-aur-lib"]),
        patch("sysforge.build_core.repo_packages", return_value=set()),
        patch("sysforge.build_core.batch_install_makedeps"),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=aur_deps),
        patch("sysforge.primitives.aur_resolve.build_resolved_deps") as build_aur,
    ):
        build_core.prepare_deps(
            [target.pkgbuild_path], {},
            building_names={"gamescope-nvidia"},
        )
    assert build_aur.call_args.kwargs.get("interactive") is False


def test_build_resolved_deps_threads_interactive_into_build_options(tmp_path):
    """``build_resolved_deps(interactive=True)`` must set ``interactive`` on the
    per-dep BuildOptions handed to makepkg_wrapper.run."""
    from sysforge.primitives import aur_resolve

    dep_dir = tmp_path / "some-aur-lib"
    dep_dir.mkdir()
    (dep_dir / "PKGBUILD").write_text("pkgname=some-aur-lib\n")
    deps = [SimpleNamespace(
        name="some-aur-lib", source="aur",
        pkgbuild_path=dep_dir / "PKGBUILD",
        required_by=["gamescope-nvidia"],
    )]
    seen = {}

    def fake_run(pkgbuild_path, options=None):
        seen["options"] = options

    with patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_run):
        aur_resolve.build_resolved_deps(deps, interactive=True)
    assert seen["options"].interactive is True


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
        patch("sysforge.build_core.collect_builddeps", return_value=missing),
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


def test_prepare_deps_preinstalls_repo_runtime_depends(tmp_path):
    """Regression for the pyside6/proton-cachyos failure: the repo arm must
    pre-install repo *runtime* depends, not only makedepends. The per-package
    makepkg call runs with -s stripped, and makepkg checks runtime ``depends``
    before building (exit 8 if missing), so a repo runtime dep like ``pyside6``
    must reach ``pacman -S``. collect_builddeps returns depends+makedepends+
    checkdepends; an AUR runtime dep must still be left to the AUR arm."""
    target = _make_target(tmp_path, "proton-cachyos")
    # A repo runtime dep (pyside6) + a repo makedep (cmake) + an AUR runtime dep.
    build_deps = ["pyside6", "cmake", "python-ufonormalizer"]
    with (
        patch("sysforge.build_core.collect_builddeps", return_value=build_deps),
        patch("sysforge.build_core.filter_missing_deps", return_value=build_deps),
        # pyside6 + cmake are in sync repos; python-ufonormalizer is AUR-only.
        patch("sysforge.build_core.repo_packages",
              return_value={"pyside6", "cmake"}),
        patch("sysforge.build_core.batch_install_makedeps") as mk_install,
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=[]),
        patch("sysforge.primitives.aur_resolve.build_resolved_deps"),
    ):
        build_core.prepare_deps([target.pkgbuild_path], {})

    # The repo runtime dep is pre-installed alongside the repo makedep; the
    # AUR-only dep is not in the pacman -S transaction.
    mk_install.assert_called_once_with(["cmake", "pyside6"])
    passed = mk_install.call_args.args[0]
    assert "python-ufonormalizer" not in passed


def test_prepare_deps_makedep_failure_is_nonfatal(tmp_path):
    """A failed makedep pre-install warns and still resolves AUR deps —
    it must not abort the batch."""
    target = _make_target(tmp_path)
    with (
        patch("sysforge.build_core.collect_builddeps", return_value=["x"]),
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
    with patch("sysforge.build_core.collect_builddeps") as cm:
        assert build_core.prepare_deps([], {}) is True
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# prepare_deps — dependency review gate
# ---------------------------------------------------------------------------

def _dep_gate_patches(aur_deps):
    """Patch stack for the dep-gate tests: no repo deps, canned AUR deps."""
    return [
        patch("sysforge.build_core.collect_builddeps", return_value=[]),
        patch("sysforge.build_core.filter_missing_deps", return_value=[]),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch",
              return_value=aur_deps),
    ]


def test_prepare_deps_review_abort_returns_false(tmp_path):
    """A dep-gate abort stops before any dep build and reports False."""
    target = _make_target(tmp_path)
    dep = SimpleNamespace(
        name="libdep", source="aur",
        pkgbuild_path=tmp_path / "libdep" / "PKGBUILD",
    )
    with contextlib.ExitStack() as stack:
        for p in _dep_gate_patches([dep]):
            stack.enter_context(p)
        build_aur = stack.enter_context(
            patch("sysforge.primitives.aur_resolve.build_resolved_deps"))
        rd = stack.enter_context(
            patch("sysforge.build_core.review_deps",
                  return_value=build_core.DECISION_ABORT))
        proceed = build_core.prepare_deps(
            [target.pkgbuild_path], {},
            state_dir=tmp_path / "state", review="prompt",
        )
    assert proceed is False
    rd.assert_called_once()
    build_aur.assert_not_called()


def test_prepare_deps_review_accept_builds_deps(tmp_path):
    """Accept (or clean) proceeds to build_resolved_deps; the gate sees the
    recorded reviewed_commit and the prompt-mode interactive flag."""
    from sysforge.primitives.build_state import BuildState
    state_dir = tmp_path / "state"
    bs = BuildState(state_dir)
    bs.record(pkgname="libdep", pkgver="1", pkgrel="1", epoch="0",
              pkgbase="libdep", pkgbuild_dir=tmp_path / "libdep",
              build_mode="source_built", reviewed_commit="dep123")
    bs.save()
    target = _make_target(tmp_path)
    dep = SimpleNamespace(
        name="libdep", source="aur",
        pkgbuild_path=tmp_path / "libdep" / "PKGBUILD",
    )
    with contextlib.ExitStack() as stack:
        for p in _dep_gate_patches([dep]):
            stack.enter_context(p)
        build_aur = stack.enter_context(
            patch("sysforge.primitives.aur_resolve.build_resolved_deps"))
        rd = stack.enter_context(
            patch("sysforge.build_core.review_deps", return_value="accept"))
        proceed = build_core.prepare_deps(
            [target.pkgbuild_path], {},
            state_dir=state_dir, review="prompt",
        )
    assert proceed is True
    build_aur.assert_called_once()
    (entries,), kwargs = rd.call_args
    assert entries == [("libdep", tmp_path / "libdep", "dep123")]
    assert kwargs == {"interactive": True}


def test_prepare_deps_review_auto_passes_interactive_false(tmp_path):
    """update's auto mode consults the dep gate with interactive=False."""
    target = _make_target(tmp_path)
    dep = SimpleNamespace(
        name="libdep", source="aur",
        pkgbuild_path=tmp_path / "libdep" / "PKGBUILD",
    )
    with contextlib.ExitStack() as stack:
        for p in _dep_gate_patches([dep]):
            stack.enter_context(p)
        stack.enter_context(
            patch("sysforge.primitives.aur_resolve.build_resolved_deps"))
        rd = stack.enter_context(
            patch("sysforge.build_core.review_deps", return_value="accept"))
        build_core.prepare_deps(
            [target.pkgbuild_path], {},
            state_dir=tmp_path / "state", review="auto",
        )
    assert rd.call_args.kwargs == {"interactive": False}


def test_prepare_deps_review_off_never_consults_gate(tmp_path):
    target = _make_target(tmp_path)
    dep = SimpleNamespace(
        name="libdep", source="aur",
        pkgbuild_path=tmp_path / "libdep" / "PKGBUILD",
    )
    with contextlib.ExitStack() as stack:
        for p in _dep_gate_patches([dep]):
            stack.enter_context(p)
        stack.enter_context(
            patch("sysforge.primitives.aur_resolve.build_resolved_deps"))
        rd = stack.enter_context(patch("sysforge.build_core.review_deps"))
        proceed = build_core.prepare_deps(
            [target.pkgbuild_path], {},
            state_dir=tmp_path / "state",  # review defaults to "off"
        )
    assert proceed is True
    rd.assert_not_called()


def test_build_and_install_dep_review_abort_propagates(tmp_path):
    """A False from prepare_deps surfaces as outcome.aborted with nothing
    built or installed — same clean-return contract as the target gate."""
    target = _make_target(tmp_path)
    with contextlib.ExitStack() as stack:
        for p in _patch_build_env(
            run_side_effect=lambda *a, **k: None,
            snapshot_return=[],
            install_capture=lambda files: True,
        ):
            stack.enter_context(p)
        stack.enter_context(
            patch("sysforge.build_core.prepare_deps", return_value=False))
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False,
            review="auto", state_dir=tmp_path / "state",
        )
    assert outcome.aborted
    assert outcome.built_pkgs == [] and outcome.built_pkg_files == []


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
        # Isolate from the host's makepkg.conf — a real PKGDEST would
        # redirect every search_dir away from the tmp_path PKGBUILD dirs.
        patch("sysforge.build_core.get_pkgdest", return_value=None),
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
        install_capture=lambda paths, **_kw: installs.append(list(paths)) or True,
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
        install_capture=lambda paths, **_kw: True,
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
        install_capture=lambda paths, **_kw: True,
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
        install_capture=lambda paths, **_kw: True,
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


def test_build_and_install_records_phase_timings(tmp_path):
    """The outcome carries dep-prep / per-package / install phase records."""
    target = _make_target(tmp_path)
    artifact = target.pkgbuild_path.parent / "foo-1-1-x86_64.pkg.tar.zst"

    def fake_run(pkgbuild_path, options=None):
        _touch_future(artifact)

    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset({artifact}),
        install_capture=lambda paths, **_kw: True,
    )):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False,
        )

    assert [r.name for r in outcome.phase_records] == [
        "dep prep", "build: foo", "install",
    ]
    assert all(r.duration_ms >= 0 for r in outcome.phase_records)


def test_build_and_install_accumulates_on_caller_timer(tmp_path):
    """A caller-supplied PhaseTimer (the update path) collects the records."""
    from sysforge.primitives.timing import PhaseRecord, PhaseTimer

    target = _make_target(tmp_path)
    artifact = target.pkgbuild_path.parent / "foo-1-1-x86_64.pkg.tar.zst"

    def fake_run(pkgbuild_path, options=None):
        _touch_future(artifact)

    timer = PhaseTimer(records=[PhaseRecord("source sync", 5)])
    with _ctx(_patch_build_env(
        run_side_effect=fake_run,
        snapshot_return=frozenset({artifact}),
        install_capture=lambda paths, **_kw: True,
    )):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False, timer=timer,
        )

    assert [r.name for r in timer.records] == [
        "source sync", "dep prep", "build: foo", "install",
    ]
    # outcome aliases the same records list
    assert outcome.phase_records is timer.records


# ---------------------------------------------------------------------------
# Intra-batch dependency ordering + just-in-time install
# ---------------------------------------------------------------------------

def _write_target(tmp_path, pkgbase, body):
    """Target whose PKGBUILD has real depends/provides content — the ordering
    pass re-parses the file, so the text must carry the arrays."""
    pkgdir = tmp_path / pkgbase
    pkgdir.mkdir(exist_ok=True)
    (pkgdir / "PKGBUILD").write_text(body)
    return build_core.target_from_pkgbuild(pkgdir / "PKGBUILD")


def _ordered_build_env(events):
    """Patch stack that records ("build", pkgbase) / ("install", [filenames])
    events in execution order, with per-target artifact dirs (no shared
    snapshot, unlike _patch_build_env)."""
    def fake_run(pkgbuild_path, options=None):
        base = Path(pkgbuild_path).parent.name
        events.append(("build", base))
        _touch_future(
            Path(pkgbuild_path).parent / f"{base}-1-1-x86_64.pkg.tar.zst"
        )

    def fake_install(paths, **_kw):
        events.append(("install", sorted(p.name for p in paths)))
        return True

    return [
        patch("sysforge.build_core.prepare_deps"),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_run),
        patch("sysforge.build_core.snapshot_pkg_dir",
              side_effect=lambda d: frozenset(Path(d).glob("*.pkg.tar*"))),
        patch("sysforge.build_core.get_all_installed_packages", return_value={}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: (list(files), [])),
        patch("sysforge.build_core.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.build_core.get_pkgdest", return_value=None),
        patch("sysforge.primitives.cache_probe.reset_session"),
        patch("sysforge.primitives.cache_probe.emit_session_report"),
    ]


def test_intra_batch_dep_builds_and_installs_before_dependent(tmp_path):
    """Alphabetical-adversarial: 'aa-loader' makedepends on 'zz-headers',
    which sorts after it. The dep must build first AND be installed before
    the dependent's build starts (the Vulkan 1.4.354 stale-headers failure:
    deferred bulk install left the sibling configuring against the old
    installed version)."""
    loader = _write_target(
        tmp_path, "aa-loader",
        "pkgname=aa-loader\nmakedepends=('zz-headers' 'cmake')\n",
    )
    headers = _write_target(tmp_path, "zz-headers", "pkgname=zz-headers\n")

    events = []
    with _ctx(_ordered_build_env(events)):
        outcome = build_core.build_and_install(
            [loader, headers], config={}, sync_source=False,
        )

    assert events == [
        ("build", "zz-headers"),
        ("install", ["zz-headers-1-1-x86_64.pkg.tar.zst"]),
        ("build", "aa-loader"),
        ("install", ["aa-loader-1-1-x86_64.pkg.tar.zst"]),
    ]
    assert outcome.built_pkgs == ["zz-headers", "aa-loader"]
    assert outcome.install_failed is False
    # Each artifact reported exactly once (JIT-installed files are not
    # re-installed by the final bulk pass).
    assert sorted(p.name for p in outcome.built_pkg_files) == [
        "aa-loader-1-1-x86_64.pkg.tar.zst",
        "zz-headers-1-1-x86_64.pkg.tar.zst",
    ]


def test_intra_batch_edge_via_provides_with_version_constraint(tmp_path):
    """The dep name usually targets a *provides* of the sibling (vulkan-headers
    is provided by vulkan-headers-git), and carries a version constraint."""
    loader = _write_target(
        tmp_path, "aa-loader-git",
        "pkgname=aa-loader-git\nmakedepends=('vulkan-headers>=1.4.354')\n",
    )
    headers = _write_target(
        tmp_path, "zz-headers-git",
        "pkgname=zz-headers-git\nprovides=('vulkan-headers=1.4.354')\n",
    )

    events = []
    with _ctx(_ordered_build_env(events)):
        outcome = build_core.build_and_install(
            [loader, headers], config={}, sync_source=False,
        )

    assert [e for e in events if e[0] == "build"] == [
        ("build", "zz-headers-git"), ("build", "aa-loader-git"),
    ]
    assert outcome.built_pkgs == ["zz-headers-git", "aa-loader-git"]


def test_intra_batch_edge_via_soname_provides(tmp_path):
    """Soname deps (libvulkan.so) match a sibling's soname provides — the
    vulkan-validation-layers → vulkan-icd-loader edge."""
    layers = _write_target(
        tmp_path, "aa-layers-git",
        "pkgname=aa-layers-git\ndepends=('libvulkan.so')\n",
    )
    icd = _write_target(
        tmp_path, "zz-icd-git",
        "pkgname=zz-icd-git\nprovides=('libvulkan.so')\n",
    )

    events = []
    with _ctx(_ordered_build_env(events)):
        outcome = build_core.build_and_install(
            [layers, icd], config={}, sync_source=False,
        )

    assert outcome.built_pkgs == ["zz-icd-git", "aa-layers-git"]


def test_intra_batch_cycle_warns_and_keeps_original_order(tmp_path):
    a = _write_target(
        tmp_path, "aa-pkg", "pkgname=aa-pkg\ndepends=('zz-pkg')\n",
    )
    z = _write_target(
        tmp_path, "zz-pkg", "pkgname=zz-pkg\ndepends=('aa-pkg')\n",
    )

    events = []
    with _ctx(_ordered_build_env(events)):
        outcome = build_core.build_and_install(
            [a, z], config={}, sync_source=False,
        )

    # Original order preserved on a cycle; both still build.
    assert [e for e in events if e[0] == "build"] == [
        ("build", "aa-pkg"), ("build", "zz-pkg"),
    ]
    assert outcome.built_pkgs == ["aa-pkg", "zz-pkg"]


def test_intra_batch_no_edges_keeps_order_and_single_install(tmp_path):
    """Regression guard: unrelated targets keep their order and get exactly
    one (final) bulk install — no JIT churn."""
    targets = [
        _write_target(tmp_path, name, f"pkgname={name}\n")
        for name in ("ccc", "aaa", "bbb")
    ]

    events = []
    with _ctx(_ordered_build_env(events)):
        outcome = build_core.build_and_install(
            targets, config={}, sync_source=False,
        )

    assert events == [
        ("build", "ccc"),
        ("build", "aaa"),
        ("build", "bbb"),
        ("install", [
            "aaa-1-1-x86_64.pkg.tar.zst",
            "bbb-1-1-x86_64.pkg.tar.zst",
            "ccc-1-1-x86_64.pkg.tar.zst",
        ]),
    ]
    assert outcome.built_pkgs == ["ccc", "aaa", "bbb"]


def test_intra_batch_failed_dep_dependent_still_builds(tmp_path):
    """A failed intra-batch dep only warns — the dependent builds against the
    installed version and any failure of its own is recorded normally."""
    loader = _write_target(
        tmp_path, "aa-loader",
        "pkgname=aa-loader\nmakedepends=('zz-headers')\n",
    )
    headers = _write_target(tmp_path, "zz-headers", "pkgname=zz-headers\n")

    events = []
    env = _ordered_build_env(events)

    def failing_run(pkgbuild_path, options=None):
        base = Path(pkgbuild_path).parent.name
        events.append(("build", base))
        if base == "zz-headers":
            raise RuntimeError("compile blew up")
        _touch_future(
            Path(pkgbuild_path).parent / f"{base}-1-1-x86_64.pkg.tar.zst"
        )

    env[1] = patch(
        "sysforge.primitives.makepkg_wrapper.run", side_effect=failing_run
    )
    with _ctx(env + [
        patch("sysforge.build_core._record_build_failure"),
    ]):
        outcome = build_core.build_and_install(
            [loader, headers], config={}, sync_source=False,
        )

    assert outcome.failed_pkgs == ["zz-headers"]
    assert outcome.built_pkgs == ["aa-loader"]
    # No artifact from the failed dep: only the dependent's final install.
    assert events == [
        ("build", "zz-headers"),
        ("build", "aa-loader"),
        ("install", ["aa-loader-1-1-x86_64.pkg.tar.zst"]),
    ]


def test_build_and_install_resolves_system_pkgdest(tmp_path):
    """When the system makepkg.conf sets PKGDEST, artifacts land there — the
    snapshot (and hence JIT + final install) must search it even when the
    caller doesn't pass ``pkgdest`` (the `build` verb doesn't; only `update`
    resolved it, so `sysforge build` silently installed nothing)."""
    target = _make_target(tmp_path, "foo")
    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    artifact = pkgdest / "foo-1-1-x86_64.pkg.tar.zst"

    snapshot_dirs = []

    def fake_run(pkgbuild_path, options=None):
        _touch_future(artifact)

    installs = []
    with _ctx([
        patch("sysforge.build_core.prepare_deps"),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_run),
        patch("sysforge.build_core.snapshot_pkg_dir",
              side_effect=lambda d: snapshot_dirs.append(Path(d))
              or frozenset(Path(d).glob("*.pkg.tar*"))),
        patch("sysforge.build_core.get_all_installed_packages", return_value={}),
        patch("sysforge.build_core.filter_pkgs_to_installed",
              side_effect=lambda files, inst: (list(files), [])),
        patch("sysforge.build_core.batch_install_pkgs",
              side_effect=lambda paths, **_kw: installs.append(list(paths)) or True),
        patch("sysforge.build_core.get_pkgdest", return_value=pkgdest),
        patch("sysforge.primitives.cache_probe.reset_session"),
        patch("sysforge.primitives.cache_probe.emit_session_report"),
    ]):
        outcome = build_core.build_and_install(
            [target], config={}, sync_source=False,
        )

    assert snapshot_dirs == [pkgdest]          # searched PKGDEST, not srcdir
    assert installs == [[artifact]]
    assert outcome.built_pkgs == ["foo"]


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
              side_effect=lambda paths, **_kw: installs.append(list(paths)) or True),
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


def test_install_built_installs_requested_fresh_pkg(tmp_path):
    """Regression for ``build`` not installing a freshly-built package: a
    pkgname the user explicitly asked to build must be installed even when it is
    not yet on the system. With a real filter_pkgs_to_installed and nothing
    installed, the artifact is dropped by default but kept via always_install."""
    from sysforge.primitives.pacman import filter_pkgs_to_installed

    art = tmp_path / "proton-cachyos-1-1-x86_64.pkg.tar.zst"
    art.touch()
    installs = []
    patches = [
        patch("sysforge.build_core.get_all_installed_packages", return_value={}),
        patch("sysforge.build_core.filter_pkgs_to_installed", filter_pkgs_to_installed),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="proton-cachyos"),
        patch("sysforge.build_core.batch_install_pkgs",
              side_effect=lambda paths, **_kw: installs.append(list(paths)) or True),
    ]
    # Default: a not-yet-installed package is dropped (split-pkgbase safety).
    with _ctx(patches):
        kept, _ = build_core.install_built([art])
    assert kept == [] and installs == []

    # Explicitly requested: installed even though not on the system.
    installs.clear()
    with _ctx(patches):
        kept, _ = build_core.install_built(
            [art], always_install={"proton-cachyos"},
        )
    assert kept == [art] and installs == [[art]]


# ---------------------------------------------------------------------------
# Helper: context-manager fan-out (splat a list of patches into one `with`)
# ---------------------------------------------------------------------------



@contextlib.contextmanager
def _ctx(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


# ---------------------------------------------------------------------------
# make_build_options — shared BuildOptions factory for pipeline stages (P1b)
# ---------------------------------------------------------------------------
#
# These lock in field-for-field parity with the BuildOptions each stage used to
# hand-assemble, so the factory extraction stays behavior-preserving and a
# future field move is caught here rather than at runtime.

from sysforge.build_core import make_build_options  # noqa: E402
from sysforge.pipeline.stages.base import RunOptions  # noqa: E402


def _run_options(**kw):
    """A RunOptions with distinctive non-default values for the four fields the
    factory maps, so a dropped/renamed mapping shows up as a wrong value."""
    base = dict(
        no_pkg_logs=True,            # → pkg_log=False
        persist_log=True,
        state_dir=Path("/tmp/sf-state-parity"),
        abi_check=True,
        log_dir=Path("/tmp/sf-logs"),
    )
    base.update(kw)
    return RunOptions(**base)


def test_make_build_options_kernel_parity():
    opts = _run_options()
    bo = make_build_options(
        "kernel", opts,
        log_dir=opts.log_dir,
        profile_conf="prof.toml",
        update=False,
        interactive=True,
        cc_override="/usr/bin/clang",
        cxx_override="/usr/bin/clang++",
        source="local",
        toolchain_variant="pgo_llvm",
    )
    # Common mapping
    assert bo.pkg_log is False          # not no_pkg_logs (True)
    assert bo.persist_log is True
    assert bo.state_dir == Path("/tmp/sf-state-parity")
    assert bo.abi_check is True
    # Kernel stage constants
    assert bo.owner_stage == "kernel"
    assert bo.no_install is True
    # Per-call overrides
    assert bo.update is False
    assert bo.interactive is True
    assert bo.cc_override == "/usr/bin/clang"
    assert bo.source == "local"
    assert bo.toolchain_variant == "pgo_llvm"
    assert bo.log_dir == Path("/tmp/sf-logs")
    assert bo.profile_conf == "prof.toml"


def test_make_build_options_toolchain_parity():
    opts = _run_options()
    bo = make_build_options(
        "toolchain", opts,
        extra_flags=["-x"],
        compiler_flags_extra="-O3",
        linker_flags_extra="-fuse-ld=lld",
        cc_override="cc",
        cxx_override="cxx",
        init_session=False,
        update=True,
        strip_full_lto=True,
        extra_env={"K": "V"},
        strip_flags=frozenset({"-i"}),
        toolchain_variant="pgo_llvm",
        owner_stage="toolchain",
    )
    assert bo.pkg_log is False
    assert bo.persist_log is True
    assert bo.state_dir == Path("/tmp/sf-state-parity")
    assert bo.abi_check is True
    # Toolchain stage constant
    assert bo.pgo_managed is True
    # Toolchain does NOT set log_dir / profile_conf / source / no_install —
    # they must keep their BuildOptions defaults.
    assert bo.log_dir is None
    assert bo.profile_conf is None
    assert bo.source is None
    assert bo.no_install is False
    # owner_stage override wins
    assert bo.owner_stage == "toolchain"
    assert bo.pgo_managed is True
    assert bo.strip_full_lto is True
    assert bo.extra_env == {"K": "V"}


def test_make_build_options_packages_parity():
    opts = _run_options()
    bo = make_build_options(
        "packages", opts,
        log_dir=opts.log_dir,
        profile_conf="p.toml",
        update=True,
        cc_override="cc",
        cxx_override="cxx",
        ld_override="ld",
        source="aur",
        toolchain_variant="gcc",
    )
    assert bo.pkg_log is False
    assert bo.persist_log is True
    assert bo.state_dir == Path("/tmp/sf-state-parity")
    assert bo.abi_check is True
    # packages has no stage constants — these stay default
    assert bo.owner_stage is None
    assert bo.no_install is False
    assert bo.pgo_managed is False
    assert bo.init_session is True      # not set → default
    assert bo.ld_override == "ld"
    assert bo.source == "aur"


def test_make_build_options_override_wins_over_stage_default():
    """An explicit override beats the stage constant (e.g. kernel building a
    package it does install, or stamping a different owner)."""
    opts = _run_options()
    bo = make_build_options("kernel", opts, no_install=False, owner_stage="other")
    assert bo.no_install is False
    assert bo.owner_stage == "other"


def test_make_build_options_abi_check_defaults_false_when_absent():
    """A run-options object lacking abi_check degrades to False, not error."""
    opts = SimpleNamespace(no_pkg_logs=False, persist_log=False, state_dir=None)
    bo = make_build_options("packages", opts)
    assert bo.abi_check is False
    assert bo.pkg_log is True


def test_make_build_options_unknown_stage_raises():
    opts = _run_options()
    try:
        make_build_options("nonesuch", opts)
    except ValueError as e:
        assert "nonesuch" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown stage")


# ---------------------------------------------------------------------------
# build_and_install — PKGBUILD review gate
# ---------------------------------------------------------------------------

from sysforge.build_core import DECISION_ABORT, DECISION_SKIP  # noqa: E402
from sysforge.primitives.build_state import BuildState  # noqa: E402


def _run_gated(targets, *, decisions=None, review="prompt", state_dir=None,
               run_capture=None):
    """Drive build_and_install with the gate decision function stubbed and
    the build/install externals from _patch_build_env neutralised."""
    calls = []

    def _fake_run(pkgbuild_path, options=None):
        calls.append(Path(pkgbuild_path))

    def _decide(pkgbase, pkgbuild_dir, reviewed_commit, interactive=True):
        if run_capture is not None:
            run_capture.append((pkgbase, reviewed_commit, interactive))
        return decisions[pkgbase]

    with contextlib.ExitStack() as stack:
        for p in _patch_build_env(
            run_side_effect=_fake_run,
            snapshot_return=[],
            install_capture=lambda files: True,
        ):
            stack.enter_context(p)
        rt = stack.enter_context(
            patch("sysforge.build_core.review_target",
                  side_effect=_decide if decisions is not None else None))
        outcome = build_core.build_and_install(
            targets, config={}, sync_source=False,
            review=review, state_dir=state_dir,
        )
    return outcome, calls, rt


def test_review_gate_skip_drops_target_keeps_rest(tmp_path):
    foo = _make_target(tmp_path, "foo")
    bar = _make_target(tmp_path, "bar")
    outcome, calls, _ = _run_gated(
        [foo, bar],
        decisions={"foo": DECISION_SKIP, "bar": "accept"},
        state_dir=tmp_path / "state",
    )
    assert outcome.review_skipped == ["foo"]
    assert not outcome.aborted
    assert calls == [bar.pkgbuild_path]  # only bar reached makepkg


def test_review_gate_abort_builds_nothing(tmp_path):
    foo = _make_target(tmp_path, "foo")
    bar = _make_target(tmp_path, "bar")
    outcome, calls, _ = _run_gated(
        [foo, bar],
        decisions={"foo": DECISION_ABORT, "bar": "accept"},
        state_dir=tmp_path / "state",
    )
    assert outcome.aborted
    assert calls == []  # nothing built
    assert outcome.built_pkgs == [] and outcome.built_pkg_files == []


def test_review_gate_all_skipped_short_circuits(tmp_path):
    foo = _make_target(tmp_path, "foo")
    outcome, calls, _ = _run_gated(
        [foo],
        decisions={"foo": DECISION_SKIP},
        state_dir=tmp_path / "state",
    )
    assert outcome.review_skipped == ["foo"]
    assert calls == []


def test_review_disabled_never_consults_gate(tmp_path):
    foo = _make_target(tmp_path, "foo")
    outcome, calls, rt = _run_gated(
        [foo], decisions=None, review="off", state_dir=tmp_path / "state",
    )
    rt.assert_not_called()
    assert calls == [foo.pkgbuild_path]


def test_review_auto_mode_passes_interactive_false(tmp_path):
    """review="auto" (the `update` default) consults the gate with
    interactive=False so changes auto-accept instead of prompting."""
    foo = _make_target(tmp_path, "foo")
    seen = []
    outcome, calls, _ = _run_gated(
        [foo],
        decisions={"foo": "accept"},
        review="auto",
        state_dir=tmp_path / "state",
        run_capture=seen,
    )
    assert seen == [("foo", None, False)]
    assert calls == [foo.pkgbuild_path]


def test_review_gate_passes_recorded_reviewed_commit(tmp_path):
    """The gate looks up reviewed_commit from build_state by pkgname."""
    state_dir = tmp_path / "state"
    bs = BuildState(state_dir)
    bs.record(pkgname="foo", pkgver="1", pkgrel="1", epoch="0",
              pkgbase="foo", pkgbuild_dir=tmp_path / "foo",
              build_mode="source_built", reviewed_commit="abc123")
    bs.save()
    seen = []
    _run_gated(
        [_make_target(tmp_path, "foo")],
        decisions={"foo": "accept"},
        state_dir=state_dir,
        run_capture=seen,
    )
    assert seen == [("foo", "abc123", True)]
