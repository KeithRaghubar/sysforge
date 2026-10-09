"""
test_local_repo.py — the sandbox's local pacman repo (3.1.0-F10).

Covers:
  1. sync() reconciles [sysforge-local] to the installed source-built set:
     installed version (never the newest artifact), replace + remove converge,
     no tool run when nothing changed, pruned artifact warns, tool failure
     refuses, .sig never published, once per session.
  2. the chroot pacman.conf section: first among repos, idempotent, rewritten
     on a state-dir move, empty fallback db created, refusal on write failure.
  3. the 2026-09-11 regression: identical pkgver resolves from sysforge-local.
"""
import os
import shutil
import subprocess  # noqa: TID251 — the regression test drives real pacman/makepkg
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives import build_sandbox as bs
from sysforge.primitives import local_repo as lr


@pytest.fixture(autouse=True)
def _reset():
    lr.reset_session()
    yield
    lr.reset_session()


class FakeRepoTools:
    """Stands in for repo-add/repo-remove: keeps the db as a real tar of
    <name>-<ver>/desc entries, so sync's db reader is exercised for real."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        tool, db, *rest = [a for a in argv if not a.startswith("-")]
        entries = lr._read_db(Path(db))
        if tool.endswith("repo-add"):
            for f in rest:
                name, ver = lr._split_pkgfile(Path(f).name)
                old = entries.get(name)
                if old and "-R" in argv:
                    (Path(db).parent / old[1]).unlink(missing_ok=True)
                entries[name] = (ver, Path(f).name)
        else:
            for name in rest:
                entries.pop(name, None)
        with tarfile.open(db, "w:gz") as t:
            for name, (ver, fname) in entries.items():
                data = f"%FILENAME%\n{fname}\n\n%NAME%\n{name}\n\n%VERSION%\n{ver}\n".encode()
                info = tarfile.TarInfo(f"{name}-{ver}/desc")
                info.size = len(data)
                import io
                t.addfile(info, io.BytesIO(data))


def _pkg(pkgdest: Path, name: str, ver: str) -> Path:
    pkgdest.mkdir(parents=True, exist_ok=True)
    p = pkgdest / f"{name}-{ver}-x86_64.pkg.tar.zst"
    p.write_bytes(f"{name}{ver}".encode())
    return p


def _sync(tmp_path, installed: dict, tools=None):
    tools = tools or FakeRepoTools()
    with patch.object(bs, "source_built_packages", return_value=set(installed)), \
            patch("sysforge.primitives.local_repo.pacman.get_installed_version",
                  side_effect=lambda n: installed.get(n)), \
            patch("sysforge.primitives.local_repo.run.run_or_raise", side_effect=tools):
        return lr.sync(tmp_path / "state", tmp_path / "pkgdest"), tools


def test_publishes_the_installed_version_not_the_newest(tmp_path):
    """Review Focus 4: PKGDEST is an archive of every build."""
    _pkg(tmp_path / "pkgdest", "llvm-libs", "22.1.8-2")
    _pkg(tmp_path / "pkgdest", "llvm-libs", "22.1.9-1")
    res, _ = _sync(tmp_path, {"llvm-libs": "22.1.8-2"})
    assert res.added == ["llvm-libs"]
    assert lr._read_db(lr.db_path(tmp_path / "state"))["llvm-libs"][0] == "22.1.8-2"


def test_replace_and_remove_converge_on_the_installed_set(tmp_path):
    _pkg(tmp_path / "pkgdest", "a", "1-1")
    _pkg(tmp_path / "pkgdest", "b", "1-1")
    _sync(tmp_path, {"a": "1-1", "b": "1-1"})
    lr.reset_session()
    _pkg(tmp_path / "pkgdest", "a", "2-1")
    res, tools = _sync(tmp_path, {"a": "2-1"})
    db = lr._read_db(lr.db_path(tmp_path / "state"))
    assert db == {"a": ("2-1", "a-2-1-x86_64.pkg.tar.zst")}
    assert res.added == ["a"] and res.removed == ["b"]
    assert not (lr.repo_dir(tmp_path / "state") / "b-1-1-x86_64.pkg.tar.zst").exists()
    # The superseded version's file goes too (deleted by sync, not repo-add -R,
    # which would also delete a same-version rebuild's new file).
    assert not (lr.repo_dir(tmp_path / "state") / "a-1-1-x86_64.pkg.tar.zst").exists()


def test_unchanged_set_runs_no_tool(tmp_path):
    _pkg(tmp_path / "pkgdest", "a", "1-1")
    _sync(tmp_path, {"a": "1-1"})
    lr.reset_session()
    res, tools = _sync(tmp_path, {"a": "1-1"})
    assert tools.calls == [] and res.added == res.removed == []


def test_pruned_installed_artifact_warns_and_continues(tmp_path, caplog):
    res, _ = _sync(tmp_path, {"gone": "1-1"})
    assert res.missing == ["gone"]


def test_repo_add_failure_refuses(tmp_path):
    _pkg(tmp_path / "pkgdest", "a", "1-1")
    with pytest.raises(bs.SandboxUnavailable, match="repo-add"):
        _sync(tmp_path, {"a": "1-1"}, tools=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom")))


def test_signatures_are_not_published(tmp_path):
    p = _pkg(tmp_path / "pkgdest", "a", "1-1")
    Path(str(p) + ".sig").write_bytes(b"sig")
    _sync(tmp_path, {"a": "1-1"})
    assert not list(lr.repo_dir(tmp_path / "state").glob("*.sig"))


def test_runs_once_per_session(tmp_path):
    _pkg(tmp_path / "pkgdest", "a", "1-1")
    _sync(tmp_path, {"a": "1-1"})
    res, tools = _sync(tmp_path, {"a": "1-1", "b": "1-1"})
    assert tools.calls == [] and res.added == []


def test_repo_is_outside_any_pkgbuild_dir(tmp_path):
    assert lr.repo_dir(tmp_path / "state") == tmp_path / "state" / "local-repo"


# ---------------------------------------------------------------------------
# 2. The chroot pacman.conf section
# ---------------------------------------------------------------------------

_STOCK_CONF = "[options]\nArchitecture = auto\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n\n" \
              "[extra]\nInclude = /etc/pacman.d/mirrorlist\n"


def _root_conf(tmp_path, text=_STOCK_CONF):
    root = tmp_path / "chroot" / "root"
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "pacman.conf").write_text(text)
    return bs.SandboxPolicy(enabled=True, chroot_dir=tmp_path / "chroot", local_repo=True)


def _ensure(pol, state):
    """ensure_chroot_section, counting conf rewrites (replace_file calls)."""
    real = lr.replace_file
    with patch("sysforge.primitives.local_repo.replace_file", side_effect=real) as rf:
        lr.ensure_chroot_section(pol, state)
    return rf


def test_section_is_inserted_before_the_first_repo(tmp_path):
    pol = _root_conf(tmp_path)
    _ensure(pol, tmp_path / "state")
    text = (pol.chroot_dir / "root/etc/pacman.conf").read_text()
    assert text.index("[sysforge-local]") < text.index("[core]")
    assert text.index("[options]") < text.index("[sysforge-local]")
    assert f"Server = file://{lr.repo_dir(tmp_path / 'state')}" in text
    assert "SigLevel = Never" in text


def test_section_is_idempotent(tmp_path):
    pol = _root_conf(tmp_path)
    _ensure(pol, tmp_path / "state")
    rp = _ensure(pol, tmp_path / "state")
    text = (pol.chroot_dir / "root/etc/pacman.conf").read_text()
    assert text.count("[sysforge-local]") == 1
    assert rp.call_count == 0  # nothing to write the second time


def test_section_rewritten_when_the_state_dir_moves(tmp_path):
    pol = _root_conf(tmp_path)
    _ensure(pol, tmp_path / "old")
    _ensure(pol, tmp_path / "new")
    text = (pol.chroot_dir / "root/etc/pacman.conf").read_text()
    assert str(tmp_path / "new") in text and str(tmp_path / "old") not in text
    assert text.count("[sysforge-local]") == 1


def test_conf_rewrite_keeps_the_existing_mode(tmp_path):
    """The root's pacman.conf is replaced through atomic_write.replace_file
    (mode/owner kept), not reinstalled at a fixed mode."""
    pol = _root_conf(tmp_path)
    conf = pol.chroot_dir / "root/etc/pacman.conf"
    conf.chmod(0o640)
    _ensure(pol, tmp_path / "state")
    assert conf.stat().st_mode & 0o777 == 0o640


def test_conf_replace_failure_refuses(tmp_path):
    pol = _root_conf(tmp_path)
    with patch("sysforge.primitives.local_repo.replace_file",
               side_effect=PermissionError("read-only")):
        with pytest.raises(bs.SandboxUnavailable, match="pacman.conf"):
            lr.ensure_chroot_section(pol, tmp_path / "state")


# ---------------------------------------------------------------------------
# 3. The 2026-09-11 regression
# ---------------------------------------------------------------------------

_real_tools = pytest.mark.skipif(
    not (shutil.which("pacman") and shutil.which("repo-add") and shutil.which("makepkg")),
    reason="needs pacman, repo-add and makepkg")


@_real_tools
def test_identical_pkgver_resolves_from_sysforge_local(tmp_path):
    """The 2026-09-11 break: the same llvm-libs pkgver in [sysforge-local] and
    [extra] must resolve to [sysforge-local] because it is listed first."""
    dbdir = tmp_path / "db" / "sync"
    dbdir.mkdir(parents=True)
    for repo, desc in (("sysforge-local", "host build"), ("extra", "repo build")):
        d = tmp_path / repo
        d.mkdir()
        (d / "PKGBUILD").write_text(
            f"pkgname=llvm-libs\npkgver=22.1.8\npkgrel=2\npkgdesc='{desc}'\narch=(any)\n"
            "package() { :; }\n")
        env = {k: v for k, v in os.environ.items() if k not in ("CC", "CXX", "MAKEPKG_CONF")}
        env.update(PKGDEST=str(d), BUILDDIR=str(d / "b"), SRCDEST=str(d / "s"))
        subprocess.run(["makepkg", "-d", "--nocheck", "-f"], cwd=d, check=True,
                       capture_output=True, env=env)
        pkg = next(d.glob("llvm-libs-*.pkg.tar*"))
        subprocess.run(["repo-add", "-q", str(d / f"{repo}.db.tar.gz"), str(pkg)],
                       check=True, capture_output=True)
        shutil.copy(d / f"{repo}.db.tar.gz", dbdir / f"{repo}.db")
    section = lr.section_text(tmp_path / "unused").replace(
        f"file://{lr.repo_dir(tmp_path / 'unused')}", f"file://{tmp_path / 'sysforge-local'}")
    conf = tmp_path / "pacman.conf"
    conf.write_text(
        "[options]\nArchitecture = auto\n\n" + section
        + f"\n[extra]\nSigLevel = Never\nServer = file://{tmp_path / 'extra'}\n")
    out = subprocess.run(
        ["pacman", "--config", str(conf), "--dbpath", str(tmp_path / "db"),
         "-Sp", "--print-format", "%r %v", "llvm-libs"],
        check=True, capture_output=True, text=True).stdout.strip()
    assert out == "sysforge-local 22.1.8-2"


# ---------------------------------------------------------------------------
# Final-review fixes
# ---------------------------------------------------------------------------

def test_same_version_rebuild_republishes_the_new_build(tmp_path):
    """Review #2: a reconverge/PGO rebuild keeps name-ver-rel-arch, and makepkg
    writes a new inode. The repo must serve the new bytes, not the old link."""
    p = _pkg(tmp_path / "pkgdest", "llvm-libs", "22.1.8-2")
    _sync(tmp_path, {"llvm-libs": "22.1.8-2"})
    lr.reset_session()
    p.unlink()
    p.write_bytes(b"rebuilt with new flags")
    res, tools = _sync(tmp_path, {"llvm-libs": "22.1.8-2"})
    published = lr.repo_dir(tmp_path / "state") / p.name
    assert published.read_bytes() == b"rebuilt with new flags"
    assert res.added == ["llvm-libs"]
    assert not any("-R" in c for c in tools.calls)  # -R would delete the new file


def test_pruned_installed_artifact_keeps_its_published_copy(tmp_path):
    """Review #3: the repo's hard link may be the last copy of the build the
    host runs; pruning PKGDEST must not make sync delete it."""
    p = _pkg(tmp_path / "pkgdest", "a", "1-1")
    _sync(tmp_path, {"a": "1-1"})
    lr.reset_session()
    p.unlink()
    res, _ = _sync(tmp_path, {"a": "1-1"})
    assert res.removed == [] and res.missing == []
    assert lr._read_db(lr.db_path(tmp_path / "state"))["a"][0] == "1-1"
    assert (lr.repo_dir(tmp_path / "state") / p.name).exists()


def test_section_names_only_the_host_repo(tmp_path):
    """Review #1: arch-nspawn bind-mounts every file:// Server from the HOST,
    so an in-root fallback path would make every container fail to start."""
    block = lr.section_text(tmp_path / "state")
    servers = [line for line in block.splitlines() if line.startswith("Server")]
    assert servers == [f"Server = file://{lr.repo_dir(tmp_path / 'state')}"]


def test_ensure_seeds_a_valid_repo_on_the_host_before_listing_it(tmp_path):
    """The bind source must exist before any arch-nspawn reads the block."""
    pol = _root_conf(tmp_path)
    state = tmp_path / "state"
    _ensure(pol, state)
    assert lr._read_db(lr.db_path(state)) == {}
    assert (lr.repo_dir(state) / f"{lr.REPO_NAME}.db").exists()  # what pacman fetches


def test_ensure_keeps_an_existing_repo(tmp_path):
    pol = _root_conf(tmp_path)
    _pkg(tmp_path / "pkgdest", "a", "1-1")
    _sync(tmp_path, {"a": "1-1"})
    _ensure(pol, tmp_path / "state")
    assert "a" in lr._read_db(lr.db_path(tmp_path / "state"))


def test_remove_section_drops_the_block(tmp_path):
    """Review #1a: a block left behind keeps the (stale) repo mounted."""
    pol = _root_conf(tmp_path)
    _ensure(pol, tmp_path / "state")
    lr.remove_chroot_section(pol)
    assert (pol.chroot_dir / "root/etc/pacman.conf").read_text() == _STOCK_CONF


def test_remove_section_without_a_block_writes_nothing(tmp_path):
    pol = _root_conf(tmp_path)
    with patch("sysforge.primitives.local_repo.replace_file") as rf:
        lr.remove_chroot_section(pol)
    assert not rf.called


def test_remove_section_failure_refuses(tmp_path):
    pol = _root_conf(tmp_path)
    _ensure(pol, tmp_path / "state")
    with patch("sysforge.primitives.local_repo.replace_file",
               side_effect=PermissionError("ro")):
        with pytest.raises(bs.SandboxUnavailable, match="pacman.conf"):
            lr.remove_chroot_section(pol)
