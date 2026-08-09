# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""CLI and config surface for the source freeze (3.0.0-F2)."""
from pathlib import Path

import pytest

from sysforge import cli
from sysforge.primitives.net_policy import reset_policy

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse(argv):
    return cli._build_parser().parse_args(argv)


def test_frozen_flag_parses_as_a_global():
    assert _parse(["--frozen", "update"]).frozen is True


def test_no_frozen_flag_parses_as_a_global():
    assert _parse(["--no-frozen", "update"]).no_frozen is True


def test_thaw_is_repeatable():
    args = _parse(["--frozen", "--thaw", "mesa", "--thaw", "llvm", "update"])
    assert args.thaw == ["mesa", "llvm"]


def test_freeze_flags_hoist_from_after_the_subcommand():
    """Users put global flags wherever they like; _hoist_global_flags fixes it."""
    hoisted = cli._hoist_global_flags(["update", "--frozen", "--thaw", "mesa"])
    assert hoisted[0] == "--frozen"
    assert "--thaw" in hoisted and "mesa" in hoisted
    assert hoisted.index("--thaw") < hoisted.index("update")


def test_thaw_equals_form_hoists_with_its_value():
    hoisted = cli._hoist_global_flags(["update", "--thaw=mesa"])
    assert hoisted[0] == "--thaw=mesa"


def test_shipped_config_declares_security_section():
    import tomllib
    data = tomllib.loads(
        (_REPO_ROOT / "etc/sysforge/sysforge.toml").read_text(encoding="utf-8"))
    assert data["security"]["freeze_sources"] is False, (
        "the freeze must ship OFF — a fresh install behaves as before")


def test_fixture_config_matches_shipped():
    import tomllib
    shipped = tomllib.loads(
        (_REPO_ROOT / "etc/sysforge/sysforge.toml").read_text(encoding="utf-8"))
    fixture = tomllib.loads(
        (_REPO_ROOT / "tests/data/etc/sysforge/sysforge.toml")
        .read_text(encoding="utf-8"))
    assert "security" in fixture
    assert fixture["security"].keys() == shipped["security"].keys()


def teardown_function():
    reset_policy()


def _touch_future(path: Path) -> None:
    """Create an artifact with an mtime safely after the build loop's
    build_start, so ``snapshot_pkg_dir``'s mtime filter keeps it."""
    import os
    import time
    path.touch()
    future = time.time() + 100
    os.utime(path, (future, future))


def test_build_no_update_succeeds_under_freeze(tmp_path, monkeypatch):
    """`build --no-update` causes no egress, so the freeze must not block it.

    This is the documented way to rebuild an on-disk checkout while frozen:
    ``sync_source=False`` is exactly what ``build --no-update`` passes to
    ``build_core.build_and_install`` (see ``build_cmd.py``'s
    ``sync_source=not args.no_update``). Runs the real build engine (with the
    makepkg/install/snapshot externals faked, same as test_build_core.py)
    under an active frozen policy and proves the build actually completes —
    if the freeze wrongly reached this path it would raise ``NetworkFrozen``
    instead.
    """
    import contextlib
    from unittest.mock import patch

    from sysforge import build_core
    from sysforge.build_core import BuildTarget
    from sysforge.primitives.net_policy import NetPolicy, reset_policy, set_policy

    pkgdir = tmp_path / "foo"
    pkgdir.mkdir()
    (pkgdir / "PKGBUILD").write_text("pkgname=foo\n")
    target = BuildTarget(
        pkgbase="foo", pkgnames=["foo"], pkgbuild_path=pkgdir / "PKGBUILD")
    artifact = pkgdir / "foo-1-1-x86_64.pkg.tar.zst"

    set_policy(NetPolicy(frozen=True, thawed=frozenset()))
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sysforge.build_core.prepare_deps"))
            stack.enter_context(patch(
                "sysforge.primitives.makepkg_wrapper.run",
                side_effect=lambda *a, **k: _touch_future(artifact)))
            stack.enter_context(patch(
                "sysforge.build_core.snapshot_pkg_dir",
                return_value=frozenset({artifact})))
            stack.enter_context(patch(
                "sysforge.build_core.get_all_installed_packages",
                return_value={}))
            stack.enter_context(patch(
                "sysforge.build_core.filter_pkgs_to_installed",
                side_effect=lambda files, inst: (list(files), [])))
            stack.enter_context(patch(
                "sysforge.build_core.batch_install_pkgs",
                side_effect=lambda paths, **_kw: True))
            stack.enter_context(patch(
                "sysforge.build_core.get_pkgdest", return_value=None))
            outcome = build_core.build_and_install(
                [target], config={}, sync_source=False,
                review="off", state_dir=tmp_path / "state",
            )
        # The freeze must not have interfered: the build actually ran and
        # produced the package, exactly as it does unfrozen.
        assert outcome.built_pkgs == ["foo"]
        assert not outcome.aborted
    finally:
        reset_policy()


def test_frozen_run_exits_non_zero_with_the_package_named(update_scenario, capsys):
    """A denial is a blocker: the run raises (non-zero exit for a scripted
    caller) and the refused package's name is shown in the captured output.

    Drives the real ``cmd_update`` path via the ``update_scenario`` fixture
    (same harness Task 4's seam tests use in test_update.py) with htop's
    source sync faked to return ``STATUS_FROZEN`` — the shape a live
    ``--frozen`` run produces once the CLI in this task installs the policy
    at entry.
    """
    from types import SimpleNamespace

    from sysforge.primitives.source_sync import STATUS_FROZEN

    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=0.9.0\npkgrel=1\n")
    update_scenario.fake_sync(
        {"htop": (STATUS_FROZEN, "source freeze active — refused: htop")}
    )
    args = SimpleNamespace(
        state_dir=None, dry_run=False, devel=False, offline=False,
        no_pkg_log=True, persist_log=False, log_dir=None, profile_conf=None,
        cache_report=False, packages=None,
    )
    with pytest.raises(RuntimeError, match="htop"):
        update_scenario.run(args, installed={"htop": "0.9.0-1"}, foreign={"htop": "0.9.0-1"})
    combined = "".join(capsys.readouterr())
    assert "source freeze active" in combined
    assert "htop" in combined
