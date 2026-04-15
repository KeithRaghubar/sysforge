"""
test_pacman.py — tests for pacman.py: collect_makedeps, filter_missing_deps,
get_installed_version, snapshot_pkg_dir.
"""
from unittest.mock import patch, MagicMock

from sysforge.primitives.pacman import (
    collect_makedeps,
    filter_missing_deps,
    get_installed_version,
    get_all_installed_packages,
    get_foreign_packages,
    get_pacman_sync_version,
    snapshot_pkg_dir,
)


# ---------------------------------------------------------------------------
# collect_makedeps
# ---------------------------------------------------------------------------

class TestCollectMakedeps:

    def _write_pkgbuild(self, tmp_path, name, makedepends):
        d = tmp_path / name
        d.mkdir()
        p = d / "PKGBUILD"
        if isinstance(makedepends, list):
            deps_str = " ".join(f"'{d}'" for d in makedepends)
            p.write_text(f"pkgname={name}\nmakedepends=({deps_str})\n")
        else:
            p.write_text(f"pkgname={name}\nmakedepends=('{makedepends}')\n")
        return p

    def test_empty_input(self):
        assert collect_makedeps([]) == []

    def test_strips_version_constraints(self, tmp_path):
        p = self._write_pkgbuild(tmp_path, "foo", ["cmake>=3.16", "python>3.10", "git"])
        result = collect_makedeps([p])
        assert "cmake" in result
        assert "python" in result
        assert "git" in result
        # Version constraints stripped
        assert not any(">=" in d for d in result)

    def test_sorted_unique(self, tmp_path):
        p1 = self._write_pkgbuild(tmp_path, "a", ["cmake", "git"])
        p2 = self._write_pkgbuild(tmp_path, "b", ["cmake", "python"])
        result = collect_makedeps([p1, p2])
        assert result == sorted(set(result))
        assert result.count("cmake") == 1

    def test_bad_pkgbuild_skipped(self, tmp_path):
        bad = tmp_path / "bad" / "PKGBUILD"
        bad.parent.mkdir()
        bad.write_text("this is not valid\n")
        result = collect_makedeps([bad])
        assert result == []


# ---------------------------------------------------------------------------
# filter_missing_deps (mocked subprocess)
# ---------------------------------------------------------------------------

class TestFilterMissingDeps:

    def test_empty_list(self):
        assert filter_missing_deps([]) == []

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_all_satisfied(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        result = filter_missing_deps(["cmake", "git"])
        assert result == []

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_some_missing(self, mock_run):
        mock_run.return_value = MagicMock(stdout="cmake\n", returncode=127)
        result = filter_missing_deps(["cmake", "git"])
        assert result == ["cmake"]


# ---------------------------------------------------------------------------
# get_installed_version (mocked subprocess)
# ---------------------------------------------------------------------------

class TestGetInstalledVersion:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_installed(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="htop 3.3.0-1\n", returncode=0
        )
        assert get_installed_version("htop") == "3.3.0-1"

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_not_installed(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert get_installed_version("nonexistent") is None


# ---------------------------------------------------------------------------
# get_all_installed_packages (mocked subprocess)
# ---------------------------------------------------------------------------

class TestGetAllInstalledPackages:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="htop 3.3.0-1\nneovim 0.10.0-1\n", returncode=0
        )
        result = get_all_installed_packages()
        assert result == {"htop": "3.3.0-1", "neovim": "0.10.0-1"}

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_error_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert get_all_installed_packages() == {}


# ---------------------------------------------------------------------------
# get_foreign_packages (mocked subprocess)
# ---------------------------------------------------------------------------

class TestGetForeignPackages:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_parses_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="sysforge-git 0.2.0-1\nyay 12.4.2-1\n", returncode=0
        )
        result = get_foreign_packages()
        assert result == {"sysforge-git": "0.2.0-1", "yay": "12.4.2-1"}


# ---------------------------------------------------------------------------
# get_pacman_sync_version (mocked subprocess)
# ---------------------------------------------------------------------------

class TestGetPacmanSyncVersion:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_found(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Repository      : extra\nName            : htop\nVersion         : 3.3.0-1\n",
            returncode=0,
        )
        assert get_pacman_sync_version("htop") == "3.3.0-1"

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert get_pacman_sync_version("nonexistent") is None


# ---------------------------------------------------------------------------
# snapshot_pkg_dir
# ---------------------------------------------------------------------------

class TestSnapshotPkgDir:

    def test_nonexistent_dir(self, tmp_path):
        result = snapshot_pkg_dir(tmp_path / "nope")
        assert result == frozenset()

    def test_finds_packages(self, tmp_path):
        (tmp_path / "foo-1.0-1-x86_64.pkg.tar.zst").touch()
        (tmp_path / "bar-2.0-1-x86_64.pkg.tar.xz").touch()
        (tmp_path / "bar-2.0-1-x86_64.pkg.tar.zst.sig").touch()
        (tmp_path / "unrelated.txt").touch()
        result = snapshot_pkg_dir(tmp_path)
        names = {p.name for p in result}
        assert "foo-1.0-1-x86_64.pkg.tar.zst" in names
        assert "bar-2.0-1-x86_64.pkg.tar.xz" in names
        assert "bar-2.0-1-x86_64.pkg.tar.zst.sig" not in names
        assert "unrelated.txt" not in names

    def test_uncompressed_pkg_tar(self, tmp_path):
        (tmp_path / "foo-1.0-1-x86_64.pkg.tar").touch()
        result = snapshot_pkg_dir(tmp_path)
        assert len(result) == 1
