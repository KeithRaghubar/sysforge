"""
test_install_reconcile.py — pacman-hook sentinel parsing + external-install
discrimination for the source-built → pacman auto-demote feature.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sysforge.primitives.install_reconcile as ir


def test_parse_events_multiple_blocks():
    text = (
        "2026-06-21T20:07:00Z\nmesa\nllvm\n\n"
        "2026-06-21T20:09:00Z\nhtop\n\n"
    )
    events = ir._parse_events(text)
    assert events == [{"mesa", "llvm"}, {"htop"}]


def test_parse_events_empty():
    assert ir._parse_events("") == []


def test_external_targets_subtracts_self_install(tmp_path):
    d = tmp_path / "sentinels"
    d.mkdir(parents=True)
    # buildstate hook recorded both the user's pacman -S mesa AND sysforge's own
    # pacman -U llvm; self-install recorded only llvm.
    (d / "buildstate").write_text(
        "2026-06-21T20:07:00Z\nmesa\n\n"
        "2026-06-21T20:08:00Z\nllvm\n\n"
    )
    (d / "self-install").write_text("2026-06-21T20:08:00Z\nllvm\n\n")
    assert ir.external_install_targets(d) == {"mesa"}


def test_external_targets_empty_when_no_sentinels(tmp_path):
    assert ir.external_install_targets(tmp_path / "sentinels") == set()


def test_record_self_install_appends(tmp_path):
    d = tmp_path / "sentinels"
    ir.record_self_install(["mesa", "llvm"], sentinel_dir=d)
    ir.record_self_install(["htop"], sentinel_dir=d)
    assert ir._read_targets(d / "self-install") == {"mesa", "llvm", "htop"}


def test_record_self_install_is_group_writable(tmp_path):
    """2.1.0-B13: the self-install sentinel must be created group-writable so a
    later run under a different uid in the ``sysforge`` group (e.g. a plain
    ``sysforge update`` after a prior ``sudo sysforge update``) can append to it
    rather than failing EACCES. The setgid dir only grants group *ownership*;
    the group-*write* bit has to be set explicitly against the umask."""
    import stat

    d = tmp_path / "sentinels"
    ir.record_self_install(["mesa"], sentinel_dir=d)
    mode = (d / "self-install").stat().st_mode
    assert mode & stat.S_IWGRP, oct(mode)


def test_record_self_install_heals_non_group_writable_file(tmp_path):
    """2.1.0-B13: a pre-existing sentinel left non-group-writable by an earlier
    (pre-fix) run is healed to group-writable when the current process owns it,
    so it stops blocking cross-uid appends going forward."""
    import stat

    d = tmp_path / "sentinels"
    d.mkdir(parents=True)
    stale = d / "self-install"
    stale.write_text("2026-06-21T20:07:00Z\nold\n\n")
    stale.chmod(0o644)  # simulate a umask-644 file from before the fix
    ir.record_self_install(["mesa"], sentinel_dir=d)
    assert stale.stat().st_mode & stat.S_IWGRP


def test_record_self_install_ignores_empty(tmp_path):
    d = tmp_path / "sentinels"
    ir.record_self_install([], sentinel_dir=d)
    ir.record_self_install([None, ""], sentinel_dir=d)
    assert not (d / "self-install").exists()


def test_clear_sentinels(tmp_path):
    d = tmp_path / "sentinels"
    d.mkdir(parents=True)
    (d / "buildstate").write_text("x\n")
    (d / "self-install").write_text("x\n")
    ir.clear_reconcile_sentinels(d)
    assert not (d / "buildstate").exists()
    assert not (d / "self-install").exists()
