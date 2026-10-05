"""`sysforge state boot-entries` (3.3.0-F5)."""
import argparse

from sysforge.primitives import boot_entries as be

HDR = "# sysforge-managed (template: 97-arch-custom.conf, role: propeller) — r\n"


def test_rows_for():
    ents = [
        be.parse_entry("99-arch.conf", "linux /vmlinuz-linux\n"),
        be.parse_entry("97-arch-custom.conf", "linux /vmlinuz-linux-sysforge\n"),
        be.parse_entry("sysforge-linux-sysforge-propeller.conf",
                       HDR + "linux /vmlinuz-linux-sysforge-propeller\n"),
        be.parse_entry("sysforge-gone.conf", HDR + "linux /vmlinuz-gone\n"),
    ]
    rows = be.rows_for(ents, {"linux", "linux-sysforge", "linux-sysforge-propeller",
                              "linux-rt-lts"})
    assert rows == [
        ("linux", "99-arch.conf", "you", "-"),
        ("linux-rt-lts", "-", "none", "-"),
        ("linux-sysforge", "97-arch-custom.conf", "you", "-"),
        ("linux-sysforge-propeller", "sysforge-linux-sysforge-propeller.conf", "sysforge",
         "97-arch-custom.conf"),
        ("(missing)", "sysforge-gone.conf", "sysforge", "97-arch-custom.conf"),
    ]


def test_cmd_prints_table_quiet_at_default(tmp_path, monkeypatch, capsys, quiet_at_default):
    from sysforge import state_cmd
    boot = tmp_path / "boot"
    (boot / "loader/entries").mkdir(parents=True)
    (boot / "loader/entries/99-arch.conf").write_text("linux /vmlinuz-linux\n")
    (boot / "vmlinuz-linux").write_bytes(b"x")
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    monkeypatch.setattr(state_cmd, "_boot_entries_mode", lambda: ("systemd-boot", True))
    quiet_at_default(lambda: state_cmd.cmd_state_boot_entries(
        argparse.Namespace(no_pager=True)))
    # The fixture drains capsys; rerun once to assert on stdout directly.
    assert state_cmd.cmd_state_boot_entries(argparse.Namespace(no_pager=True)) == 0
    out = capsys.readouterr().out
    assert "KERNEL" in out
    assert "99-arch.conf" in out


def test_cmd_notes_when_not_managed(monkeypatch, capsys):
    from sysforge import state_cmd
    monkeypatch.setattr(state_cmd, "_boot_entries_mode", lambda: ("grub", False))
    assert state_cmd.cmd_state_boot_entries(argparse.Namespace(no_pager=True)) == 0
    assert "grub" in capsys.readouterr().out


def test_cmd_unreadable_entries_dir(tmp_path, monkeypatch, capsys):
    from sysforge import state_cmd
    monkeypatch.setattr(be, "BOOT_DIR", tmp_path / "nope")
    monkeypatch.setattr(state_cmd, "_boot_entries_mode", lambda: ("systemd-boot", True))
    assert state_cmd.cmd_state_boot_entries(argparse.Namespace(no_pager=True)) == 1
    assert "cannot read" in capsys.readouterr().out
