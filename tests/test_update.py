"""
test_update.py — unit tests for sysforge.update

All subprocess calls (pacman -Q, git pull, makepkg) and filesystem access to
the state dir are mocked so no real system state is required.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge.update import (
    _get_installed_version, _is_vcs, cmd_update,
    _get_foreign_packages, _load_packages_toml_names,
    _append_to_packages_toml, _discover_and_add, _DiscoveredResult,
)


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
# _get_installed_version
# ---------------------------------------------------------------------------

def _mock_pacman(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_installed_version_found():
    with patch("subprocess.run", return_value=_mock_pacman("htop 3.3.0-1\n")):
        assert _get_installed_version("htop") == "3.3.0-1"


def test_get_installed_version_not_installed():
    with patch("subprocess.run", return_value=_mock_pacman("", returncode=1)):
        assert _get_installed_version("htop") is None


# ---------------------------------------------------------------------------
# cmd_update — helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        state_dir=None,
        dry_run=False,
        devel=False,
        no_update=True,  # skip git pull in most tests
        no_pkg_log=True,
        persist_log=False,
        log_dir=None,
        profile_conf=None,
        cache_report=False,
        all=False,
        packages=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_build_state(packages):
    """Return a mock BuildState.all_packages() dict."""
    bs = MagicMock()
    bs.all_packages.return_value = packages
    return bs


# ---------------------------------------------------------------------------
# cmd_update — empty state
# ---------------------------------------------------------------------------

def test_empty_build_state_exits_cleanly(capsys):
    with patch("sysforge.update.BuildState") as MockBS:
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(_make_args())
    captured = capsys.readouterr()
    assert "No packages recorded" in captured.err


# ---------------------------------------------------------------------------
# cmd_update — version checks
# ---------------------------------------------------------------------------

def _run_update_with_package(tmp_path, pkgbase, pkgver_installed, pkgver_pkgbuild,
                              args_extra=None):
    """
    Helper: set up a fake PKGBUILD in tmp_path, a build state entry, and run cmd_update.
    Returns (results_actions_list, capsys) but we capture manually.
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

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value={"globals": parsed_globals}),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update._get_installed_version", return_value=pkgver_installed),
        patch("sysforge.update.vercmp") as mock_vercmp,
    ):
        MockBS.return_value.all_packages.return_value = state_data
        # vercmp: return 1 if pkgbuild > installed, 0 if equal, -1 if pkgbuild < installed
        from sysforge.primitives.version import format_version
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


def test_check_not_installed(tmp_path):
    pkg_dir = tmp_path / "htop"
    pkg_dir.mkdir()
    state_data = {
        "htop": {
            "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args()
    parsed = {"globals": {"pkgname": "htop", "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update._get_installed_version", return_value=None),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        (tmp_path / "htop" / "PKGBUILD").write_text("pkgname=htop\n")

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert any(r.action == "NOT_INSTALLED" for r in results)


def test_vcs_always_flagged(tmp_path):
    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    state_data = {
        "neovim-git": {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args()
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
    ):
        MockBS.return_value.all_packages.return_value = state_data
        (pkg_dir / "PKGBUILD").write_text("pkgname=neovim-git\n")

        def capture(res_list, a):
            results.extend(res_list)
        with patch("sysforge.update._print_summary", side_effect=capture):
            cmd_update(args)

    assert any(r.action == "DEVEL" for r in results)


def test_dry_run_no_build(tmp_path):
    pkg_dir = tmp_path / "htop"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=htop\n")
    state_data = {
        "htop": {
            "pkgver": "3.3.0", "pkgrel": "1", "epoch": "0",
            "pkgbase": "htop", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(dry_run=True)
    parsed = {"globals": {"pkgname": "htop", "pkgver": "3.4.1", "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update._get_installed_version", return_value="3.3.0-1"),
        patch("sysforge.update.vercmp", return_value=1),
        patch("sysforge.primitives.makepkg_wrapper.run") as mock_build,
    ):
        MockBS.return_value.all_packages.return_value = state_data
        cmd_update(args)

    mock_build.assert_not_called()


def test_devel_flag_triggers_vcs_rebuild(tmp_path):
    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=neovim-git\n")
    state_data = {
        "neovim-git": {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(devel=True)
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.primitives.makepkg_wrapper.run") as mock_build,
        patch("sysforge.primitives.cache_probe.reset_session"),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        with patch("sysforge.update.build_run" if hasattr(__import__("sysforge.update", fromlist=["build_run"]), "build_run") else "sysforge.primitives.makepkg_wrapper.run"):
            # Import lazily inside cmd_update; patch at the source
            pass

        # Patch the lazy import inside cmd_update
        import sysforge.primitives.makepkg_wrapper as mw
        mw_run_orig = mw.run
        call_count = []

        def fake_run(*a, **kw):
            call_count.append(1)

        mw.run = fake_run
        try:
            cmd_update(args)
        finally:
            mw.run = mw_run_orig

    assert len(call_count) == 1


def test_no_devel_skips_vcs_build(tmp_path):
    pkg_dir = tmp_path / "neovim-git"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=neovim-git\n")
    state_data = {
        "neovim-git": {
            "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0",
            "pkgbase": "neovim-git", "pkgbuild_dir": str(pkg_dir),
            "built_at": "2026-03-17T10:00:00Z",
        }
    }
    args = _make_args(devel=False)
    parsed = {"globals": {"pkgname": "neovim-git", "pkgver": "r1234.gabcdef", "pkgrel": "1", "epoch": "0"}}

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
    ):
        MockBS.return_value.all_packages.return_value = state_data

        import sysforge.primitives.makepkg_wrapper as mw
        call_count = []
        mw_run_orig = mw.run

        def fake_run(*a, **kw):
            call_count.append(1)

        mw.run = fake_run
        try:
            cmd_update(args)
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

    pull_call_count = []

    def fake_pull(d):
        pull_call_count.append(d.name)
        if d.name == "htop":
            raise RuntimeError("git pull failed")

    parsed_neovim = {"globals": {"pkgname": "neovim", "pkgver": "0.9.0", "pkgrel": "1", "epoch": "0"}}

    args = _make_args(no_update=False)
    results = []

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.git_pull_rebase", side_effect=fake_pull),
        patch("sysforge.update.parse_pkgbuild", return_value=parsed_neovim),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update._get_installed_version", return_value="0.9.0-1"),
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
# _get_foreign_packages
# ---------------------------------------------------------------------------

def _mock_pacman_qm(stdout, returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_get_foreign_packages_returns_dict():
    output = "yay 12.3.3-1\nneovim-git r1234.gabcdef-1\n"
    with patch("subprocess.run", return_value=_mock_pacman_qm(output)):
        result = _get_foreign_packages()
    assert result == {"yay": "12.3.3-1", "neovim-git": "r1234.gabcdef-1"}


def test_get_foreign_packages_empty_on_failure():
    with patch("subprocess.run", return_value=_mock_pacman_qm("", returncode=1)):
        result = _get_foreign_packages()
    assert result == {}


def test_get_foreign_packages_empty_output():
    with patch("subprocess.run", return_value=_mock_pacman_qm("")):
        result = _get_foreign_packages()
    assert result == {}


# ---------------------------------------------------------------------------
# _load_packages_toml_names
# ---------------------------------------------------------------------------

def test_load_packages_toml_names_reads_names(tmp_path):
    toml = (tmp_path / "packages.toml")
    toml.write_text(
        "[build]\npkgbuild_dir = \"~/builds\"\n\n"
        "[[package]]\nname = \"htop\"\nsource = \"repo\"\n\n"
        "[[package]]\nname = \"yay\"\nsource = \"aur\"\n"
    )
    result = _load_packages_toml_names(toml)
    assert result == {"htop", "yay"}


def test_load_packages_toml_names_missing_file(tmp_path):
    result = _load_packages_toml_names(tmp_path / "nonexistent.toml")
    assert result == set()


def test_load_packages_toml_names_empty_file(tmp_path):
    toml = tmp_path / "packages.toml"
    toml.write_text("[build]\npkgbuild_dir = \"~/builds\"\n")
    result = _load_packages_toml_names(toml)
    assert result == set()


# ---------------------------------------------------------------------------
# _append_to_packages_toml
# ---------------------------------------------------------------------------

def test_append_to_packages_toml_creates_file(tmp_path):
    path = tmp_path / "packages.toml"
    entries = [{"name": "yay", "source": "aur"}]
    with patch("sysforge.packages_cmd._entry_toml_block",
               side_effect=lambda e: f'[[package]]\nname = "{e["name"]}"\n'):
        _append_to_packages_toml(path, entries)
    assert path.exists()
    content = path.read_text()
    assert "yay" in content


def test_append_to_packages_toml_appends_to_existing(tmp_path):
    path = tmp_path / "packages.toml"
    path.write_text("[build]\npkgbuild_dir = \"~/builds\"\n\n[[package]]\nname = \"htop\"\n")
    entries = [{"name": "yay", "source": "aur"}]
    with patch("sysforge.packages_cmd._entry_toml_block",
               side_effect=lambda e: f'[[package]]\nname = "{e["name"]}"\n'):
        _append_to_packages_toml(path, entries)
    content = path.read_text()
    assert "htop" in content
    assert "yay" in content


# ---------------------------------------------------------------------------
# _discover_and_add
# ---------------------------------------------------------------------------

def _make_discover_args(**kwargs):
    defaults = dict(dry_run=False, devel=False, packages=None, profile_conf=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_discover_no_foreign_packages():
    bs = MagicMock()
    bs.all_packages.return_value = {}
    with patch("sysforge.update._get_foreign_packages", return_value={}):
        results = _discover_and_add(_make_discover_args(), bs, {})
    assert results == []


def test_discover_all_already_tracked():
    bs = MagicMock()
    bs.all_packages.return_value = {"yay": {"pkgbase": "yay", "pkgver": "12.0"}}
    with patch("sysforge.update._get_foreign_packages", return_value={"yay": "12.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()):
        results = _discover_and_add(_make_discover_args(), bs, {})
    assert results == []


def test_discover_not_in_aur(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    with patch("sysforge.update._get_foreign_packages", return_value={"localonly": "1.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={}), \
         patch("sysforge.update._append_to_packages_toml"):
        results = _discover_and_add(_make_discover_args(), bs, {})
    assert len(results) == 1
    assert results[0].action == "NOT_FOUND"
    assert results[0].pkgname == "localonly"


def test_discover_adds_aur_package(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\npkgver=12.3.3\npkgrel=1\n")

    with patch("sysforge.update._get_foreign_packages", return_value={"yay": "12.0.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"yay": {"Name": "yay"}}), \
         patch("sysforge.primitives.config.find_pkgbuild", return_value=pkgbuild), \
         patch("sysforge.update.parse_pkgbuild",
               return_value={"globals": {"pkgname": "yay", "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}), \
         patch("sysforge.update.vercmp", return_value=1), \
         patch("sysforge.update._append_to_packages_toml") as mock_append:
        results = _discover_and_add(_make_discover_args(), bs, {})

    assert len(results) == 1
    assert results[0].action == "OUTDATED"
    assert results[0].pkgname == "yay"
    mock_append.assert_called_once()


def test_discover_dry_run_no_write(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}

    with patch("sysforge.update._get_foreign_packages", return_value={"yay": "12.0.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"yay": {"Name": "yay"}}), \
         patch("sysforge.update._append_to_packages_toml") as mock_append:
        results = _discover_and_add(_make_discover_args(dry_run=True), bs, {})

    # In dry_run, no PKGBUILD is fetched, entry still added to results but not written
    mock_append.assert_not_called()
    assert len(results) == 1


def test_discover_vcs_package_flagged_as_devel():
    bs = MagicMock()
    bs.all_packages.return_value = {}

    with patch("sysforge.update._get_foreign_packages", return_value={"neovim-git": "r1234.gabcdef-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"neovim-git": {"Name": "neovim-git"}}), \
         patch("sysforge.primitives.config.find_pkgbuild", return_value=None), \
         patch("sysforge.update._append_to_packages_toml"):
        results = _discover_and_add(_make_discover_args(), bs, {})

    assert any(r.pkgname == "neovim-git" and r.action == "DEVEL" for r in results)


# ---------------------------------------------------------------------------
# cmd_update --all integration
# ---------------------------------------------------------------------------

def test_cmd_update_all_with_empty_build_state(tmp_path, capsys):
    """--all with empty build_state should not exit early."""
    args = _make_args(all=True)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_and_add", return_value=[]) as mock_discover, \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    mock_discover.assert_called_once()
    captured = capsys.readouterr()
    assert "No packages recorded" in captured.err


def test_cmd_update_all_discovered_printed(tmp_path, capsys):
    """Discovery results are printed when --all is set."""
    discovered = [
        _DiscoveredResult(pkgname="yay", action="ADDED", installed_ver="12.0-1"),
    ]
    args = _make_args(all=True)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_and_add", return_value=discovered), \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    captured = capsys.readouterr()
    assert "yay" in captured.out
    assert "ADDED" in captured.out


def test_cmd_update_all_outdated_is_built(tmp_path):
    """Discovered OUTDATED packages are built when not dry_run."""
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\n")

    discovered = [
        _DiscoveredResult(
            pkgname="yay", action="OUTDATED",
            installed_ver="12.0-1", pkgbuild_ver="12.3.3-1",
            pkgbuild_path=pkgbuild,
        )
    ]
    args = _make_args(all=True)

    build_calls = []
    def fake_build(path, **kwargs):
        build_calls.append(path)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_and_add", return_value=discovered), \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")), \
         patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build), \
         patch("sysforge.primitives.cache_probe.reset_session"):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    assert len(build_calls) == 1
    assert build_calls[0] == pkgbuild


def test_cmd_update_all_dry_run_no_build(tmp_path):
    """--all --dry-run prints discovery but does not build."""
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\n")

    discovered = [
        _DiscoveredResult(
            pkgname="yay", action="OUTDATED",
            installed_ver="12.0-1", pkgbuild_ver="12.3.3-1",
            pkgbuild_path=pkgbuild,
        )
    ]
    args = _make_args(all=True, dry_run=True)

    import sysforge.primitives.makepkg_wrapper as mw
    build_calls = []
    mw_run_orig = mw.run

    def fake_build(*a, **kw):
        build_calls.append(1)

    mw.run = fake_build
    try:
        with patch("sysforge.update.BuildState") as MockBS, \
             patch("sysforge.update._discover_and_add", return_value=discovered), \
             patch("sysforge.update.load_config", return_value={}), \
             patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")):
            MockBS.return_value.all_packages.return_value = {}
            cmd_update(args)
    finally:
        mw.run = mw_run_orig

    assert build_calls == []
