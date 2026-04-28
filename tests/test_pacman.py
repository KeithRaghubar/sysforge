"""
test_pacman.py — tests for pacman.py: collect_makedeps, filter_missing_deps,
get_installed_version, snapshot_pkg_dir.
"""
from unittest.mock import patch, MagicMock

from sysforge.primitives.pacman import (
    collect_makedeps,
    filter_missing_deps,
    filter_pkgs_to_installed,
    get_installed_version,
    get_all_installed_packages,
    get_foreign_packages,
    get_pacman_sync_version,
    get_pkgbase,
    read_pkgname_from_file,
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


# ---------------------------------------------------------------------------
# read_pkgname_from_file
# ---------------------------------------------------------------------------

class TestReadPkgnameFromFile:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_reads_pkgname(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="pkgname = foo-bar-git\npkgver = 1.0.0\n",
            returncode=0,
        )
        assert read_pkgname_from_file("/tmp/foo-bar-git-1.0.0-1-x86_64.pkg.tar.zst") == "foo-bar-git"

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_missing_pkgname_field(self, mock_run):
        mock_run.return_value = MagicMock(stdout="pkgver = 1.0.0\n", returncode=0)
        assert read_pkgname_from_file("/tmp/x.pkg.tar.zst") is None

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_bsdtar_failure(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert read_pkgname_from_file("/tmp/x.pkg.tar.zst") is None

    @patch("sysforge.primitives.pacman.subprocess.run", side_effect=FileNotFoundError)
    def test_bsdtar_not_installed(self, _mock_run):
        assert read_pkgname_from_file("/tmp/x.pkg.tar.zst") is None


# ---------------------------------------------------------------------------
# filter_pkgs_to_installed
# ---------------------------------------------------------------------------

class TestFilterPkgsToInstalled:
    """Split-pkgbase rebuilds emit a .pkg.tar per pkgname; only install
    files for pkgnames already on the system."""

    @patch("sysforge.primitives.pacman.read_pkgname_from_file")
    def test_keeps_only_installed(self, mock_read):
        pkg_map = {
            "/tmp/foo-1-1.pkg.tar.zst": "foo",
            "/tmp/foo-bar-1-1.pkg.tar.zst": "foo-bar",
            "/tmp/foo-dev-1-1.pkg.tar.zst": "foo-dev",
        }
        mock_read.side_effect = lambda p: pkg_map[str(p)]
        keep, dropped = filter_pkgs_to_installed(list(pkg_map), installed={"foo", "foo-dev"})
        assert set(str(p) for p in keep) == {"/tmp/foo-1-1.pkg.tar.zst", "/tmp/foo-dev-1-1.pkg.tar.zst"}
        assert [pn for _, pn in dropped] == ["foo-bar"]

    @patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value=None)
    def test_unreadable_file_kept(self, _mock_read):
        # If we can't determine pkgname, fall through to pacman to surface the error.
        keep, dropped = filter_pkgs_to_installed(["/tmp/x.pkg.tar.zst"], installed=set())
        assert keep == ["/tmp/x.pkg.tar.zst"]
        assert dropped == []

    @patch("sysforge.primitives.pacman.read_pkgname_from_file")
    def test_empty_input(self, _mock_read):
        keep, dropped = filter_pkgs_to_installed([], installed={"foo"})
        assert keep == []
        assert dropped == []


# ---------------------------------------------------------------------------
# get_pkgbase
# ---------------------------------------------------------------------------

class TestGetPkgbase:

    def _make_db(self, tmp_path, pkgname, version, base=None, extra_fields=""):
        entry = tmp_path / f"{pkgname}-{version}"
        entry.mkdir()
        desc = f"%NAME%\n{pkgname}\n\n%VERSION%\n{version}\n"
        if base is not None:
            desc += f"\n%BASE%\n{base}\n"
        if extra_fields:
            desc += "\n" + extra_fields
        (entry / "desc").write_text(desc)
        return entry

    def test_split_package_returns_base(self, tmp_path):
        self._make_db(tmp_path, "linux-custom-headers", "6.19.12.arch1-1",
                      base="linux-custom")
        assert get_pkgbase("linux-custom-headers", root=tmp_path) == "linux-custom"

    def test_no_base_field_returns_none(self, tmp_path):
        self._make_db(tmp_path, "foo", "1.0-1", base=None)
        assert get_pkgbase("foo", root=tmp_path) is None

    def test_not_installed_returns_none(self, tmp_path):
        assert get_pkgbase("ghost", root=tmp_path) is None

    def test_missing_desc_returns_none(self, tmp_path):
        entry = tmp_path / "foo-1.0-1"
        entry.mkdir()
        assert get_pkgbase("foo", root=tmp_path) is None

    def test_base_equals_name_for_non_split(self, tmp_path):
        # Some packages record %BASE% even when pkgbase == pkgname.
        self._make_db(tmp_path, "vim", "9.1-1", base="vim")
        assert get_pkgbase("vim", root=tmp_path) == "vim"

    def test_only_first_value_after_base(self, tmp_path):
        # %BASE% is a single-value field; section ends at next blank or %TAG%.
        self._make_db(tmp_path, "linux-custom-headers", "6.19.12.arch1-1",
                      base="linux-custom",
                      extra_fields="%DESC%\nKernel headers\n")
        assert get_pkgbase("linux-custom-headers", root=tmp_path) == "linux-custom"
