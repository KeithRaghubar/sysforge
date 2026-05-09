"""
test_aur.py — unit tests for sysforge.primitives.aur

Covers:
    aur_info               — successful batch query, empty result, network error, bad JSON
    aur_clone              — successful clone, git failure
    git_fetch_and_compare  — not-a-repo skip, no-tracking skip, up_to_date, fetched, diverged, rate-limited
    is_{transient,rate_limit}_git_error — classifier smoke tests
    fetch_aur_name_cache   — fresh cache skip, download + write, network failure, force refresh
"""
import gzip
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives.aur import (
    aur_clone,
    aur_info,
    classify_head_vs_upstream,
    fetch_aur_name_cache,
    git_fetch_and_compare,
    git_is_dirty,
    import_pgp_keys,
    is_rate_limit_error,
    is_repo_package,
    is_transient_git_error,
    pkgctl_checkout,
    purge_src,
    repo_packages,
)


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


def test_aur_clone_timeout_raises_and_cleans_up(tmp_path):
    dest = tmp_path / "slow-pkg"
    dest.mkdir()  # simulate partial clone directory

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    with patch("subprocess.run", side_effect=fake_run), patch("time.sleep"):
        with pytest.raises(RuntimeError, match="timed out after 60s.*after retry"):
            aur_clone("slow-pkg", dest, timeout=60)

    assert not dest.exists(), "partial clone directory should be cleaned up"


def test_aur_clone_transient_error_retries_then_succeeds(tmp_path):
    dest = tmp_path / "flaky-pkg"
    attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            dest.mkdir()  # partial clone dir
            return subprocess.CompletedProcess(
                cmd, 128, stdout="",
                stderr="fatal: unable to access 'https://aur.archlinux.org/flaky-pkg.git/': "
                       "Recv failure: Connection reset by peer",
            )
        dest.mkdir(exist_ok=True)
        (dest / "PKGBUILD").write_text("pkgname=flaky-pkg\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run), patch("time.sleep"):
        aur_clone("flaky-pkg", dest)

    assert attempts == 2
    assert (dest / "PKGBUILD").exists()


def test_aur_clone_transient_error_retries_then_raises(tmp_path):
    dest = tmp_path / "broken-pkg"

    def fake_run(cmd, **kwargs):
        dest.mkdir(exist_ok=True)
        return subprocess.CompletedProcess(
            cmd, 128, stdout="",
            stderr="fatal: unable to access '...': Recv failure: Connection reset by peer",
        )

    with patch("subprocess.run", side_effect=fake_run), patch("time.sleep"):
        with pytest.raises(RuntimeError, match="AUR clone failed"):
            aur_clone("broken-pkg", dest)


def test_aur_clone_non_transient_error_does_not_retry(tmp_path):
    """Non-transient errors (e.g. repo not found) should raise immediately without retry."""
    attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="repository not found")

    with patch("subprocess.run", side_effect=fake_run), patch("time.sleep"):
        with pytest.raises(RuntimeError, match="AUR clone failed"):
            aur_clone("nonexistent-pkg", tmp_path / "nonexistent")

    assert attempts == 1, "non-transient errors must not retry"


def test_aur_clone_timeout_zero_disables():
    """timeout=0 should pass None to subprocess (no timeout)."""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("pkg", Path("/tmp/pkg"), timeout=0)

    assert captured[0].get("timeout") is None


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

class _FakePopen:
    """Minimal Popen stand-in for pkgctl_checkout tests.

    `lines` is the stdout stream that `_drain` will iterate.
    `returncode_after_wait` is what `proc.returncode` becomes after `.wait()`.
    `wait_raises` (optional) is an exception raised by `.wait()` (e.g. TimeoutExpired).
    `on_start` is invoked once during construction so tests can simulate
    side effects pkgctl would have (creating the dest dir).
    """
    def __init__(self, cmd, *, lines=(), returncode_after_wait=0,
                 wait_raises=None, on_start=None, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = iter(lines)
        self._returncode = None
        self._returncode_after_wait = returncode_after_wait
        self._wait_raises = wait_raises
        self.killed = False
        if on_start is not None:
            on_start()

    def wait(self, timeout=None):
        if self._wait_raises is not None:
            # Raise on the first call (the timed wait), then behave normally
            # for the post-kill wait.
            exc = self._wait_raises
            self._wait_raises = None
            raise exc
        self._returncode = self._returncode_after_wait
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9

    @property
    def returncode(self):
        return self._returncode


def _popen_factory(*, lines=(), returncode_after_wait=0, wait_raises=None,
                   on_start=None, captured=None):
    def factory(cmd, **kwargs):
        if captured is not None:
            captured.append((cmd, kwargs))
        return _FakePopen(
            cmd,
            lines=lines,
            returncode_after_wait=returncode_after_wait,
            wait_raises=wait_raises,
            on_start=on_start,
            **kwargs,
        )
    return factory


def test_pkgctl_checkout_success(tmp_path):
    pkg_dir = tmp_path / "htop"

    def on_start():
        pkg_dir.mkdir()
        (pkg_dir / "PKGBUILD").write_text("pkgname=htop\n")

    with patch("subprocess.Popen", side_effect=_popen_factory(on_start=on_start)):
        pkgctl_checkout("htop", pkg_dir)

    assert (pkg_dir / "PKGBUILD").exists()


def test_pkgctl_checkout_runs_in_parent(tmp_path):
    captured = []
    pkg_dir = tmp_path / "htop"

    with patch("subprocess.Popen", side_effect=_popen_factory(captured=captured)):
        pkgctl_checkout("htop", pkg_dir)

    cmd, kwargs = captured[0]
    assert cmd[:3] == ["pkgctl", "repo", "clone"]
    assert "--protocol=https" in cmd
    assert "htop" in cmd
    assert kwargs.get("cwd") == str(tmp_path)


def test_pkgctl_checkout_failure_raises(tmp_path):
    factory = _popen_factory(
        lines=["error: package not found\n"],
        returncode_after_wait=1,
    )
    with patch("subprocess.Popen", side_effect=factory):
        with pytest.raises(RuntimeError, match="pkgctl checkout failed"):
            pkgctl_checkout("nonexistent", tmp_path / "nonexistent")


def test_pkgctl_checkout_timeout_raises_and_cleans_up(tmp_path):
    dest = tmp_path / "slow-pkg"
    dest.mkdir()

    factory = _popen_factory(
        wait_raises=subprocess.TimeoutExpired(cmd=["pkgctl"], timeout=60),
    )
    with patch("subprocess.Popen", side_effect=factory):
        with pytest.raises(RuntimeError, match="timed out after 60s"):
            pkgctl_checkout("slow-pkg", dest, timeout=60)

    assert not dest.exists(), "partial checkout directory should be cleaned up"


def test_pkgctl_checkout_creates_parent_dir(tmp_path):
    """
    A fresh system may not have pkgbuild_src_dir yet. pkgctl_checkout sets
    cwd=str(dest.parent) on the subprocess, which raises FileNotFoundError
    before the binary even runs if the parent doesn't exist. Guard with mkdir.
    """
    parent = tmp_path / "src"  # deliberately not created
    dest = parent / "htop"
    assert not parent.exists()

    captured = {}

    def factory(cmd, **kwargs):
        captured["parent_exists"] = Path(kwargs["cwd"]).exists()
        return _FakePopen(cmd, **kwargs)

    with patch("subprocess.Popen", side_effect=factory):
        pkgctl_checkout("htop", dest)

    assert captured.get("parent_exists") is True


def test_pkgctl_checkout_streams_output_to_log(tmp_path):
    """Lines from pkgctl/git progress must reach the build log so -vvv shows
    progress instead of a silent multi-minute wait."""
    factory = _popen_factory(
        lines=[
            "==> Cloning htop ...\n",
            "Cloning into 'htop'...\n",
            "Receiving objects: 100% (123/123), done.\n",
        ],
    )
    debug_calls = []
    with patch("subprocess.Popen", side_effect=factory), \
         patch("sysforge.primitives.aur._build_log") as mock_log:
        mock_log.debug.side_effect = lambda msg: debug_calls.append(msg)
        pkgctl_checkout("htop", tmp_path / "htop")

    assert "==> Cloning htop ..." in debug_calls
    assert "Cloning into 'htop'..." in debug_calls
    assert "Receiving objects: 100% (123/123), done." in debug_calls


def test_aur_clone_creates_parent_dir(tmp_path):
    """
    git clone <url> <dest> requires dest's parent to exist. aur_clone must
    create it so a fresh system without ~/src works.
    """
    parent = tmp_path / "src"  # deliberately not created
    dest = parent / "mesa-git"
    assert not parent.exists()

    def fake_run(cmd, **kwargs):
        # Verify parent existed at the moment subprocess was about to run
        assert dest.parent.exists()
        dest.mkdir()
        (dest / "PKGBUILD").write_text("pkgname=mesa-git\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        aur_clone("mesa-git", dest)

    assert (dest / "PKGBUILD").exists()


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
    """Verify both rev-list directions are queried (ahead + behind)."""
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
    assert any("@{u}..HEAD" in c for c in rev_list_calls)
    assert any("HEAD..@{u}" in c for c in rev_list_calls)


# ---------------------------------------------------------------------------
# purge_src
# ---------------------------------------------------------------------------

def test_purge_src_nonexistent_is_noop(tmp_path):
    """Missing dir → silent no-op, never invokes git."""
    target = tmp_path / "missing"
    purge_src(target)
    assert not target.exists()


def test_purge_src_non_git_dir_purges_unconditionally(tmp_path):
    """Plain directory with no .git is removed without dirty checks."""
    target = tmp_path / "plain"
    target.mkdir()
    (target / "file.txt").write_text("hi")

    not_repo = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="not a git repo")
    with patch("subprocess.run", return_value=not_repo):
        purge_src(target)
    assert not target.exists()


def test_purge_src_clean_repo_purges(tmp_path):
    """Clean git repo with upstream and zero unpushed commits → purged."""
    target = tmp_path / "clean"
    target.mkdir()
    (target / "PKGBUILD").write_text("pkgname=foo")

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "@{u}" in cmd_str and "rev-list" not in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="0", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        purge_src(target)
    assert not target.exists()


def test_purge_src_dirty_worktree_raises(tmp_path):
    """Uncommitted changes → RuntimeError, dir is preserved."""
    target = tmp_path / "dirty"
    target.mkdir()
    (target / "PKGBUILD").write_text("pkgname=foo")

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M PKGBUILD", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="refusing to purge"):
            purge_src(target)
    assert target.exists()
    assert (target / "PKGBUILD").exists()


def test_purge_src_unpushed_commits_raises(tmp_path):
    """Clean worktree but unpushed commits → RuntimeError, dir preserved."""
    target = tmp_path / "ahead"
    target.mkdir()

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "@{u}" in cmd_str and "rev-list" not in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main", stderr="")
        if "rev-list" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="3", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="refusing to purge"):
            purge_src(target)
    assert target.exists()


def test_purge_src_no_upstream_raises(tmp_path):
    """Repo with no tracking branch (entirely local) → refused."""
    target = tmp_path / "local-only"
    target.mkdir()

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)
        if "--git-dir" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout=".git", stderr="")
        if "status" in cmd_str:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "@{u}" in cmd_str:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="no upstream")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError, match="refusing to purge"):
            purge_src(target)
    assert target.exists()


# ---------------------------------------------------------------------------
# purge_src force=True bypasses the dirty-tree guard
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    """Run a git command, asserting success. Helper for the real-git tests."""
    subprocess.run(["git", "-C", str(cwd), *args],
                   check=True, capture_output=True)


def _seed_upstream_and_local(tmp_path: Path, *,
                             local_email: str = "local@example.test",
                             rewrite_upstream: bool = False,
                             upstream_extra: int = 0,
                             local_extra: int = 0,
                             local_authored_extra: bool = False) -> Path:
    """Seed an upstream + clone with controllable ahead/behind/diverged shape.

    Returns the local clone Path. The remote tracking branch is named
    ``main`` and the clone has ``user.email`` set to ``local_email`` so
    authorship-based classification has a stable identity to compare against.
    """
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=upstream)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--initial-branch=main", cwd=seed)
    _git("config", "user.email", "upstream@example.test", cwd=seed)
    _git("config", "user.name", "upstream", cwd=seed)
    _git("config", "commit.gpgsign", "false", cwd=seed)
    (seed / "PKGBUILD").write_text("pkgname=foo\n")
    _git("add", "PKGBUILD", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("remote", "add", "origin", str(upstream), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)

    local = tmp_path / "local"
    _git("clone", str(upstream), str(local), cwd=tmp_path)
    _git("config", "user.email", local_email, cwd=local)
    _git("config", "user.name", "local", cwd=local)
    _git("config", "commit.gpgsign", "false", cwd=local)

    if rewrite_upstream:
        # Force-push a rewritten history on upstream: same logical content,
        # different SHAs. Mirrors what gitlab.archlinux.org's pkgctl release
        # flow does each upgpkg.
        _git("checkout", "--orphan", "rewrite", cwd=seed)
        (seed / "PKGBUILD").write_text("pkgname=foo\n# rewritten\n")
        _git("add", "PKGBUILD", cwd=seed)
        _git("commit", "-m", "rewritten initial", cwd=seed)
        _git("branch", "-M", "main", cwd=seed)
        _git("push", "--force", "origin", "main", cwd=seed)
        # Don't pull into local — leaving local's HEAD on the original SHA
        # is what creates the diverged-upstream-only state.

    for i in range(upstream_extra):
        (seed / f"u{i}.txt").write_text(f"u{i}")
        _git("add", f"u{i}.txt", cwd=seed)
        _git("commit", "-m", f"upstream-{i}", cwd=seed)
        _git("push", "origin", "main", cwd=seed)

    for i in range(local_extra):
        author = local_email if local_authored_extra else "upstream@example.test"
        env_name = "local" if local_authored_extra else "upstream"
        (local / f"l{i}.txt").write_text(f"l{i}")
        _git("add", f"l{i}.txt", cwd=local)
        subprocess.run(
            ["git", "-C", str(local), "-c", f"user.email={author}",
             "-c", f"user.name={env_name}",
             "commit", "-m", f"local-{i}"],
            check=True, capture_output=True,
        )

    # Refresh local's tracking ref so @{u} reflects the actual upstream tip
    # after a force-push.
    _git("fetch", "origin", cwd=local)
    return local


def test_classify_clean_repo_returns_clean(tmp_path):
    local = _seed_upstream_and_local(tmp_path)
    state, n_local, n_upstream = classify_head_vs_upstream(local)
    assert state == "clean"
    assert n_local == 0
    assert n_upstream == 0


def test_classify_behind_only(tmp_path):
    """Upstream advanced; local has nothing extra → behind."""
    local = _seed_upstream_and_local(tmp_path, upstream_extra=2)
    state, n_local, n_upstream = classify_head_vs_upstream(local)
    assert state == "behind"
    assert n_local == 0
    assert n_upstream == 2


def test_classify_ahead_only(tmp_path):
    """Local has unpushed commits, upstream unchanged → ahead."""
    local = _seed_upstream_and_local(
        tmp_path, local_extra=3, local_authored_extra=True,
    )
    state, n_local, n_upstream = classify_head_vs_upstream(local)
    assert state == "ahead"
    assert n_local == 3
    assert n_upstream == 0


def test_classify_diverged_upstream_only(tmp_path):
    """Upstream force-pushed; no local commits authored by local user.

    The exact reproduction of Keith's LLVM workstation state.
    """
    local = _seed_upstream_and_local(tmp_path, rewrite_upstream=True)
    state, n_local, n_upstream = classify_head_vs_upstream(local)
    assert state == "diverged_upstream"
    assert n_local >= 1
    assert n_upstream >= 1


def test_classify_diverged_user(tmp_path):
    """Upstream force-pushed AND local user authored a divergent commit."""
    local = _seed_upstream_and_local(
        tmp_path,
        rewrite_upstream=True,
        local_extra=1,
        local_authored_extra=True,
    )
    state, n_local, n_upstream = classify_head_vs_upstream(local)
    assert state == "diverged_user"
    assert n_local >= 2
    assert n_upstream >= 1


def test_git_is_dirty_diverged_upstream_returns_clean(tmp_path):
    """Force-pushed upstream with no local user commits → not dirty."""
    local = _seed_upstream_and_local(tmp_path, rewrite_upstream=True)
    assert git_is_dirty(local) is False


def test_git_is_dirty_diverged_user_returns_dirty(tmp_path):
    """Divergence with at least one local-user-authored commit → dirty."""
    local = _seed_upstream_and_local(
        tmp_path, rewrite_upstream=True,
        local_extra=1, local_authored_extra=True,
    )
    assert git_is_dirty(local) is True


def test_purge_src_force_skips_dirty_check(tmp_path):
    """force=True purges even when git_is_dirty would refuse."""
    local = _seed_upstream_and_local(
        tmp_path, local_extra=2, local_authored_extra=True,
    )
    assert git_is_dirty(local) is True
    purge_src(local, force=True)
    assert not local.exists()


def test_purge_src_force_logs_forced_marker(tmp_path):
    """The 'forced' marker is logged so the operator can audit."""
    local = _seed_upstream_and_local(
        tmp_path, local_extra=1, local_authored_extra=True,
    )
    with patch("sysforge.primitives.aur._git_log") as mock_log:
        purge_src(local, force=True)
    msg = mock_log.warn.call_args.args[0]
    assert "(forced)" in msg


def test_dirty_reason_ahead_label_via_classify(tmp_path):
    """Sanity-check the ahead label produced by llvm_state._dirty_reason."""
    from sysforge.primitives.llvm_state import _dirty_reason
    local = _seed_upstream_and_local(
        tmp_path, local_extra=4, local_authored_extra=True,
    )
    is_dirty, reason = _dirty_reason(local)
    assert is_dirty is True
    assert reason == "4 commits ahead of upstream"


def test_dirty_reason_diverged_user_label(tmp_path):
    from sysforge.primitives.llvm_state import _dirty_reason
    local = _seed_upstream_and_local(
        tmp_path, rewrite_upstream=True,
        local_extra=2, local_authored_extra=True,
    )
    is_dirty, reason = _dirty_reason(local)
    assert is_dirty is True
    assert reason and reason.startswith("diverged from upstream (")


def test_dirty_reason_diverged_upstream_clean(tmp_path):
    """Force-pushed upstream / no user-authored commits → not dirty."""
    from sysforge.primitives.llvm_state import _dirty_reason
    local = _seed_upstream_and_local(tmp_path, rewrite_upstream=True)
    is_dirty, reason = _dirty_reason(local)
    assert is_dirty is False
    assert reason is None


# ---------------------------------------------------------------------------
# git_fetch_and_compare
# ---------------------------------------------------------------------------

def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        ["git"], returncode, stdout=stdout, stderr=stderr,
    )


def test_git_fetch_and_compare_not_a_repo_skips(tmp_path):
    """Plain directory with no .git — returns not_a_repo, no network call."""
    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "--git-dir" in cmd:
            return _cp(returncode=128, stderr="not a git repo")
        raise AssertionError(f"unexpected git call after not-a-repo: {cmd}")

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "not_a_repo"


def test_git_fetch_and_compare_no_tracking_skips(tmp_path):
    """Git repo but no tracking branch — returns no_tracking."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(returncode=128, stderr="no upstream")
        raise AssertionError(f"unexpected git call after no-tracking: {cmd}")

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "no_tracking"


def test_git_fetch_and_compare_up_to_date(tmp_path):
    """HEAD == FETCH_HEAD after shallow fetch → up_to_date, no merge."""
    head = "a" * 40

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:5] == ["rev-parse", "HEAD"] or cmd[3:5] == ["rev-parse", "FETCH_HEAD"]:
            return _cp(stdout=head)
        if "fetch" in cmd:
            return _cp()
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "up_to_date"
    assert outcome.head_before == head
    assert outcome.head_after == head


def test_git_fetch_and_compare_fetched_fast_forward(tmp_path):
    """HEAD is ancestor of FETCH_HEAD → ff-merge succeeds → fetched."""
    calls = []
    old = "a" * 40
    new = "b" * 40

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:6] == ["rev-parse", "--verify", "--quiet"]:
            return _cp()  # HEAD exists (empty-repo probe in git_is_dirty)
        if cmd[3:5] == ["rev-parse", "HEAD"]:
            if any("merge" in c for c in calls[:-1]):
                return _cp(stdout=new)  # post-merge
            return _cp(stdout=old)
        if cmd[3:5] == ["rev-parse", "FETCH_HEAD"]:
            return _cp(stdout=new)
        if "fetch" in cmd:
            return _cp()
        if "merge-base" in cmd:
            return _cp()  # HEAD is ancestor
        if "diff-index" in cmd or "status" in cmd or "rev-list" in cmd:
            return _cp()  # clean working tree
        if "merge" in cmd and "--ff-only" in cmd:
            return _cp()
        raise AssertionError(f"unexpected command: {cmd}")

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "fetched"
    assert outcome.head_before == old
    assert outcome.head_after == new


def test_git_fetch_and_compare_diverged_not_ancestor(tmp_path):
    """HEAD is NOT ancestor of FETCH_HEAD → diverged, no merge."""
    old = "a" * 40
    new = "b" * 40
    merge_calls = []

    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:5] == ["rev-parse", "HEAD"]:
            return _cp(stdout=old)
        if cmd[3:5] == ["rev-parse", "FETCH_HEAD"]:
            return _cp(stdout=new)
        if "fetch" in cmd:
            return _cp()
        if "merge-base" in cmd:
            return _cp(returncode=1)  # not an ancestor
        if "merge" in cmd and "--ff-only" in cmd:
            merge_calls.append(cmd)
            return _cp()
        return _cp()

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "diverged"
    assert outcome.head_before == old
    assert outcome.head_after == new
    # Divergence must NOT trigger a merge attempt.
    assert merge_calls == []


def test_git_fetch_and_compare_rate_limited(tmp_path):
    """Fetch stderr contains '429' → rate_limited status."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:5] == ["rev-parse", "HEAD"]:
            return _cp(stdout="a" * 40)
        if "fetch" in cmd:
            return _cp(returncode=128, stderr="fatal: unable to access: error: 429 Too Many Requests")
        return _cp()

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path)
    assert outcome.status == "rate_limited"
    assert "429" in (outcome.error or "")


def test_git_fetch_and_compare_fetch_timeout(tmp_path):
    """subprocess.TimeoutExpired → failed with timeout message."""
    def fake_run(cmd, **kwargs):
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:5] == ["rev-parse", "HEAD"]:
            return _cp(stdout="a" * 40)
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30)
        return _cp()

    with patch("subprocess.run", side_effect=fake_run):
        outcome = git_fetch_and_compare(tmp_path, timeout=30)
    assert outcome.status == "failed"
    assert "timed out after 30s" in (outcome.error or "")


def test_git_fetch_and_compare_uses_git_dash_c(tmp_path):
    """All git invocations target the given dir via -C."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "--git-dir" in cmd_str:
            return _cp(stdout=".git")
        if "@{u}" in cmd_str:
            return _cp(stdout="origin/main")
        if cmd[3:5] == ["rev-parse", "HEAD"]:
            return _cp(stdout="a" * 40)
        if cmd[3:5] == ["rev-parse", "FETCH_HEAD"]:
            return _cp(stdout="a" * 40)
        if "fetch" in cmd:
            return _cp()
        return _cp()

    with patch("subprocess.run", side_effect=fake_run):
        git_fetch_and_compare(tmp_path)

    for cmd in calls:
        assert cmd[:3] == ["git", "-C", str(tmp_path)]


# ---------------------------------------------------------------------------
# is_transient_git_error / is_rate_limit_error
# ---------------------------------------------------------------------------

def test_is_transient_git_error_matches_timeout():
    assert is_transient_git_error("fetch timed out after 30s") is True


def test_is_transient_git_error_negative():
    assert is_transient_git_error("fatal: repository not found") is False


def test_is_rate_limit_error_429_503():
    assert is_rate_limit_error("fatal: error: 429 Too Many Requests") is True
    assert is_rate_limit_error("fatal: error: 503 Service Unavailable") is True


def test_is_rate_limit_error_negative():
    assert is_rate_limit_error("fatal: couldn't resolve host") is False


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
