"""
test_state_orphans.py — coverage for `sysforge state orphans`.

Verifies that the command surfaces untracked + superseded artifacts in
PKGDEST, prints a sane summary, and only deletes when --prune is set
(with confirmation by default).
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sysforge.state_cmd import cmd_state_orphans


def _args(prune: bool = False, no_confirm: bool = False):
    return SimpleNamespace(prune=prune, no_confirm=no_confirm)


def _mk_pkg(pkgdest: Path, name: str, ver: str, rel: str = "1") -> Path:
    pkgdest.mkdir(parents=True, exist_ok=True)
    path = pkgdest / f"{name}-{ver}-{rel}-x86_64.pkg.tar.zst"
    path.write_bytes(b"x" * 1024)
    return path


def test_pkgdest_unset_prints_message(capsys):
    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=None), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={}):
        cmd_state_orphans(_args())
    out = capsys.readouterr().out
    assert "PKGDEST not set" in out


def test_pkgdest_missing_prints_message(tmp_path, capsys):
    with patch("sysforge.primitives.pacman.get_pkgdest",
               return_value=tmp_path / "nope"), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={}):
        cmd_state_orphans(_args())
    out = capsys.readouterr().out
    assert "does not exist" in out


def test_no_orphans_message(tmp_path, capsys):
    pkgdest = tmp_path / "builds"
    pkgdest.mkdir()
    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={}):
        cmd_state_orphans(_args())
    out = capsys.readouterr().out
    assert "No orphans" in out


def test_lists_untracked_and_superseded_without_prune(tmp_path, capsys):
    pkgdest = tmp_path / "builds"
    _mk_pkg(pkgdest, "ghost", "1.0")     # untracked
    _mk_pkg(pkgdest, "htop", "3.3.0")    # superseded
    _mk_pkg(pkgdest, "htop", "3.4.0")    # current

    name_map = {
        f"ghost-1.0-1-x86_64.pkg.tar.zst": "ghost",
        f"htop-3.3.0-1-x86_64.pkg.tar.zst": "htop",
        f"htop-3.4.0-1-x86_64.pkg.tar.zst": "htop",
    }
    def fake_read(path):
        return name_map.get(Path(path).name)

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={"htop": "3.4.0-1"}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               side_effect=fake_read):
        cmd_state_orphans(_args())

    out = capsys.readouterr().out
    assert "Untracked" in out
    assert "ghost-1.0-1-x86_64.pkg.tar.zst" in out
    assert "Superseded" in out
    assert "htop-3.3.0-1-x86_64.pkg.tar.zst" in out
    # The current build is not surfaced
    assert "htop-3.4.0-1-x86_64.pkg.tar.zst" not in out
    # Without --prune the user is told how to delete
    assert "--prune" in out


def test_prune_with_no_confirm_deletes_orphans(tmp_path, capsys):
    pkgdest = tmp_path / "builds"
    ghost = _mk_pkg(pkgdest, "ghost", "1.0")
    stale = _mk_pkg(pkgdest, "htop", "3.3.0")
    current = _mk_pkg(pkgdest, "htop", "3.4.0")

    name_map = {
        ghost.name: "ghost",
        stale.name: "htop",
        current.name: "htop",
    }
    def fake_read(path):
        return name_map.get(Path(path).name)

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={"htop": "3.4.0-1"}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               side_effect=fake_read):
        cmd_state_orphans(_args(prune=True, no_confirm=True))

    out = capsys.readouterr().out
    assert not ghost.exists()
    assert not stale.exists()
    assert current.exists()
    assert "Removed 2 file(s)" in out


def test_prune_declined_keeps_files(tmp_path, capsys, monkeypatch):
    pkgdest = tmp_path / "builds"
    ghost = _mk_pkg(pkgdest, "ghost", "1.0")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               return_value="ghost"):
        cmd_state_orphans(_args(prune=True, no_confirm=False))

    out = capsys.readouterr().out
    assert ghost.exists()
    assert "Aborted" in out


def test_prune_eof_treated_as_decline(tmp_path, capsys, monkeypatch):
    pkgdest = tmp_path / "builds"
    ghost = _mk_pkg(pkgdest, "ghost", "1.0")

    def _eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               return_value="ghost"):
        cmd_state_orphans(_args(prune=True, no_confirm=False))

    out = capsys.readouterr().out
    assert ghost.exists()
    assert "Aborted" in out
