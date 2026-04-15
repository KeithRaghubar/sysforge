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
    _is_vcs, cmd_update,
    _load_packages_toml_names,
    _append_to_packages_toml, _discover_new_packages, _UpdateResult,
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
    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update._load_full_packages_toml", return_value=({}, [])):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(_make_args())
    captured = capsys.readouterr()
    assert "No packages found" in captured.err


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

    fake_manifest = ({}, [{"name": pkgbase, "source": "aur"}])

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value={"globals": parsed_globals}),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages",
              return_value={pkgbase: pkgver_installed}),
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
    fake_manifest = ({}, [{"name": "htop", "source": "aur"}])

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages", return_value={}),
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
    fake_manifest = ({}, [{"name": "neovim-git", "source": "aur"}])

    results = []
    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages", return_value={}),
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

    fake_manifest = ({}, [{"name": "htop", "source": "aur"}])

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages",
              return_value={"htop": "3.3.0-1"}),
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
    fake_manifest = ({}, [{"name": "neovim-git", "source": "aur"}])

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages", return_value={}),
        patch("sysforge.primitives.makepkg_wrapper.run"),
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
    fake_manifest = ({}, [{"name": "neovim-git", "source": "aur"}])

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.parse_pkgbuild", return_value=parsed),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages", return_value={}),
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

    def fake_pull(d, **kwargs):
        pull_call_count.append(d.name)
        if d.name == "htop":
            raise RuntimeError("git pull failed")

    parsed_neovim = {"globals": {"pkgname": "neovim", "pkgver": "0.9.0", "pkgrel": "1", "epoch": "0"}}

    args = _make_args(offline=False)
    results = []

    fake_manifest = ({}, [
        {"name": "htop", "source": "aur"},
        {"name": "neovim", "source": "aur"},
    ])

    with (
        patch("sysforge.update.BuildState") as MockBS,
        patch("sysforge.update.git_pull_rebase", side_effect=fake_pull),
        patch("sysforge.update.purge_src", side_effect=RuntimeError("recovery purge failed")),
        patch("sysforge.update.parse_pkgbuild", return_value=parsed_neovim),
        patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")),
        patch("sysforge.update.load_config", return_value={}),
        patch("sysforge.update._load_full_packages_toml", return_value=fake_manifest),
        patch("sysforge.update.get_all_installed_packages",
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
# _load_packages_toml_names
# ---------------------------------------------------------------------------

def test_load_packages_toml_names_reads_names(tmp_path):
    toml = (tmp_path / "packages.toml")
    toml.write_text(
        "[build]\npkgbuild_src_dir = \"~/src\"\n\n"
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
    toml.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n")
    result = _load_packages_toml_names(toml)
    assert result == set()


# ---------------------------------------------------------------------------
# _append_to_packages_toml
# ---------------------------------------------------------------------------

def test_append_to_packages_toml_creates_file(tmp_path):
    path = tmp_path / "packages.toml"
    entries = [{"name": "yay", "source": "aur"}]
    with patch("sysforge.packages_cmd.entry_toml_block",
               side_effect=lambda e: f'[[package]]\nname = "{e["name"]}"\n'):
        _append_to_packages_toml(path, entries)
    assert path.exists()
    content = path.read_text()
    assert "yay" in content


def test_append_to_packages_toml_appends_to_existing(tmp_path):
    path = tmp_path / "packages.toml"
    path.write_text("[build]\npkgbuild_src_dir = \"~/src\"\n\n[[package]]\nname = \"htop\"\n")
    entries = [{"name": "yay", "source": "aur"}]
    with patch("sysforge.packages_cmd.entry_toml_block",
               side_effect=lambda e: f'[[package]]\nname = "{e["name"]}"\n'):
        _append_to_packages_toml(path, entries)
    content = path.read_text()
    assert "htop" in content
    assert "yay" in content


# ---------------------------------------------------------------------------
# _discover_new_packages
# ---------------------------------------------------------------------------

def _make_discover_args(**kwargs):
    defaults = dict(dry_run=False, devel=False, packages=None, profile_conf=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_discover_no_foreign_packages(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    packages_path = tmp_path / "packages.toml"
    with patch("sysforge.update.get_foreign_packages", return_value={}):
        entries, not_found = _discover_new_packages(_make_discover_args(), bs, packages_path)
    assert entries == []
    assert not_found == []


def test_discover_all_already_tracked(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {"yay": {"pkgbase": "yay", "pkgver": "12.0"}}
    packages_path = tmp_path / "packages.toml"
    with patch("sysforge.update.get_foreign_packages", return_value={"yay": "12.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()):
        entries, not_found = _discover_new_packages(_make_discover_args(), bs, packages_path)
    assert entries == []
    assert not_found == []


def test_discover_not_in_aur(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    packages_path = tmp_path / "packages.toml"
    with patch("sysforge.update.get_foreign_packages", return_value={"localonly": "1.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={}), \
         patch("sysforge.update._append_to_packages_toml"):
        entries, not_found = _discover_new_packages(_make_discover_args(), bs, packages_path)
    assert entries == []
    assert not_found == ["localonly"]


def test_discover_adds_aur_package(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\npkgver=12.3.3\npkgrel=1\n")
    packages_path = tmp_path / "packages.toml"

    with patch("sysforge.update.get_foreign_packages", return_value={"yay": "12.0.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"yay": {"Name": "yay"}}), \
         patch("sysforge.update._append_to_packages_toml") as mock_append:
        entries, not_found = _discover_new_packages(_make_discover_args(), bs, packages_path)

    assert len(entries) == 1
    assert entries[0]["name"] == "yay"
    assert entries[0]["source"] == "aur"
    mock_append.assert_called_once()


def test_discover_dry_run_no_write(tmp_path):
    bs = MagicMock()
    bs.all_packages.return_value = {}
    packages_path = tmp_path / "packages.toml"

    with patch("sysforge.update.get_foreign_packages", return_value={"yay": "12.0.0-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"yay": {"Name": "yay"}}), \
         patch("sysforge.update._append_to_packages_toml") as mock_append:
        entries, not_found = _discover_new_packages(_make_discover_args(dry_run=True), bs, packages_path)

    mock_append.assert_not_called()
    assert len(entries) == 1


def test_discover_vcs_package_is_added(tmp_path):
    """VCS packages are added to packages.toml by discovery (classified as DEVEL in version check)."""
    bs = MagicMock()
    bs.all_packages.return_value = {}
    packages_path = tmp_path / "packages.toml"

    with patch("sysforge.update.get_foreign_packages", return_value={"neovim-git": "r1234.gabcdef-1"}), \
         patch("sysforge.update._load_packages_toml_names", return_value=set()), \
         patch("sysforge.primitives.aur.aur_info", return_value={"neovim-git": {"Name": "neovim-git"}}), \
         patch("sysforge.update._append_to_packages_toml"):
        entries, not_found = _discover_new_packages(_make_discover_args(), bs, packages_path)

    assert any(e["name"] == "neovim-git" for e in entries)


# ---------------------------------------------------------------------------
# cmd_update --all integration
# ---------------------------------------------------------------------------

def test_cmd_update_all_with_empty_discovery(tmp_path, capsys):
    """--all with empty build_state and no discovered packages shows message."""
    args = _make_args(all=True)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_new_packages", return_value=([], [])), \
         patch("sysforge.update.load_config", return_value={}), \
         patch("sysforge.update._load_full_packages_toml", return_value=({}, [])), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    captured = capsys.readouterr()
    assert "No packages found" in captured.err


def test_cmd_update_all_discovered_shows_in_summary(tmp_path, capsys):
    """Discovered packages appear in the version check summary."""
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    (pkg_dir / "PKGBUILD").write_text("pkgname=yay\npkgver=12.3.3\npkgrel=1\n")

    # Mock discovery returning an entry that will be assembled into packages dict
    discovered_entries = [{"name": "yay", "source": "aur"}]
    args = _make_args(all=True)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_new_packages", return_value=(discovered_entries, [])), \
         patch("sysforge.update.load_config", return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}), \
         patch("sysforge.update._load_full_packages_toml", return_value=({}, [])), \
         patch("sysforge.update.get_all_installed_packages", return_value={"yay": "12.0-1"}), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")), \
         patch("sysforge.update.parse_pkgbuild",
               return_value={"globals": {"pkgname": "yay", "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}), \
         patch("sysforge.update.vercmp", return_value=1):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    captured = capsys.readouterr()
    assert "yay" in captured.out
    assert "DISCOVERED" in captured.out or "discovered" in captured.out


def test_cmd_update_all_outdated_is_built(tmp_path):
    """Discovered OUTDATED packages are built when not dry_run."""
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\n")

    discovered_entries = [{"name": "yay", "source": "aur"}]
    args = _make_args(all=True)

    build_calls = []
    def fake_build(path, **kwargs):
        build_calls.append(path)

    with patch("sysforge.update.BuildState") as MockBS, \
         patch("sysforge.update._discover_new_packages", return_value=(discovered_entries, [])), \
         patch("sysforge.update.load_config", return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}), \
         patch("sysforge.update._load_full_packages_toml", return_value=({}, [])), \
         patch("sysforge.update.get_all_installed_packages", return_value={"yay": "12.0-1"}), \
         patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")), \
         patch("sysforge.update.parse_pkgbuild",
               return_value={"globals": {"pkgname": "yay", "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}), \
         patch("sysforge.update.vercmp", return_value=1), \
         patch("sysforge.primitives.makepkg_wrapper.run", side_effect=fake_build), \
         patch("sysforge.primitives.cache_probe.reset_session"):
        MockBS.return_value.all_packages.return_value = {}
        cmd_update(args)

    assert len(build_calls) == 1


def test_cmd_update_all_dry_run_no_build(tmp_path):
    """--all --dry-run prints discovery but does not build."""
    pkg_dir = tmp_path / "yay"
    pkg_dir.mkdir()
    pkgbuild = pkg_dir / "PKGBUILD"
    pkgbuild.write_text("pkgname=yay\n")

    discovered_entries = [{"name": "yay", "source": "aur"}]
    args = _make_args(all=True, dry_run=True)

    import sysforge.primitives.makepkg_wrapper as mw
    build_calls = []
    mw_run_orig = mw.run

    def fake_build(*a, **kw):
        build_calls.append(1)

    mw.run = fake_build
    try:
        with patch("sysforge.update.BuildState") as MockBS, \
             patch("sysforge.update._discover_new_packages", return_value=(discovered_entries, [])), \
             patch("sysforge.update.load_config", return_value={"paths": {"pkgbuild_src_dir": str(tmp_path)}}), \
             patch("sysforge.update._load_full_packages_toml", return_value=({}, [])), \
             patch("sysforge.update.get_all_installed_packages", return_value={"yay": "12.0-1"}), \
             patch("sysforge.update.resolve_state_dir", return_value=(tmp_path, "test")), \
             patch("sysforge.update.parse_pkgbuild",
                   return_value={"globals": {"pkgname": "yay", "pkgver": "12.3.3", "pkgrel": "1", "epoch": "0"}}), \
             patch("sysforge.update.vercmp", return_value=1):
            MockBS.return_value.all_packages.return_value = {}
            cmd_update(args)
    finally:
        mw.run = mw_run_orig

    assert build_calls == []
