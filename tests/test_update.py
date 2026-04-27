"""
test_update.py — unit tests for sysforge.update

All subprocess calls (pacman -Q, git pull, makepkg) and filesystem access to
the state dir are mocked so no real system state is required.

Iteration model under test: the live install set (`pacman -Qm` + repo
packages selected by overrides). packages.toml entries are overrides only;
override entries with no installed counterpart are inert and silently
skipped (no NOT_INSTALLED action).
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update import (
    _is_vcs, cmd_update,
    _check_one_pkgbase, _UpdateResult,
)
from sysforge.primitives.pacman import get_installed_version, get_foreign_packages


# ---------------------------------------------------------------------------
# _is_vcs
# ---------------------------------------------------------------------------

def test_is_vcs_git():
    assert _is_vcs("neovim-git")

def test_is_vcs_svn():
    assert _is_vcs("foo-svn")

def test_is_vcs_hg():
    assert _is_vcs("bar-hg")

def test_is_vcs_bzr():
    assert _is_vcs("baz-bzr")

def test_is_vcs_false():
    assert not _is_vcs("htop")
    assert not _is_vcs("llvm")
    assert not _is_vcs("python-requests")


# ---------------------------------------------------------------------------
# get_installed_version
# ---------------------------------------------------------------------------

def _mock_pacman(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_installed_version_found():
    with patch("subprocess.run", return_value=_mock_pacman("htop 3.3.0-1\n")):
        assert get_installed_version("htop") == "3.3.0-1"


def test_get_installed_version_not_installed():
    with patch("subprocess.run", return_value=_mock_pacman("", returncode=1)):
        assert get_installed_version("htop") is None


# ---------------------------------------------------------------------------
# cmd_update — helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        state_dir=None,
        dry_run=False,
        devel=False,
        offline=True,  # skip network in most tests
        no_pkg_log=True,
        persist_log=False,
        log_dir=None,
        profile_conf=None,
        cache_report=False,
        packages=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# cmd_update — empty state
# ---------------------------------------------------------------------------

def test_empty_install_set_exits_cleanly(capsys):
    """No foreign packages and no repo-source overrides → nothing in scope."""
    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update._load_overrides", return_value=({}, {})), \
         patch("sysforge.update.get_all_installed_packages", return_value={}), \
         patch("sysforge.update.get_foreign_packages", return_value={}):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(_make_args())
    captured = capsys.readouterr()
    assert "No installed packages in scope" in captured.err


# ---------------------------------------------------------------------------
# cmd_update — version checks
# ---------------------------------------------------------------------------

def _run_update_with_package(tmp_path, pkgbase, pkgver_installed, pkgver_pkgbuild,
                              args_extra=None):
    """
    Helper: set up a fake PKGBUILD in tmp_path, a build state entry, and run cmd_update.
    Returns list of _UpdateResult.
    """
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text(f"pkgname={pkgbase}\npkgver={pkgver_pkgbuild}\npkgrel=1\n")

    state_data = {
        pkgbase: {
            "pkgver": pkgver_installed.split("-")[0],
            "pkgrel": pkgver_installed.split("-")[1] if "-" in pkgver_installed else "1",
            "epoch": "0",
            "pkgbase": pkgbase,
            "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }

    args = _make_args(**(args_extra or {}))

    parsed_globals = {
        "pkgname": pkgbase,
        "pkgver": pkgver_pkgbuild,
        "pkgrel": "1",
        "epoch": "0",
    }

    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "aur"}})

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value={"globals": parsed_globals}),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: pkgver_installed}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: pkgver_installed}),
        patch("sysforge.update.vercmp") as mock_vercmp,
    ):
        MockBS.return_value.all_packages.return_value = state_data
        # vercmp: 1 if pkgbuild > installed, 0 if equal, -1 if pkgbuild < installed
        pkgbuild_ver = f"{pkgver_pkgbuild}-1"
        mock_vercmp.side_effect = lambda a, b: 1 if a == pkgbuild_ver and a != b else (0 if a == b else -1)

        results = []
        orig_print_summary = __import__("sysforge.update", fromlist=["_print_summary"])._print_summary

        def capture_summary(res_list, a):
            results.extend(res_list)
            orig_print_summary(res_list, a)

        with patch("sysforge.update._print_summary", side_effect=capture_summary):
            cmd_update(args)

    return results


def test_check_needs_rebuild(tmp_path, capsys):
    results = _run_update_with_package(tmp_path, "htop", "3.3.0-1", "3.4.1")
    actions = [r.action for r in results]
    assert "NEEDS_REBUILD" in actions


def test_check_up_to_date(tmp_path, capsys):
    results = _run_update_with_package(tmp_path, "htop", "3.4.1-1", "3.4.1")
    actions = [r.action for r in results]
    assert "UP_TO_DATE" in actions


# ---------------------------------------------------------------------------
# _check_one_pkgbase — RPC version fallback for unresolvable bash expansions
# ---------------------------------------------------------------------------

def _write_pkgbuild(dir_: Path, body: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "PKGBUILD"
    p.write_text(body)
    return p


def test_unresolved_pkgver_uses_cached_rpc_version(tmp_path):
    """PKGBUILD with bash parameter expansion falls back to cached RPC version."""
    pkgbase = "1password"
    pkg_dir = tmp_path / pkgbase
    _write_pkgbuild(
        pkg_dir,
        '_tarver=8.12.10-36\npkgname=1password\npkgver=${_tarver//-/_}\npkgrel=36\n',
    )
    entry = {"pkgbuild_dir": str(pkg_dir)}

    result = _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry=entry,
        sync_failures={},
        all_installed={pkgbase: "8.12.10-36"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={pkgbase: "8.12.10-36"},
    )
    assert result is not None
    assert result.action == "UP_TO_DATE"
    assert result.pkgbuild_ver == "8.12.10-36"


def test_unresolved_pkgver_without_cache_is_skipped(tmp_path):
    """No cached RPC version → skip rather than compare gibberish."""
    pkgbase = "openssl-1.0"
    pkg_dir = tmp_path / pkgbase
    _write_pkgbuild(
        pkg_dir,
        '_ver=1.0.2u\npkgname=openssl-1.0\npkgver=${_ver/[a-z]/.${_ver//[0-9.]/}}\npkgrel=7\n',
    )
    entry = {"pkgbuild_dir": str(pkg_dir)}

    result = _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry=entry,
        sync_failures={},
        all_installed={pkgbase: "1.0.2.u-7"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
    )
    assert result is None


# ---------------------------------------------------------------------------
# Live-install-set iteration: override entries for uninstalled packages are
# silently ignored (no NOT_INSTALLED action under the new model).
# ---------------------------------------------------------------------------

def test_uninstalled_override_is_silently_skipped(tmp_path, capsys):
    """An override entry for a package that isn't installed is inert — not
    iterated, no NOT_INSTALLED action emitted, no source sync attempted."""
    pkg_dir = tmp_path / "mesa-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=mesa-git\npkgver=1\npkgrel=1\n")

    # mesa (repo) is installed; mesa-git (override) is not.
    overrides = ({}, {"mesa-git": {"name": "mesa-git", "source": "aur"}})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={"mesa": "1:25.3.1-1"}),
        patch("sysforge.update.get_foreign_packages", return_value={}),
        patch("sysforge.update._sync_sources", return_value={}) as mock_sync,
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    actions = [r.action for r in results]
    assert "NOT_INSTALLED" not in actions
    assert "mesa-git" not in {r.pkgbase for r in results}
    sync_map = mock_sync.call_args.args[0] if mock_sync.call_args else {}
    assert "mesa-git" not in sync_map


def test_installed_aur_without_override_uses_defaults(tmp_path):
    """AUR package installed but with no override entry → walked with defaults."""
    pkgbase = "yay"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\npkgver=12.3.3\npkgrel=1\n")

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),  # no overrides
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "12.3.3-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "12.3.3-1"}),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert {r.pkgbase for r in results} == {pkgbase}


def test_repo_package_without_override_is_not_iterated(tmp_path):
    """A repo (non-foreign) package with no override → out of scope."""
    overrides = ({}, {})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        # mesa is installed (a repo package), no foreign packages.
        patch("sysforge.update.get_all_installed_packages",
              return_value={"mesa": "1:25.3.1-1"}),
        patch("sysforge.update.get_foreign_packages", return_value={}),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert {r.pkgbase for r in results} == set()


def test_repo_package_with_override_is_iterated(tmp_path):
    """A repo package WITH a `source=repo` override → walked."""
    pkgbase = "llvm"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\npkgver=20.1.0\npkgrel=1\n")

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "20.1.0", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "repo", "cache": False}})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        # llvm is installed via repo (not foreign), but the override pulls it in.
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "20.1.0-1"}),
        patch("sysforge.update.get_foreign_packages", return_value={}),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert {r.pkgbase for r in results} == {pkgbase}


# ---------------------------------------------------------------------------
# DEVEL / dry-run / no-devel
# ---------------------------------------------------------------------------

def test_vcs_installed_is_devel(tmp_path):
    """Installed VCS package → DEVEL (rebuildable with --devel)."""
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")

    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "aur"}})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert any(r.action == "DEVEL" for r in results)


def test_dry_run_no_build(tmp_path):
    pkgbase = "htop"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    state_data = {
        pkgbase: {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "aur"}})

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "3.3.0-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "3.3.0-1"}),
        patch("sysforge.update.vercmp", return_value=1),
        patch("sysforge.primitives.makepkg_wrapper.run") as mock_build,
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(_make_args(dry_run=True))

    mock_build.assert_not_called()


def test_devel_flag_triggers_vcs_rebuild(tmp_path):
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "aur"}})

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
        patch("sysforge.primitives.cache_probe.reset_session"),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        import sysforge.primitives.makepkg_wrapper as mw
        mw_run_orig = mw.run
        call_count = []

        def fake_run(*a, **kw):
            call_count.append(1)

        mw.run = fake_run
        try:
            cmd_update(_make_args(devel=True))
        finally:
            mw.run = mw_run_orig

    assert len(call_count) == 1


def test_no_devel_skips_vcs_build(tmp_path):
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {pkgbase: {"name": pkgbase, "source": "aur"}})

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={pkgbase: "r1234.gabcdef-1"}),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        import sysforge.primitives.makepkg_wrapper as mw
        call_count = []
        mw_run_orig = mw.run

        def fake_run(*a, **kw):
            call_count.append(1)

        mw.run = fake_run
        try:
            cmd_update(_make_args(devel=False))
        finally:
            mw.run = mw_run_orig

    assert len(call_count) == 0


def test_pull_failure_continues_to_next_package(tmp_path):
    pkg1_dir = tmp_path / "htop"
    pkg1_dir.mkdir()
    (pkg1_dir / "PKGBUILD").write_text("pkgname=htop\n")

    pkg2_dir = tmp_path / "neovim"
    pkg2_dir.mkdir()
    (pkg2_dir / "PKGBUILD").write_text("pkgname=neovim\n")

    state_data = {
        "htop": {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg1_dir),
            "built_at": "2026-03-17T10:00:00Z",
        },
        "neovim": {
            "pkgver": "0.9.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim", "pkgbuild_dir": str(pkg2_dir),
            "built_at": "2026-03-17T10:00:00Z",
        },
    }

    parsed_neovim = {"globals": {"pkgname": "neovim", "pkgver": "0.9.0", "pkgrel": "1", "epoch": "0"}}

    args = _make_args(offline=False)
    results = []

    overrides = ({}, {
        "htop": {"name": "htop", "source": "aur"},
        "neovim": {"name": "neovim", "source": "aur"},
    })

    with (
        patch("sysforge.update.BuildState") as MockBS,
        # Simulate the scheduler reporting htop failed and neovim up-to-date.
        patch("sysforge.update._sync_sources",
              return_value={"htop": "git fetch failed"}),
        patch("sysforge.update.parse_pkgbuild", return_value=parsed_neovim),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={"htop": "0.9.0-1", "neovim": "0.9.0-1"}),
        patch("sysforge.update.get_foreign_packages",
              return_value={"htop": "0.9.0-1", "neovim": "0.9.0-1"}),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    actions = {r.pkgbase: r.action for r in results}
    assert actions.get("htop") == "PULL_FAILED"
    assert actions.get("neovim") == "UP_TO_DATE"


# ---------------------------------------------------------------------------
# get_foreign_packages
# ---------------------------------------------------------------------------

def _mock_pacman_qm(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_foreign_packages_returns_dict():
    output = "yay 12.3.3-1\nneovim-git r1234.gabcdef-1\n"
    with patch("subprocess.run", return_value=_mock_pacman_qm(output)):
        result = get_foreign_packages()
    assert result == {"yay": "12.3.3-1", "neovim-git": "r1234.gabcdef-1"}


def test_get_foreign_packages_empty_on_failure():
    with patch("subprocess.run", return_value=_mock_pacman_qm("", returncode=1)):
        result = get_foreign_packages()
    assert result == {}


def test_get_foreign_packages_empty_output():
    with patch("subprocess.run", return_value=_mock_pacman_qm("")):
        result = get_foreign_packages()
    assert result == {}


# ---------------------------------------------------------------------------
# Split-pkgbase install filter: only install pkgnames already on the system.
# Regression — pipewire-full-git (16 split pkgnames) was rebuilding all
# sub-packages when only 2 were installed, silently adding 14 new packages.
# ---------------------------------------------------------------------------

def test_already_built_installs_existing_artifact(tmp_path):
    """makepkg AlreadyBuilt → locate existing .pkg.tar and install, not fail.

    Regression: makepkg's "A package has already been built" was treated as a
    build failure even though PKGDEST already held the right artifact.
    """
    from sysforge.primitives.makepkg_wrapper import AlreadyBuilt

    pkg_dir = tmp_path / "htop"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=htop\npkgver=3.4.1\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    existing_pkg = pkgdest / "htop-3.4.1-1-x86_64.pkg.tar.zst"
    existing_pkg.touch()

    state_data = {
        "htop": {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args()
    parsed = {"globals": {"pkgname": "htop", "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"htop": {"name": "htop", "source": "aur"}})
    installed = {"htop": "3.3.0-1"}

    def fake_build_run(*a, **kw):
        raise AlreadyBuilt(pkgbuild)

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.collect_makedeps", return_value=[]),
        patch("sysforge.update.filter_missing_deps", return_value=[]),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.update.snapshot_pkg_dir", return_value=frozenset()),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="htop"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch", return_value=[]),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build_run),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert install_calls == [["htop-3.4.1-1-x86_64.pkg.tar.zst"]]


def test_split_pkgbase_only_installs_installed_subpkgnames(tmp_path):
    pkg_dir = tmp_path / "pipewire-full-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=pipewire-full-git\n")

    # Build emits 4 pkg files for 4 split pkgnames; only 2 are installed.
    built_files = [
        pkg_dir / "pipewire-full-git-1.0-1-x86_64.pkg.tar.zst",
        pkg_dir / "pipewire-full-ffmpeg-git-1.0-1-x86_64.pkg.tar.zst",
        pkg_dir / "pipewire-full-vulkan-git-1.0-1-x86_64.pkg.tar.zst",
        pkg_dir / "libpipewire-full-git-1.0-1-x86_64.pkg.tar.zst",
    ]

    def fake_build_run(*a, **kw):
        # Stamp mtime after build_start so snapshot_pkg_dir's mtime filter keeps them.
        import time
        time.sleep(0.01)
        for f in built_files:
            f.touch()

    state_data = {
        "pipewire-full-ffmpeg-git": {
            "pkgver": "1.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "pipewire-full-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        },
        "pipewire-full-vulkan-git": {
            "pkgver": "1.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "pipewire-full-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        },
    }
    args = _make_args(devel=True)
    parsed = {"globals": {"pkgname": "pipewire-full-git", "pkgver": "1.0", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {
        "pipewire-full-ffmpeg-git": {"name": "pipewire-full-ffmpeg-git", "source": "aur"},
        "pipewire-full-vulkan-git": {"name": "pipewire-full-vulkan-git", "source": "aur"},
    })
    installed = {
        "pipewire-full-ffmpeg-git": "1.0-1",
        "pipewire-full-vulkan-git": "1.0-1",
    }

    def fake_read_pkgname(path):
        stem = Path(path).name
        for pn in ["pipewire-full-ffmpeg-git", "pipewire-full-vulkan-git",
                   "libpipewire-full-git", "pipewire-full-git"]:
            if stem.startswith(pn + "-"):
                return pn
        return None

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.collect_makedeps", return_value=[]),
        patch("sysforge.update.filter_missing_deps", return_value=[]),
        patch("sysforge.update.get_pkgdest", return_value=None),
        patch("sysforge.update.snapshot_pkg_dir", return_value=frozenset(built_files)),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", side_effect=fake_read_pkgname),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch", return_value=[]),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build_run),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert len(install_calls) == 1
    installed_names = install_calls[0]
    assert set(installed_names) == {
        "pipewire-full-ffmpeg-git-1.0-1-x86_64.pkg.tar.zst",
        "pipewire-full-vulkan-git-1.0-1-x86_64.pkg.tar.zst",
    }
    # Crucially, the un-installed split sub-packages must NOT be in the install set.
    assert "libpipewire-full-git-1.0-1-x86_64.pkg.tar.zst" not in installed_names
    assert "pipewire-full-git-1.0-1-x86_64.pkg.tar.zst" not in installed_names


# ---------------------------------------------------------------------------
# --install-only: install pre-built artifacts without re-running makepkg.
# ---------------------------------------------------------------------------

def test_install_only_installs_existing_artifact_without_building(tmp_path):
    """--install-only: locate the artifact in PKGDEST and install it; never call build_run."""
    pkg_dir = tmp_path / "htop"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=htop\npkgver=3.4.1\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    existing_pkg = pkgdest / "htop-3.4.1-1-x86_64.pkg.tar.zst"
    existing_pkg.touch()

    state_data = {
        "htop": {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(install_only=True)
    parsed = {"globals": {"pkgname": "htop", "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"htop": {"name": "htop", "source": "aur"}})
    installed = {"htop": "3.3.0-1"}

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    build_calls = []

    def fake_build_run(*a, **kw):
        build_calls.append(a)
        raise AssertionError("build_run must not be called with --install-only")

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="htop"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build_run),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert build_calls == []
    assert install_calls == [["htop-3.4.1-1-x86_64.pkg.tar.zst"]]


def test_install_only_skips_when_artifact_missing(tmp_path):
    """--install-only: PKGBUILD newer than installed but no artifact in PKGDEST → skip, no install."""
    pkg_dir = tmp_path / "htop"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=htop\npkgver=3.4.1\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    # Note: NO matching artifact at 3.4.1; only an older one.
    (pkgdest / "htop-3.3.0-1-x86_64.pkg.tar.zst").touch()

    state_data = {
        "htop": {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(install_only=True)
    parsed = {"globals": {"pkgname": "htop", "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"htop": {"name": "htop", "source": "aur"}})
    installed = {"htop": "3.3.0-1"}

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="htop"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=AssertionError("no build")),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    # Nothing eligible → no install call at all.
    assert install_calls == []


def test_install_only_rejects_incompatible_flags():
    """--install-only with build-tuning flags must abort via fatal()."""
    import pytest
    from sysforge.cli import _cmd_update

    args = _make_args(install_only=True, no_cleanbuild=True)
    with pytest.raises(SystemExit):
        _cmd_update(args)


# ---------------------------------------------------------------------------
# VCS fallback: pkgver() bumps the version dynamically, so the static
# pkgbuild_ver never matches the actual artifact filename. Both the
# AlreadyBuilt path and --install-only must fall back to a pkgname-only
# lookup and pick the newest by vercmp.
# ---------------------------------------------------------------------------

def test_already_built_vcs_falls_back_to_newest_pkgname_match(tmp_path):
    """VCS package: AlreadyBuilt → static pkgbuild_ver doesn't match the
    bumped filename (0.1.0-1 vs 0.1.0.r45.g1234567-1); helper must fall
    back to a pkgname-only glob and queue the newest artifact for install.
    """
    from sysforge.primitives.makepkg_wrapper import AlreadyBuilt

    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    bumped_pkg = pkgdest / "neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"
    bumped_pkg.touch()

    state_data = {
        "neovim-git": {
            "pkgver": "0.1.0.r10.gaaaaaaa", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(devel=True)
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "0.1.0", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"neovim-git": {"name": "neovim-git", "source": "aur"}})
    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}

    def fake_build_run(*a, **kw):
        raise AlreadyBuilt(pkgbuild)

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.collect_makedeps", return_value=[]),
        patch("sysforge.update.filter_missing_deps", return_value=[]),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.update.snapshot_pkg_dir", return_value=frozenset()),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="neovim-git"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.aur_resolve.resolve_aur_deps_batch", return_value=[]),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build_run),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert install_calls == [["neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"]]


def test_install_only_vcs_picks_newest_artifact_in_pkgdest(tmp_path):
    """--install-only on a VCS package: static pkgbuild_ver mismatches the
    artifact filename; the helper must fall back to a pkgname-only glob
    and select the newest by vercmp, while excluding artifacts not strictly
    newer than installed.
    """
    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    # Two artifacts in PKGDEST: an older one (== installed, skip) and a
    # newer one (the intended target).
    (pkgdest / "neovim-git-0.1.0.r10.gaaaaaaa-1-x86_64.pkg.tar.zst").touch()
    newest = pkgdest / "neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"
    newest.touch()

    state_data = {
        "neovim-git": {
            "pkgver": "0.1.0.r10.gaaaaaaa", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(install_only=True, devel=True)
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "0.1.0", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"neovim-git": {"name": "neovim-git", "source": "aur"}})
    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="neovim-git"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=AssertionError("no build")),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert install_calls == [["neovim-git-0.1.0.r45.g1234567-1-x86_64.pkg.tar.zst"]]


def test_install_only_vcs_skips_when_only_older_artifacts_present(tmp_path):
    """--install-only on a VCS package: only an artifact == installed exists,
    so the helper's installed_ver guard rejects it and nothing installs."""
    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=neovim-git\npkgver=0.1.0\npkgrel=1\n")

    pkgdest = tmp_path / "pkgdest"
    pkgdest.mkdir()
    # Only an artifact at the same version as installed → not strictly newer.
    (pkgdest / "neovim-git-0.1.0.r10.gaaaaaaa-1-x86_64.pkg.tar.zst").touch()

    state_data = {
        "neovim-git": {
            "pkgver": "0.1.0.r10.gaaaaaaa", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(install_only=True, devel=True)
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "0.1.0", "pkgrel": "1", "epoch": "0"}}
    overrides = ({}, {"neovim-git": {"name": "neovim-git", "source": "aur"}})
    installed = {"neovim-git": "0.1.0.r10.gaaaaaaa-1"}

    install_calls = []

    def fake_install(pkg_paths):
        install_calls.append([Path(p).name for p in pkg_paths])
        return True

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages", return_value=installed),
        patch("sysforge.update.get_foreign_packages", return_value=installed),
        patch("sysforge.update.get_pkgdest", return_value=pkgdest),
        patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="neovim-git"),
        patch("sysforge.update.batch_install_pkgs", side_effect=fake_install),
        patch("sysforge.primitives.makepkg_wrapper.run", side_effect=AssertionError("no build")),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert install_calls == []
