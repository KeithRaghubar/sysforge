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


def test_record_self_install_provisions_dir_via_fs_provision(tmp_path):
    """2.2.0-B5: the sentinel dir must be created through fs_provision (which
    self-heals group + setgid) rather than a bare mkdir, so the dir lands
    root:sysforge 2775 and both the root hooks and the unprivileged append share
    a group-writable tree. A bare mkdir left it user:user 0755 and the append
    failed EACCES."""
    from unittest.mock import patch

    d = tmp_path / "sentinels"
    with patch("sysforge.primitives.fs_provision.ensure_writable_dir") as mock_prov:
        mock_prov.side_effect = (
            lambda p, **kw: (Path(p).mkdir(parents=True, exist_ok=True), Path(p))[1])
        ir.record_self_install(["mesa"], sentinel_dir=d)

    mock_prov.assert_called_once()
    assert mock_prov.call_args.args[0] == d
    # Best-effort, unprivileged chokepoint — must never sudo-prompt.
    assert mock_prov.call_args.kwargs.get("allow_sudo") is False


def test_record_self_install_survives_fs_provision_failure(tmp_path):
    """If fs_provision can't provision (FsProvisionError), record_self_install
    still falls back to a plain mkdir + append — it is best-effort and never
    raises out of the pacman -U chokepoint."""
    from unittest.mock import patch

    from sysforge.primitives.fs_provision import FsProvisionError

    d = tmp_path / "sentinels"
    with patch(
        "sysforge.primitives.fs_provision.ensure_writable_dir",
        side_effect=FsProvisionError("no sudo"),
    ):
        ir.record_self_install(["mesa"], sentinel_dir=d)

    assert ir._read_targets(d / "self-install") == {"mesa"}


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


# --- 3.5.0-B64: ordered window, one shared consumer ---

def test_external_targets_counts_a_pacman_s_after_sysforge_s_own_install(tmp_path):
    """``build foo``, then ``pacman -S foo``: foo is in both sentinels, and a
    set difference kept it self-installed and rebuilt over the switch."""
    d = tmp_path / "sentinels"
    d.mkdir()
    (d / "buildstate").write_text(
        "2026-06-21T20:08:00Z\nfoo\n\n"   # sysforge's own pacman -U
        "2026-06-21T20:30:00Z\nfoo\n\n")  # the user's pacman -S
    (d / "self-install").write_text("2026-06-21T20:08:00Z\nfoo\n\n")
    assert ir.external_install_targets(d) == {"foo"}


def test_external_targets_keeps_a_reinstall_by_sysforge_after_pacman_s(tmp_path):
    d = tmp_path / "sentinels"
    d.mkdir()
    (d / "buildstate").write_text(
        "2026-06-21T20:08:00Z\nfoo\n\n2026-06-21T20:30:00Z\nfoo\n\n")
    (d / "self-install").write_text("2026-06-21T20:31:00Z\nfoo\n\n")
    assert ir.external_install_targets(d) == set()


def test_external_targets_reads_a_hook_stamp_one_second_ahead_as_sysforge_s(tmp_path):
    """The record lands after pacman returns, so its stamp is never earlier
    than the hook's for the same transaction."""
    d = tmp_path / "sentinels"
    d.mkdir()
    (d / "buildstate").write_text("2026-06-21T20:08:00Z\nfoo\n\n")
    (d / "self-install").write_text("2026-06-21T20:08:01Z\nfoo\n\n")
    assert ir.external_install_targets(d) == set()


class _FakeBuildState:
    def __init__(self):
        self.seen = None

    def reconcile_external_installs(self, names):
        self.seen = set(names)
        return sorted(names)

    def all_failures(self):
        return {}


def _external_window(tmp_path):
    d = tmp_path / "sentinels"
    d.mkdir()
    (d / "buildstate").write_text("2026-06-21T20:30:00Z\nmesa\nfoo-git\n\n")
    return d


def test_consume_external_installs_filters_to_sync_repo_packages_and_clears(tmp_path):
    from unittest.mock import patch

    d = _external_window(tmp_path)
    bs = _FakeBuildState()
    with patch("sysforge.primitives.pacman.get_pacman_sync_version",
               side_effect=lambda n: "1-1" if n == "mesa" else None):
        demoted = ir.consume_external_installs(bs, d)
    assert bs.seen == {"mesa"} and demoted.demoted == ["mesa"]
    assert not (d / "buildstate").exists()


def test_consume_external_installs_read_only_keeps_the_window(tmp_path):
    from unittest.mock import patch

    d = _external_window(tmp_path)
    with patch("sysforge.primitives.pacman.get_pacman_sync_version", return_value="1-1"):
        ir.consume_external_installs(_FakeBuildState(), d, read_only=True)
    assert (d / "buildstate").exists()


def test_consume_external_installs_clears_the_window_when_reconcile_raises(tmp_path):
    from unittest.mock import patch

    import pytest

    d = _external_window(tmp_path)
    bs = _FakeBuildState()
    bs.reconcile_external_installs = lambda names: (_ for _ in ()).throw(OSError("x"))
    with patch("sysforge.primitives.pacman.get_pacman_sync_version", return_value="1-1"), \
            pytest.raises(OSError):
        ir.consume_external_installs(bs, d)
    assert not (d / "buildstate").exists()


def test_record_installed_artifacts_records_each_artifact_s_pkgname(tmp_path):
    from unittest.mock import patch

    from sysforge.primitives import pacman

    d = tmp_path / "sentinels"
    names = {"a.pkg.tar.zst": "foo", "b.pkg.tar.zst": None}
    with patch.object(pacman, "read_pkgname_from_file",
                      side_effect=lambda p: names[Path(p).name]):
        pacman.record_installed_artifacts(
            [tmp_path / "a.pkg.tar.zst", tmp_path / "b.pkg.tar.zst"], sentinel_dir=d)
    assert ir._read_targets(d / "self-install") == {"foo"}
