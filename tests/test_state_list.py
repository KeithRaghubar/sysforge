"""
test_state_list.py — coverage for `sysforge state list`'s untracked-foreign
section (C3) and the basic tabular output.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sysforge.state_cmd import cmd_state_list


def _args(state_dir: Path):
    return SimpleNamespace(state_dir=str(state_dir))


def _seed_state(state_dir: Path, entries: dict[str, dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, fields in entries.items():
        lines.append(f'["{name}"]')
        for k, v in fields.items():
            lines.append(f'{k} = "{v}"')
        lines.append("")
    (state_dir / "build_state.toml").write_text("\n".join(lines))


def test_no_state_no_foreign(tmp_path, capsys):
    with patch("sysforge.primitives.pacman.get_foreign_packages", return_value={}):
        cmd_state_list(_args(tmp_path / "state"))
    out = capsys.readouterr().out
    assert "No build state recorded" in out


def test_no_state_lists_foreign_as_untracked(tmp_path, capsys):
    """Empty build_state but installed foreign packages → untracked section."""
    with patch("sysforge.primitives.pacman.get_foreign_packages",
               return_value={"yay": "12.4.2-1", "sysforge-git": "0.2.0-1"}):
        cmd_state_list(_args(tmp_path / "state"))
    out = capsys.readouterr().out
    assert "Untracked foreign packages (2)" in out
    assert "yay 12.4.2-1" in out
    assert "sysforge-git 0.2.0-1" in out


def test_tracked_foreign_excluded_from_untracked(tmp_path, capsys):
    """Foreign packages with a build_state entry are NOT in the untracked list."""
    state_dir = tmp_path / "state"
    _seed_state(state_dir, {
        "yay": {
            "pkgver": "12.4.2", "pkgrel": "1", "epoch": "0",
            "pkgbase": "yay", "build_mode": "profiled",
            "pkgbuild_dir": "/tmp/yay",
        },
    })
    with patch("sysforge.primitives.pacman.get_foreign_packages",
               return_value={"yay": "12.4.2-1", "sysforge-git": "0.2.0-1"}):
        cmd_state_list(_args(state_dir))
    out = capsys.readouterr().out
    # Tracked yay shows up in the main table.
    assert "yay" in out
    # sysforge-git remains in the untracked list.
    assert "Untracked foreign packages (1)" in out
    assert "sysforge-git 0.2.0-1" in out
    # yay is not double-listed under untracked.
    untracked_section = out.split("Untracked foreign packages")[1]
    assert "yay 12.4.2-1" not in untracked_section


def test_no_untracked_section_when_all_foreign_tracked(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _seed_state(state_dir, {
        "yay": {
            "pkgver": "12.4.2", "pkgrel": "1", "epoch": "0",
            "pkgbase": "yay", "build_mode": "profiled",
            "pkgbuild_dir": "/tmp/yay",
        },
    })
    with patch("sysforge.primitives.pacman.get_foreign_packages",
               return_value={"yay": "12.4.2-1"}):
        cmd_state_list(_args(state_dir))
    out = capsys.readouterr().out
    assert "Untracked foreign packages" not in out
