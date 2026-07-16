"""
test_pacman.py — tests for pacman.py: collect_makedeps, filter_missing_deps,
get_installed_version, snapshot_pkg_dir.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sysforge.primitives.pacman import (
    cached_pkg_files_for,
    collect_builddeps,
    collect_makedeps,
    detect_orphan_artifacts,
    filter_missing_deps,
    filter_pkgs_to_installed,
    get_installed_version,
    get_all_installed_packages,
    get_foreign_packages,
    get_pacman_cache_dirs,
    get_pacman_sync_version,
    get_pkgbase,
    read_pkg_replaces_from_file,
    read_pkgname_from_file,
    snapshot_pkg_dir,
)


@pytest.fixture(autouse=True)
def _force_non_root(monkeypatch):
    monkeypatch.setattr("sysforge.primitives.privilege.os.geteuid", lambda: 1000)


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
# collect_builddeps — depends + makedepends + checkdepends (the full set
# makepkg requires present before a -s-stripped build)
# ---------------------------------------------------------------------------

class TestCollectBuilddeps:

    def _write(self, tmp_path, name, *, depends=(), makedepends=(), checkdepends=()):
        d = tmp_path / name
        d.mkdir()
        p = d / "PKGBUILD"

        def arr(key, vals):
            return f"{key}=({' '.join(repr(v) for v in vals)})\n" if vals else ""

        p.write_text(
            f"pkgname={name}\n"
            + arr("depends", depends)
            + arr("makedepends", makedepends)
            + arr("checkdepends", checkdepends)
        )
        return p

    def test_unions_all_three_arrays(self, tmp_path):
        p = self._write(
            tmp_path, "foo",
            depends=["pyside6", "glibc"],
            makedepends=["cmake"],
            checkdepends=["python-pytest"],
        )
        result = collect_builddeps([p])
        assert set(result) == {"pyside6", "glibc", "cmake", "python-pytest"}

    def test_includes_runtime_depends_unlike_makedeps(self, tmp_path):
        """The whole point of the pyside6 fix: a repo runtime dep is collected
        by collect_builddeps even though collect_makedeps never sees it."""
        p = self._write(tmp_path, "foo", depends=["pyside6"], makedepends=["cmake"])
        assert "pyside6" in collect_builddeps([p])
        assert "pyside6" not in collect_makedeps([p])

    def test_strips_versions_and_skips_unresolved(self, tmp_path):
        p = self._write(
            tmp_path, "foo",
            depends=["glibc>=2.0", "${_pydeps[@]/#/python-}"],
            makedepends=["cmake>=3.16"],
        )
        result = collect_builddeps([p])
        assert "glibc" in result and "cmake" in result
        assert not any(">=" in d for d in result)
        # The un-evaluated shell token is dropped, never handed to pacman -S.
        assert not any("$" in d or "{" in d for d in result)


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
# checkupdates_map
# ---------------------------------------------------------------------------

from sysforge.primitives.pacman import checkupdates_map as _checkupdates_map


class TestCheckupdatesMap:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_parses_arrow_format(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="linux 6.1.0-1 -> 6.2.0-1\nfirefox 130.0-1 -> 131.0-1\n",
            stderr="",
            returncode=0,
        )
        assert _checkupdates_map() == {
            "linux": "6.2.0-1",
            "firefox": "131.0-1",
        }

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_exit_2_means_no_updates(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="", returncode=2,
        )
        assert _checkupdates_map() == {}

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_missing_binary_returns_none(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert _checkupdates_map() is None

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_error_exit_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="db not writable", returncode=1,
        )
        assert _checkupdates_map() is None

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_ignores_malformed_lines(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="linux 6.1.0-1 -> 6.2.0-1\n# comment\nbroken-line\n",
            stderr="",
            returncode=0,
        )
        assert _checkupdates_map() == {"linux": "6.2.0-1"}


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
        pkg = "/tmp/foo-bar-git-1.0.0-1-x86_64.pkg.tar.zst"
        assert read_pkgname_from_file(pkg) == "foo-bar-git"

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
# read_pkg_replaces_from_file
# ---------------------------------------------------------------------------

class TestReadPkgReplacesFromFile:

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_reads_replaces(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="pkgname = mesa-sysforge\nreplaces = mesa\nreplaces = mesa-libgl\n",
            returncode=0,
        )
        assert read_pkg_replaces_from_file("/tmp/mesa-sysforge.pkg.tar.zst") == {
            "mesa", "mesa-libgl",
        }

    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_no_replaces_field(self, mock_run):
        mock_run.return_value = MagicMock(stdout="pkgname = foo\n", returncode=0)
        assert read_pkg_replaces_from_file("/tmp/x.pkg.tar.zst") == set()

    @patch("sysforge.primitives.pacman.subprocess.run", side_effect=FileNotFoundError)
    def test_bsdtar_not_installed(self, _mock_run):
        assert read_pkg_replaces_from_file("/tmp/x.pkg.tar.zst") == set()


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
        expected = {"/tmp/foo-1-1.pkg.tar.zst", "/tmp/foo-dev-1-1.pkg.tar.zst"}
        assert set(str(p) for p in keep) == expected
        assert [pn for _, pn in dropped] == ["foo-bar"]

    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file")
    @patch("sysforge.primitives.pacman.read_pkgname_from_file")
    def test_keeps_renamed_pkg_that_replaces_installed(self, mock_name, mock_repl):
        # A conflict-mode `-sysforge` rebuild (mesa --pgo=use) emits
        # `mesa-sysforge`, whose pkgname is on neither the installed nor the
        # requested list — but it `replaces = mesa`, which IS installed, so it
        # must be kept, not dropped as a stray split sub-package.
        mock_name.return_value = "mesa-sysforge"
        mock_repl.return_value = {"mesa"}
        keep, dropped = filter_pkgs_to_installed(
            ["/tmp/mesa-sysforge-1-1.pkg.tar.zst"], installed={"mesa"},
        )
        assert [str(p) for p in keep] == ["/tmp/mesa-sysforge-1-1.pkg.tar.zst"]
        assert dropped == []

    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file", return_value=set())
    @patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value=None)
    def test_unreadable_file_kept(self, _mock_read, _mock_repl):
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
# batch_install_pkgs interactivity (B6)
# ---------------------------------------------------------------------------

from sysforge.primitives.pacman import batch_install_pkgs


class TestBatchInstallPkgsInteractive:
    """``--interactive`` must thread a real TTY prompt through the final
    ``pacman -U`` so a package-conflict question is put to the operator
    instead of being auto-answered ``N`` by ``--noconfirm`` (B6)."""

    def _make_pkg(self, tmp_path):
        p = tmp_path / "foo-1-1-x86_64.pkg.tar.zst"
        p.write_bytes(b"")
        return p

    @staticmethod
    def _pacman_call(mock_run):
        # read_pkgname_from_file (post-install marker) runs its own bsdtar
        # subprocess; the pacman -U call is the first invocation.
        return mock_run.call_args_list[0]

    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file",
           return_value=set())
    @patch("sysforge.primitives.pacman.get_all_installed_packages",
           return_value={})
    @patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="foo")
    @patch("sysforge.primitives.install_reconcile.record_self_install")
    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_default_is_noconfirm(self, mock_run, _rec, _rd, _inst, _repl, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        assert batch_install_pkgs([self._make_pkg(tmp_path)]) is True
        call = self._pacman_call(mock_run)
        assert "--noconfirm" in call.args[0]

    @patch("sysforge.primitives.pacman.read_pkgname_from_file", return_value="foo")
    @patch("sysforge.primitives.install_reconcile.record_self_install")
    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_interactive_drops_noconfirm_and_inherits_tty(
        self, mock_run, _rec, _rd, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr=None)
        assert batch_install_pkgs(
            [self._make_pkg(tmp_path)], interactive=True
        ) is True
        call = self._pacman_call(mock_run)
        assert "--noconfirm" not in call.args[0]
        # The conflict prompt must reach the operator: streams stay inherited
        # (not captured), so the question is visible and stdin can answer it.
        assert call.kwargs.get("stderr") is None


class TestBatchInstallPkgsConflictRename:
    """A conflict-mode ``-sysforge`` rename is a deliberate drop-in replacement
    (built pkg declares ``replaces = <stock>`` for an installed pkg). pacman
    only auto-processes ``replaces`` on a sync upgrade, so on a ``-U
    --noconfirm`` install the conflict prompt is declined (default N) and the
    transaction aborts. When a built pkg replaces something installed, pass
    ``--ask=4`` (ALPM_QUESTION_CONFLICT_PKG) to auto-confirm that intended
    removal; otherwise leave the prompt at its safe default."""

    def _make_pkg(self, tmp_path, name="mesa-sysforge"):
        p = tmp_path / f"{name}-1-1-x86_64.pkg.tar.zst"
        p.write_bytes(b"")
        return p

    @patch("sysforge.primitives.pacman.get_all_installed_packages",
           return_value={"mesa": "1:26.1.3-2"})
    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file",
           return_value={"mesa"})
    @patch("sysforge.primitives.pacman.read_pkgname_from_file",
           return_value="mesa-sysforge")
    @patch("sysforge.primitives.install_reconcile.record_self_install")
    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_replaces_installed_auto_confirms_conflict(
        self, mock_run, _rec, _name, _repl, _inst, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        assert batch_install_pkgs([self._make_pkg(tmp_path)]) is True
        argv = mock_run.call_args_list[0].args[0]
        assert "--noconfirm" in argv
        assert "--ask=4" in argv

    @patch("sysforge.primitives.pacman.get_all_installed_packages",
           return_value={"some-other-pkg": "1-1"})
    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file",
           return_value=set())
    @patch("sysforge.primitives.pacman.read_pkgname_from_file",
           return_value="foo")
    @patch("sysforge.primitives.install_reconcile.record_self_install")
    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_no_replaces_leaves_conflict_prompt_at_default(
        self, mock_run, _rec, _name, _repl, _inst, tmp_path
    ):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        assert batch_install_pkgs([self._make_pkg(tmp_path, "foo")]) is True
        argv = mock_run.call_args_list[0].args[0]
        assert not any(str(a).startswith("--ask") for a in argv)

    @patch("sysforge.primitives.pacman.get_all_installed_packages")
    @patch("sysforge.primitives.pacman.read_pkg_replaces_from_file",
           return_value={"mesa"})
    @patch("sysforge.primitives.pacman.read_pkgname_from_file",
           return_value="mesa-sysforge")
    @patch("sysforge.primitives.install_reconcile.record_self_install")
    @patch("sysforge.primitives.pacman.subprocess.run")
    def test_interactive_never_adds_ask(
        self, mock_run, _rec, _name, _repl, mock_inst, tmp_path
    ):
        # Interactive runs let the operator answer the prompt — no --ask, and
        # the installed-set query is never needed.
        mock_run.return_value = MagicMock(returncode=0, stderr=None)
        assert batch_install_pkgs(
            [self._make_pkg(tmp_path)], interactive=True
        ) is True
        argv = mock_run.call_args_list[0].args[0]
        assert not any(str(a).startswith("--ask") for a in argv)
        mock_inst.assert_not_called()


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


# ---------------------------------------------------------------------------
# detect_orphan_artifacts (C2)
# ---------------------------------------------------------------------------

class TestDetectOrphanArtifacts:
    def _mk_pkg(self, pkgdest, name: str, ver: str, rel: str = "1",
                arch: str = "x86_64", epoch: str = "0"):
        if epoch and epoch != "0":
            stem = f"{name}-{epoch}:{ver}-{rel}-{arch}.pkg.tar.zst"
        else:
            stem = f"{name}-{ver}-{rel}-{arch}.pkg.tar.zst"
        path = pkgdest / stem
        path.write_bytes(b"")
        return path

    def test_returns_empty_when_pkgdest_missing(self, tmp_path):
        result = detect_orphan_artifacts(tmp_path / "nope", {})
        assert result == {"superseded": []}

    def test_uninstalled_pkgname_is_not_classified(self, tmp_path):
        """Per spec: --prune wouldn't safely delete builds for
        not-installed packages (could be a kept-on-purpose kernel build, etc.),
        so don't list them as orphans."""
        self._mk_pkg(tmp_path, "ghostpkg", "1.0")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value="ghostpkg"):
            result = detect_orphan_artifacts(tmp_path, {})
        assert result == {"superseded": []}

    def test_classifies_superseded_when_artifact_older_than_installed(self, tmp_path):
        self._mk_pkg(tmp_path, "htop", "3.3.0")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value="htop"):
            result = detect_orphan_artifacts(tmp_path, {"htop": "3.4.0-1"})
        assert len(result["superseded"]) == 1

    def test_keeps_current_artifacts(self, tmp_path):
        """Same-version artifacts are not orphans — they are the current build."""
        self._mk_pkg(tmp_path, "htop", "3.4.0")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value="htop"):
            result = detect_orphan_artifacts(tmp_path, {"htop": "3.4.0-1"})
        assert result == {"superseded": []}

    def test_keeps_newer_artifacts(self, tmp_path):
        """Newer-than-installed = build hasn't been installed yet, not orphan."""
        self._mk_pkg(tmp_path, "htop", "3.5.0")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value="htop"):
            result = detect_orphan_artifacts(tmp_path, {"htop": "3.4.0-1"})
        assert result == {"superseded": []}

    def test_ignores_signature_files(self, tmp_path):
        (tmp_path / "ghostpkg-1.0-1-x86_64.pkg.tar.zst.sig").write_bytes(b"")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   side_effect=AssertionError("must not scan .sig")):
            result = detect_orphan_artifacts(tmp_path, {})
        assert result == {"superseded": []}

    def test_unparseable_filename_is_skipped(self, tmp_path):
        """Filenames the parser can't decode pass through silently."""
        path = tmp_path / "weird-thing.pkg.tar.zst"
        path.write_bytes(b"")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value="otherpkg"):
            result = detect_orphan_artifacts(tmp_path, {"otherpkg": "1.0-1"})
        assert result == {"superseded": []}

    def test_pkginfo_unreadable_passes_through(self, tmp_path):
        """A package whose .PKGINFO can't be read isn't flagged."""
        self._mk_pkg(tmp_path, "broken", "1.0")
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   return_value=None):
            result = detect_orphan_artifacts(tmp_path, {"broken": "1.0-1"})
        assert result == {"superseded": []}

    def test_mixed_sweep_only_returns_superseded(self, tmp_path):
        """A mixed PKGDEST: untracked (kept-on-purpose) is filtered out,
        only the unambiguously-stale superseded files surface."""
        names = {
            (tmp_path / "kernel-custom-6.10.0-1-x86_64.pkg.tar.zst"): "kernel-custom",
            (tmp_path / "htop-3.3.0-1-x86_64.pkg.tar.zst"): "htop",
            (tmp_path / "htop-3.4.0-1-x86_64.pkg.tar.zst"): "htop",
        }
        for p in names:
            p.write_bytes(b"")

        def fake_read(path):
            return names[path]

        # kernel-custom is NOT in installed (the kept-on-purpose case).
        with patch("sysforge.primitives.pacman.read_pkgname_from_file",
                   side_effect=fake_read):
            result = detect_orphan_artifacts(tmp_path, {"htop": "3.4.0-1"})

        assert {p.name for p in result["superseded"]} == {
            "htop-3.3.0-1-x86_64.pkg.tar.zst"
        }
        # The kernel build is not in the result at all.
        assert all("kernel-custom" not in p.name for p in result["superseded"])


# ---------------------------------------------------------------------------
# get_pacman_cache_dirs / cached_pkg_files_for (offline rollback source)
# ---------------------------------------------------------------------------

class TestPacmanCache:
    def test_cache_dirs_default_when_no_conf(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sysforge.primitives.pacman._PACMAN_CONF",
                            tmp_path / "absent.conf")
        assert get_pacman_cache_dirs() == [Path("/var/cache/pacman/pkg")]

    def test_cache_dirs_parsed_from_options(self, tmp_path, monkeypatch):
        conf = tmp_path / "pacman.conf"
        conf.write_text(
            "[options]\n"
            "CacheDir = /custom/cache /second/cache\n"
            "[core]\n"
            "CacheDir = /should/not/count\n"  # outside [options]
        )
        monkeypatch.setattr("sysforge.primitives.pacman._PACMAN_CONF", conf)
        dirs = get_pacman_cache_dirs()
        assert dirs == [Path("/custom/cache"), Path("/second/cache")]

    def test_cached_pkg_files_for_resolves_exact_version(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        # Two prefix-colliding names: llvm vs llvm-libs.
        (cache / "llvm-22.1.5-1-x86_64.pkg.tar.zst").touch()
        (cache / "llvm-libs-22.1.5-1-x86_64.pkg.tar.zst").touch()

        monkeypatch.setattr("sysforge.primitives.pacman.get_pacman_cache_dirs",
                            lambda: [cache])
        versions = {"llvm": "22.1.5-1", "llvm-libs": "22.1.5-1"}
        monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                            lambda n: versions.get(n))

        result = cached_pkg_files_for(["llvm", "llvm-libs"])
        assert result["llvm"].name == "llvm-22.1.5-1-x86_64.pkg.tar.zst"
        assert result["llvm-libs"].name == "llvm-libs-22.1.5-1-x86_64.pkg.tar.zst"

    def test_cached_pkg_files_for_missing_cache_is_none(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        # Installed but archive absent from the cache (e.g. paccache cleaned it).
        monkeypatch.setattr("sysforge.primitives.pacman.get_pacman_cache_dirs",
                            lambda: [cache])
        monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                            lambda n: "22.1.5-1")
        result = cached_pkg_files_for(["clang"])
        assert result["clang"] is None

    def test_cached_pkg_files_for_not_installed_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sysforge.primitives.pacman.get_pacman_cache_dirs",
                            lambda: [tmp_path])
        monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                            lambda n: None)
        result = cached_pkg_files_for(["openmp"])
        assert result["openmp"] is None

    def test_cached_pkg_files_for_skips_wrong_version(self, tmp_path, monkeypatch):
        cache = tmp_path / "cache"
        cache.mkdir()
        # Only an older version is cached; the installed version isn't there.
        (cache / "lld-22.1.4-1-x86_64.pkg.tar.zst").touch()
        monkeypatch.setattr("sysforge.primitives.pacman.get_pacman_cache_dirs",
                            lambda: [cache])
        monkeypatch.setattr("sysforge.primitives.pacman.get_installed_version",
                            lambda n: "22.1.5-1")
        result = cached_pkg_files_for(["lld"])
        assert result["lld"] is None
