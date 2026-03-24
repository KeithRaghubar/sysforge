"""
test_aur.py — unit tests for sysforge.primitives.aur

Covers:
    aur_info              — successful batch query, empty result, network error, bad JSON
    aur_clone             — successful clone, git failure
    git_pull_rebase       — not-a-repo skip, no-tracking skip, success, conflict abort+raise
    fetch_aur_name_cache  — fresh cache skip, download + write, network failure, force refresh
"""
import gzip
import json
import subprocess
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.aur import aur_clone, aur_info, fetch_aur_name_cache, git_is_dirty, git_pull_rebase, import_pgp_keys, is_repo_package, pkgctl_checkout, repo_packages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUR_RESPONSE_FOUND = {
    "version": 5,
    "type": "multiinfo",
    "resultcount": 2,
    "results": [
        {"ID": 1, "Name": "mesa-git",       "Version": "24.0-1"},
        {"ID": 2, "Name": "cosmic-comp-git","Version": "0.1-1"},
    ],
}

AUR_RESPONSE_EMPTY = {
    "version": 5,
    "type": "multiinfo",
    "resultcount": 0,
    "results": [],
}


def _mock_urlopen(response_dict):
    """Return a context-manager mock that yields a readable HTTP response."""
    body = json.dumps(response_dict).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# aur_info
# ---------------------------------------------------------------------------

def test_aur_info_returns_found_packages():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_FOUND)):
        result = aur_info(["mesa-git", "cosmic-comp-git", "nonexistent"])
    assert "mesa-git" in result
    assert "cosmic-comp-git" in result
    assert "nonexistent" not in result


def test_aur_info_empty_names_returns_empty():
    result = aur_info([])
    assert result == {}


def test_aur_info_empty_results():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_EMPTY)):
        result = aur_info(["totally-fake-pkg"])
    assert result == {}


def test_aur_info_network_error_returns_empty():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        result = aur_info(["mesa-git"])
    assert result == {}


def test_aur_info_bad_json_returns_empty():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json {"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = aur_info(["mesa-git"])
    assert result == {}


def test_aur_info_result_contains_version():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(AUR_RESPONSE_FOUND)):
        result = aur_info(["mesa-git"])
    assert result["mesa-git"]["Version"] == "24.0-1"


def test_aur_info_url_contains_arg_brackets():
    """Verify the query string uses literal arg[] keys (not percent-encoded)."""
    captured_url = []

    def fake_urlopen(url, timeout=None):
        captured_url.append(url)
        return _mock_urlopen(AUR_RESPONSE_EMPTY)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        aur_info(["htop"])

    assert "arg[]=" in captured_url[0]


# ---------------------------------------------------------------------------
# aur_clone
# ---------------------------------------------------------------------------

def test_aur_clone_success(tmp_path):
    dest = tmp_path / "mesa-git"

    def fake_run(cmd, **kwargs):
        # Simulate git creating the directory with a PKGBUILD
        dest.mkdir()
        (dest / "PKGBUILD").write_text("pkgname=mesa-git\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("mesa-git", dest)

    assert (dest / "PKGBUILD").exists()


def test_aur_clone_command_uses_aur_url():
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("mesa-git", Path("/tmp/mesa-git"))

    assert captured[0][0] == "git"
    assert "aur.archlinux.org" in captured[0][2]
    assert "mesa-git" in captured[0][2]


def test_aur_clone_failure_raises():
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="repository not found")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="AUR clone failed"):
            aur_clone("nonexistent-pkg-xyz", Path("/tmp/nonexistent"))


# ---------------------------------------------------------------------------
# is_repo_package
# ---------------------------------------------------------------------------

def test_is_repo_package_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
        assert is_repo_package("htop") is True


def test_is_repo_package_not_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)):
        assert is_repo_package("totally-fake-aur-only-pkg") is False


def test_is_repo_package_calls_pacman_si():
    captured = []
    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_run):
        is_repo_package("htop")

    assert captured[0][:3] == ["pacman", "-Si", "htop"]


# ---------------------------------------------------------------------------
# repo_packages
# ---------------------------------------------------------------------------

_PACMAN_SI_OUTPUT = """\
Repository      : extra
Name            : htop
Version         : 3.3.0-2

Repository      : core
Name            : gcc
Version         : 14.2.1-3
"""

def test_repo_packages_all_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, stdout=_PACMAN_SI_OUTPUT, stderr=""
    )):
        result = repo_packages(["htop", "gcc"])
    assert result == {"htop", "gcc"}


def test_repo_packages_some_found():
    # pacman exits non-zero when any name is missing; stdout still has found packages
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
        [], 1, stdout=_PACMAN_SI_OUTPUT, stderr="error: package 'yay' was not found\n"
    )):
        result = repo_packages(["htop", "gcc", "yay"])
    assert result == {"htop", "gcc"}


def test_repo_packages_none_found():
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
        [], 1, stdout="", stderr="error: package 'yay' was not found\n"
    )):
        result = repo_packages(["yay"])
    assert result == set()


def test_repo_packages_empty():
    with patch("subprocess.run") as mock_run:
        result = repo_packages([])
    mock_run.assert_not_called()
    assert result == set()


def test_repo_packages_single_invocation():
    """All names are checked in one pacman -Si call."""
    captured = []
    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        repo_packages(["htop", "gcc", "vim"])

    assert len(captured) == 1
    assert captured[0][:2] == ["pacman", "-Si"]
    assert set(captured[0][2:]) == {"htop", "gcc", "vim"}


# ---------------------------------------------------------------------------
# pkgctl_checkout
# ---------------------------------------------------------------------------

def test_pkgctl_checkout_success(tmp_path):
    pkg_dir = tmp_path / "htop"

    def fake_run(cmd, **kwargs):
        # Simulate pkgctl creating the directory with a PKGBUILD
        pkg_dir.mkdir()
        (pkg_dir / "PKGBUILD").write_text("pkgname=htop\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        pkgctl_checkout("htop", pkg_dir)

    assert (pkg_dir / "PKGBUILD").exists()


def test_pkgctl_checkout_runs_in_parent(tmp_path):
    captured = []
    pkg_dir = tmp_path / "htop"

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        pkgctl_checkout("htop", pkg_dir)

    cmd, kwargs = captured[0]
    assert cmd[:3] == ["pkgctl", "repo", "clone"]
    assert "--protocol=https" in cmd
    assert "htop" in cmd
    assert kwargs.get("cwd") == str(tmp_path)


def test_pkgctl_checkout_failure_raises(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: package not found")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="pkgctl checkout failed"):
            pkgctl_checkout("nonexistent", tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# import_pgp_keys
# ---------------------------------------------------------------------------

def _pkgmeta(keys):
    return {"globals": {"validpgpkeys": keys}}


def _fake_pkgbuild(tmp_path, asc_keys=None):
    """Create a minimal PKGBUILD dir, optionally with keys/pgp/*.asc files."""
    pb = tmp_path / "PKGBUILD"
    pb.write_text("pkgname=test\n")
    if asc_keys:
        keys_dir = tmp_path / "keys" / "pgp"
        keys_dir.mkdir(parents=True)
        for name, content in asc_keys.items():
            (keys_dir / name).write_text(content)
    return pb


def test_import_pgp_keys_no_keys_does_nothing(tmp_path):
    """No subprocess calls when validpgpkeys is absent."""
    pb = _fake_pkgbuild(tmp_path)
    with patch("subprocess.run") as mock_run:
        import_pgp_keys({"globals": {}}, pb)
    mock_run.assert_not_called()


def test_import_pgp_keys_all_present_no_recv(tmp_path):
    """No gpg --recv-keys when all keys are already in keyring."""
    pb = _fake_pkgbuild(tmp_path)
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_run):
        import_pgp_keys(_pkgmeta(["AABBCCDD"]), pb)

    assert all("--recv-keys" not in cmd for cmd in calls)


def test_import_pgp_keys_missing_triggers_recv(tmp_path):
    """Missing keys trigger a single gpg --recv-keys call."""
    pb = _fake_pkgbuild(tmp_path)

    def fake_run(cmd, **kwargs):
        if "--list-keys" in cmd:
            return subprocess.CompletedProcess(cmd, 2)  # not found
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        import_pgp_keys(_pkgmeta(["AABBCCDD", "11223344"]), pb)

    recv_calls = [c for c in mock_run.call_args_list if "--recv-keys" in c.args[0]]
    assert len(recv_calls) == 1
    recv_cmd = recv_calls[0].args[0]
    assert "AABBCCDD" in recv_cmd
    assert "11223344" in recv_cmd


def test_import_pgp_keys_recv_failure_warns_not_raises(tmp_path):
    """A failing gpg --recv-keys logs a warning but does not raise."""
    pb = _fake_pkgbuild(tmp_path)

    def fake_run(cmd, **kwargs):
        if "--list-keys" in cmd:
            return subprocess.CompletedProcess(cmd, 2)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="keyserver timeout")

    with patch("subprocess.run", side_effect=fake_run):
        import_pgp_keys(_pkgmeta(["AABBCCDD"]), pb)   # should not raise


def test_import_pgp_keys_partial_missing(tmp_path):
    """Only missing keys are sent to --recv-keys."""
    pb = _fake_pkgbuild(tmp_path)

    def fake_run(cmd, **kwargs):
        if "--list-keys" in cmd:
            key = cmd[-1]
            return subprocess.CompletedProcess(cmd, 0 if key == "AABBCCDD" else 2)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        import_pgp_keys(_pkgmeta(["AABBCCDD", "11223344"]), pb)

    recv_calls = [c for c in mock_run.call_args_list if "--recv-keys" in c.args[0]]
    recv_cmd = recv_calls[0].args[0]
    assert "11223344" in recv_cmd
    assert "AABBCCDD" not in recv_cmd


def test_import_pgp_keys_bundled_asc_imported(tmp_path):
    """keys/pgp/*.asc files are passed to gpg --import before keyserver check."""
    pb = _fake_pkgbuild(tmp_path, asc_keys={"AABBCCDD.asc": "fake key data"})

    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch("subprocess.run", side_effect=fake_run):
        import_pgp_keys(_pkgmeta(["AABBCCDD"]), pb)

    import_calls = [c for c in calls if "--import" in c]
    assert len(import_calls) == 1
    assert any("AABBCCDD.asc" in arg for arg in import_calls[0])


def test_import_pgp_keys_bundled_satisfies_no_recv(tmp_path):
    """When bundled import makes all keys present, --recv-keys is not called."""
    pb = _fake_pkgbuild(tmp_path, asc_keys={"AABBCCDD.asc": "fake key data"})

    def fake_run(cmd, **kwargs):
        # --import succeeds; --list-keys finds the key after import
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        import_pgp_keys(_pkgmeta(["AABBCCDD"]), pb)

    recv_calls = [c for c in mock_run.call_args_list if "--recv-keys" in c.args[0]]
    assert recv_calls == []


def test_import_pgp_keys_bundled_fails_falls_back_to_recv(tmp_path):
    """If bundled import fails to satisfy a key, keyserver is still tried."""
    pb = _fake_pkgbuild(tmp_path, asc_keys={"AABBCCDD.asc": "fake key data"})

    def fake_run(cmd, **kwargs):
        if "--list-keys" in cmd:
            return subprocess.CompletedProcess(cmd, 2)  # still missing after import
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run) as mock_run:
        import_pgp_keys(_pkgmeta(["AABBCCDD"]), pb)

    recv_calls = [c for c in mock_run.call_args_list if "--recv-keys" in c.args[0]]
    assert len(recv_calls) == 1


# ---------------------------------------------------------------------------
# git_is_dirty
# ---------------------------------------------------------------------------

def test_git_is_dirty_not_a_repo(tmp_path):
    """Plain directory — returns False silently."""
    not_repo = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="")
    def fake_run(cmd, **kwargs):
        return not_repo
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is False


def test_git_is_dirty_clean_repo(tmp_path):
    """Git repo with no modifications — returns False."""
    def fake_run(cmd, **kwargs):
        if "--git-dir" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        # status --short --untracked-files=no returns empty output
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is False


def test_git_is_dirty_modified_files(tmp_path):
    """Git repo with modified tracked files — returns True."""
    def fake_run(cmd, **kwargs):
        if "--git-dir" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=" M PKGBUILD\n", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is True


def test_git_is_dirty_staged_changes(tmp_path):
    """Git repo with staged changes — returns True."""
    def fake_run(cmd, **kwargs):
        if "--git-dir" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="M  PKGBUILD\n", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is True


def test_git_is_dirty_ignores_untracked(tmp_path):
    """Untracked files (e.g. build artifacts) don't count as dirty."""
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is False
    # Confirm --untracked-files=no was passed to the status call
    status_calls = [c for c in calls if "status" in c]
    assert any("--untracked-files=no" in c for c in status_calls)


def test_git_is_dirty_no_tracking_branch(tmp_path):
    """Repo with no upstream tracking branch is treated as dirty."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="no upstream")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is True


def test_git_is_dirty_unpushed_commits(tmp_path):
    """Repo with local commits not on the upstream is dirty."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="2", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is True


def test_git_is_dirty_clean_and_synced(tmp_path):
    """Repo with no uncommitted changes, a tracking branch, and zero unpushed commits."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        assert git_is_dirty(tmp_path) is False


def test_git_is_dirty_checks_rev_list_after_clean_status(tmp_path):
    """Verify rev-list is called even when status is clean."""
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        git_is_dirty(tmp_path)
    rev_list_calls = [c for c in calls if "rev-list" in c]
    assert len(rev_list_calls) == 1
    assert "@{u}..HEAD" in rev_list_calls[0]


# ---------------------------------------------------------------------------
# git_pull_rebase
# ---------------------------------------------------------------------------

def test_git_pull_rebase_not_a_repo_skips(tmp_path):
    """Plain directory with no .git — should return silently."""
    not_repo = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="not a git repo")
    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "--git-dir" in cmd:
            return not_repo
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        git_pull_rebase(tmp_path)  # no exception


def test_git_pull_rebase_no_tracking_skips(tmp_path):
    """Git repo but no tracking branch — should return silently."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="no upstream")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        git_pull_rebase(tmp_path)  # no exception


def test_git_pull_rebase_success(tmp_path):
    """Successful pull logs output and returns."""
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        if "pull" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        git_pull_rebase(tmp_path)

    pull_calls = [c for c in calls if "pull" in c]
    assert len(pull_calls) == 1
    assert "--rebase" in pull_calls[0]


def test_git_pull_rebase_conflict_aborts_and_raises(tmp_path):
    """Merge conflict → git rebase --abort is called and RuntimeError raised."""
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        if "pull" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1,
                stdout="CONFLICT (content): Merge conflict in PKGBUILD",
                stderr="error: could not apply abc1234",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="git pull --rebase failed"):
        with patch("subprocess.run", side_effect=fake_run):
            git_pull_rebase(tmp_path)

    abort_calls = [c for c in calls if "rebase" in c and "--abort" in c]
    assert len(abort_calls) == 1, "rebase --abort must be called on conflict"


def test_git_pull_rebase_uses_git_dash_c(tmp_path):
    """All git invocations use git -C <dir> rather than relying on cwd."""
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        git_pull_rebase(tmp_path)

    for cmd in calls:
        assert cmd[0] == "git"
        assert cmd[1] == "-C"
        assert cmd[2] == str(tmp_path)


# ---------------------------------------------------------------------------
# fetch_aur_name_cache
# ---------------------------------------------------------------------------

def _gz_names(names: list[str]) -> bytes:
    return gzip.compress("\n".join(names).encode())


def _mock_aur_packages_response(names: list[str]):
    body = _gz_names(names)
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_fetch_aur_name_cache_downloads_and_writes(tmp_path):
    cache = tmp_path / "aur-packages.txt"
    names = ["yay", "paru", "trizen"]

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("urllib.request.urlopen", return_value=_mock_aur_packages_response(names)):
        result = fetch_aur_name_cache()

    assert result == cache
    written = cache.read_text().splitlines()
    assert "yay" in written
    assert "paru" in written
    assert "trizen" in written


def test_fetch_aur_name_cache_skips_if_fresh(tmp_path):
    cache = tmp_path / "aur-packages.txt"
    cache.write_text("yay\nparu\n")

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("sysforge.primitives.aur.AUR_CACHE_MAX_AGE", 86400), \
         patch("sysforge.primitives.aur.time") as mock_time, \
         patch("urllib.request.urlopen") as mock_urlopen:
        mock_time.time.return_value = cache.stat().st_mtime + 3600  # 1 hour old
        result = fetch_aur_name_cache()

    mock_urlopen.assert_not_called()
    assert result == cache


def test_fetch_aur_name_cache_refreshes_if_stale(tmp_path):
    cache = tmp_path / "aur-packages.txt"
    cache.write_text("oldpkg\n")
    names = ["newpkg"]

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("sysforge.primitives.aur.AUR_CACHE_MAX_AGE", 86400), \
         patch("sysforge.primitives.aur.time") as mock_time, \
         patch("urllib.request.urlopen", return_value=_mock_aur_packages_response(names)):
        mock_time.time.return_value = cache.stat().st_mtime + 90000  # >1 day old
        result = fetch_aur_name_cache()

    assert result == cache
    assert "newpkg" in cache.read_text()


def test_fetch_aur_name_cache_force_bypasses_freshness(tmp_path):
    cache = tmp_path / "aur-packages.txt"
    cache.write_text("oldpkg\n")
    names = ["forced-pkg"]

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("urllib.request.urlopen", return_value=_mock_aur_packages_response(names)):
        result = fetch_aur_name_cache(force=True)

    assert result == cache
    assert "forced-pkg" in cache.read_text()


def test_fetch_aur_name_cache_network_error_returns_none(tmp_path):
    import urllib.error
    cache = tmp_path / "aur-packages.txt"

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        result = fetch_aur_name_cache()

    assert result is None
    assert not cache.exists()


def test_fetch_aur_name_cache_creates_parent_dirs(tmp_path):
    cache = tmp_path / "nested" / "dir" / "aur-packages.txt"
    names = ["yay"]

    with patch("sysforge.primitives.aur.AUR_CACHE_PATH", cache), \
         patch("urllib.request.urlopen", return_value=_mock_aur_packages_response(names)):
        result = fetch_aur_name_cache()

    assert result == cache
    assert cache.exists()
