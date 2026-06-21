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
