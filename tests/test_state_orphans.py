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


def _args(prune: bool = False, no_confirm: bool = False, no_pager: bool = True):
    return SimpleNamespace(prune=prune, no_confirm=no_confirm, no_pager=no_pager)


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
    assert "No superseded build artifacts" in out


def test_uninstalled_packages_not_listed(tmp_path, capsys):
    """Per Keith's spec: PKGDEST builds for not-installed packages must NOT
    appear (they could be kernel builds with local commits the user wants
    to keep around for later install)."""
    pkgdest = tmp_path / "builds"
    _mk_pkg(pkgdest, "kernel-custom", "6.10.0")  # not installed → kept on purpose
    _mk_pkg(pkgdest, "htop", "3.3.0")            # superseded
    _mk_pkg(pkgdest, "htop", "3.4.0")            # current

    name_map = {
        "kernel-custom-6.10.0-1-x86_64.pkg.tar.zst": "kernel-custom",
        "htop-3.3.0-1-x86_64.pkg.tar.zst": "htop",
        "htop-3.4.0-1-x86_64.pkg.tar.zst": "htop",
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
    assert "kernel-custom" not in out  # MUST NOT be surfaced
    assert "Superseded" in out
    assert "htop-3.3.0-1-x86_64.pkg.tar.zst" in out
    assert "htop-3.4.0-1-x86_64.pkg.tar.zst" not in out
    assert "--prune" in out


def test_prune_only_removes_superseded(tmp_path, capsys):
    """--prune must not touch the kept-on-purpose builds."""
    pkgdest = tmp_path / "builds"
    kept = _mk_pkg(pkgdest, "kernel-custom", "6.10.0")  # uninstalled — kept
    stale = _mk_pkg(pkgdest, "htop", "3.3.0")           # superseded
    current = _mk_pkg(pkgdest, "htop", "3.4.0")         # current

    name_map = {
        kept.name: "kernel-custom",
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
    assert kept.exists()             # NOT touched
    assert not stale.exists()        # removed
    assert current.exists()          # not touched (newer)
    assert "Removed 1 file(s)" in out


def test_prune_declined_keeps_files(tmp_path, capsys, monkeypatch):
    pkgdest = tmp_path / "builds"
    stale = _mk_pkg(pkgdest, "htop", "3.3.0")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={"htop": "3.4.0-1"}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               return_value="htop"):
        cmd_state_orphans(_args(prune=True, no_confirm=False))

    out = capsys.readouterr().out
    assert stale.exists()
    assert "Aborted" in out


def test_prune_eof_treated_as_decline(tmp_path, capsys, monkeypatch):
    pkgdest = tmp_path / "builds"
    stale = _mk_pkg(pkgdest, "htop", "3.3.0")

    def _eof(_prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)

    with patch("sysforge.primitives.pacman.get_pkgdest", return_value=pkgdest), \
         patch("sysforge.primitives.pacman.get_all_installed_packages",
               return_value={"htop": "3.4.0-1"}), \
         patch("sysforge.primitives.pacman.read_pkgname_from_file",
               return_value="htop"):
        cmd_state_orphans(_args(prune=True, no_confirm=False))

    out = capsys.readouterr().out
    assert stale.exists()
    assert "Aborted" in out
