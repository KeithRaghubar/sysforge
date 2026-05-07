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
    _check_one_pkgbase,
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


def test_foreign_split_package_resolves_pkgbase_from_local_db(tmp_path):
    """Foreign split-package subnames (e.g. linux-custom-headers) collapse to
    their parent pkgbase via pacman's local DB %BASE%, even when not in AUR.
    AUR RPC must NOT be called when the local DB already resolves the base."""
    pkgbase = "linux-custom"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgbase={pkgbase}\n"
        f"pkgname=({pkgbase} {pkgbase}-headers)\n"
        f"pkgver=6.19.12.arch1\npkgrel=1\n"
    )

    parsed = {"globals": {
        "pkgbase": pkgbase,
        "pkgname": [pkgbase, f"{pkgbase}-headers"],
        "pkgver": "6.19.12.arch1", "pkgrel": "1", "epoch": "0",
    }}

    foreign = {pkgbase: "6.19.12.arch1-1", f"{pkgbase}-headers": "6.19.12.arch1-1"}

    def fake_get_pkgbase(name, root=None):
        if name in foreign:
            return pkgbase
        return None

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=({}, {})),
        patch("sysforge.update.get_all_installed_packages", return_value=foreign),
        patch("sysforge.update.get_foreign_packages", return_value=foreign),
        patch("sysforge.update.get_pkgbase", side_effect=fake_get_pkgbase),
        patch("sysforge.update.aur_info") as mock_aur_info,
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    # Both subpackages collapse into the single linux-custom pkgbase group.
    assert {r.pkgbase for r in results} == {pkgbase}
    # AUR RPC must not be called — local DB already supplied %BASE%.
    mock_aur_info.assert_not_called()


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


def test_update_repo_profiled_walks_installed_repo_packages(tmp_path):
    """
    A2: with `[build] update_repo_profiled = true`, every installed repo
    package is iterated alongside foreign packages. No per-package override
    needed; the toml key opts the entire repo surface in.
    """
    pkgbase = "firefox"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(
        f"pkgname={pkgbase}\npkgver=131.0\npkgrel=1\n"
    )

    parsed = {"globals": {"pkgname": pkgbase, "pkgver": "131.0",
                          "pkgrel": "1", "epoch": "0"}}
    # build_cfg sets update_repo_profiled; no per-package overrides.
    overrides = ({"update_repo_profiled": True}, {})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config",
              return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: "131.0-1"}),
        patch("sysforge.update.get_foreign_packages", return_value={}),
        patch("sysforge.update.vercmp", return_value=0),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert {r.pkgbase for r in results} == {pkgbase}


def test_update_repo_profiled_off_skips_repo_packages(tmp_path):
    """
    Default (update_repo_profiled unset / false): a repo package without
    an override stays out of scope. Confirms the gate is load-bearing.
    """
    overrides = ({"update_repo_profiled": False}, {})

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_overrides", return_value=overrides),
        patch("sysforge.update.get_all_installed_packages",
              return_value={"firefox": "131.0-1"}),
        patch("sysforge.update.get_foreign_packages", return_value={}),
    ):
        MockBS.return_value.all_packages.return_value = {}

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(_make_args())

    assert results == []


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
    """--devel + resolved pkgver newer than installed → build runs once."""
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
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="r5678.g9999999-1"),
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


def test_devel_skips_uptodate_vcs(tmp_path):
    """--devel + resolved pkgver equal to installed → build does NOT run."""
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
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="r1234.gabcdef-1"),
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

    assert len(call_count) == 0


def test_devel_short_circuits_when_upstream_unmoved(tmp_path):
    """--devel + cached SHA matches ls-remote → evaluate_vcs_pkgver skipped."""
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    cached_sha = "f00dbabe" + "0" * 32  # 40 hex chars
    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
            "built_upstream_commit": cached_sha,
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
        patch("sysforge.update.peek_upstream_commit", return_value=cached_sha) as peek,
        patch("sysforge.update.evaluate_vcs_pkgver") as evaluate,
        patch("sysforge.primitives.cache_probe.reset_session"),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        MockBS.return_value.get.side_effect = lambda name: state_data.get(name)

        import sysforge.primitives.makepkg_wrapper as mw
        mw_run_orig = mw.run
        call_count = []
        mw.run = lambda *a, **kw: call_count.append(1)
        try:
            cmd_update(_make_args(devel=True))
        finally:
            mw.run = mw_run_orig

    assert peek.called, "peek_upstream_commit must run when cache is populated"
    assert not evaluate.called, "evaluate_vcs_pkgver must be skipped on cache hit"
    assert len(call_count) == 0, "no build should run for an UP_TO_DATE pkgbase"


def test_devel_full_resolve_on_lsremote_miss(tmp_path):
    """--devel + cached SHA differs from ls-remote → falls through to evaluate."""
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    cached_sha = "a" * 40
    new_sha = "b" * 40
    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
            "built_upstream_commit": cached_sha,
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
        patch("sysforge.update.peek_upstream_commit", return_value=new_sha),
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="r5678.gfedcba0-1") as evaluate,
        patch("sysforge.primitives.cache_probe.reset_session"),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        MockBS.return_value.get.side_effect = lambda name: state_data.get(name)

        import sysforge.primitives.makepkg_wrapper as mw
        mw_run_orig = mw.run
        call_count = []
        mw.run = lambda *a, **kw: call_count.append(1)
        try:
            cmd_update(_make_args(devel=True))
        finally:
            mw.run = mw_run_orig

    assert evaluate.called, "evaluate_vcs_pkgver must run on cache miss"
    assert len(call_count) == 1, "newer resolved pkgver should trigger one build"


def test_devel_full_resolve_when_no_cached_commit(tmp_path):
    """--devel + bs entry lacks built_upstream_commit → no peek, evaluate runs."""
    pkgbase = "neovim-git"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text(f"pkgname={pkgbase}\n")
    state_data = {
        pkgbase: {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": pkgbase, "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
            # no built_upstream_commit
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
        patch("sysforge.update.peek_upstream_commit") as peek,
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="r1234.gabcdef-1") as evaluate,
        patch("sysforge.primitives.cache_probe.reset_session"),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        MockBS.return_value.get.side_effect = lambda name: state_data.get(name)

        import sysforge.primitives.makepkg_wrapper as mw
        mw_run_orig = mw.run
        call_count = []
        mw.run = lambda *a, **kw: call_count.append(1)
        try:
            cmd_update(_make_args(devel=True))
        finally:
            mw.run = mw_run_orig

    assert not peek.called, "peek_upstream_commit must be skipped without cached SHA"
    assert evaluate.called, "evaluate_vcs_pkgver must run when cache is empty"
    assert len(call_count) == 0, "resolved == installed should not rebuild"


def test_devel_skips_when_pkgver_eval_fails(tmp_path, capsys):
    """--devel + pkgver() resolution returns None → skip with WARN, no build."""
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
        patch("sysforge.update.evaluate_vcs_pkgver", return_value=None),
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

    assert len(call_count) == 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "DEVEL_EVAL_FAILED" in combined or "pkgver() evaluation failed" in combined


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
              return_value={"htop": ("failed", "git fetch failed")}),
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
        patch("sysforge.update.evaluate_vcs_pkgver", return_value="1.0.r1.gffffff-1"),
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
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="0.1.0.r45.g1234567-1"),
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
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="0.1.0.r45.g1234567-1"),
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
        patch("sysforge.update.evaluate_vcs_pkgver",
              return_value="0.1.0.r45.g1234567-1"),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    assert install_calls == []


# ---------------------------------------------------------------------------
# Sync-status → action dispatch (verbose skip messaging)
# ---------------------------------------------------------------------------

def _check_pkgbase_with_sync_status(tmp_path, status):
    """Run _check_one_pkgbase with a sync_failures entry carrying `status`."""
    pkgbase = "htop"
    pkg_dir = tmp_path / pkgbase
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=htop\npkgver=1\npkgrel=1\n")

    return _check_one_pkgbase(
        pkgbase=pkgbase,
        pkgnames=[pkgbase],
        entry={"pkgbuild_dir": str(pkg_dir)},
        sync_failures={pkgbase: (status, "synthetic error")},
        all_installed={pkgbase: "1-1"},
        unrecorded_names=set(),
        skip_sync_check=False,
        rpc_version_by_base={},
    )


def test_sync_status_failed_maps_to_pull_failed(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "failed")
    assert result is not None
    assert result.action == "PULL_FAILED"


def test_sync_status_rate_limited_maps_to_rate_limited(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "rate_limited")
    assert result is not None
    assert result.action == "RATE_LIMITED"


def test_sync_status_purge_refused_maps_to_purge_refused(tmp_path):
    result = _check_pkgbase_with_sync_status(tmp_path, "purge_refused")
    assert result is not None
    assert result.action == "PURGE_REFUSED"


def test_sync_status_unknown_falls_back_to_pull_failed(tmp_path):
    """Defensive: an unmapped sync status still produces a recognizable action."""
    result = _check_pkgbase_with_sync_status(tmp_path, "some_future_status")
    assert result is not None
    assert result.action == "PULL_FAILED"


# ---------------------------------------------------------------------------
# _print_summary verbose-vs-default behavior
# ---------------------------------------------------------------------------

def _make_summary_results():
    from sysforge.update import _UpdateResult
    from pathlib import Path
    return [
        _UpdateResult("htop", ["htop"], "NEEDS_REBUILD", "3.3.0-1", "3.4.1-1",
                      Path("/tmp/htop/PKGBUILD")),
        _UpdateResult("neovim", ["neovim"], "UP_TO_DATE", "0.9.5-1", "0.9.5-1",
                      Path("/tmp/neovim/PKGBUILD")),
        _UpdateResult("foo-git", ["foo-git"], "DEVEL", "r10.abc-1", None,
                      Path("/tmp/foo-git/PKGBUILD")),
        _UpdateResult("bar", ["bar"], "RATE_LIMITED", None, None,
                      Path("/tmp/bar/PKGBUILD")),
    ]


def test_print_summary_default_hides_skip_lines(capsys):
    from sysforge.update import _print_summary
    args = SimpleNamespace(verbose=0)
    _print_summary(_make_summary_results(), args)
    captured = capsys.readouterr().out
    # NEEDS_REBUILD always shown (actionable)
    assert "[NEEDS_REBUILD]" in captured
    assert "htop" in captured
    # UP_TO_DATE / DEVEL / RATE_LIMITED hidden at default verbosity
    assert "[UP_TO_DATE]" not in captured
    assert "[DEVEL]" not in captured
    assert "[RATE_LIMITED]" not in captured
    # Header counts still mention every category
    assert "1 up to date" in captured
    assert "1 devel" in captured
    assert "1 rate-limited" in captured
    # Hint to the user about -v
    assert "-v" in captured


def test_print_summary_verbose_shows_all_lines(capsys):
    from sysforge.update import _print_summary
    args = SimpleNamespace(verbose=1)
    _print_summary(_make_summary_results(), args)
    captured = capsys.readouterr().out
    assert "[NEEDS_REBUILD]" in captured
    assert "[UP_TO_DATE]" in captured
    assert "[DEVEL]" in captured
    assert "[RATE_LIMITED]" in captured
    # No -v hint when already verbose
    assert "run with -v" not in captured
