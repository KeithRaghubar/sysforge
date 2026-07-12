"""
test_update_sentinels.py — pacman-hook sentinel consumption in cmd_update.

The libalpm hooks shipped under /usr/share/libalpm/hooks/ drop files at
/var/lib/sysforge/sentinels/{kernel,toolchain,buildstate} on relevant
package transactions. The first call to cmd_update reads them, surfaces
the kernel/toolchain reminders, and unlinks every sentinel.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sysforge import update as up
from sysforge.update import _consume_pacman_hook_sentinels


def _stub_logger():
    """Replace update._log with a MagicMock for the duration of the test.

    The real Logger uses __slots__, so per-attribute patching fails.
    Replace the whole logger instead.
    """
    return patch("sysforge.update._log", MagicMock())


def test_no_sentinel_dir_is_silent(tmp_path):
    """Older installs that predate the libalpm hooks have no sentinels dir;
    the consumer must skip silently rather than fatal."""
    missing = tmp_path / "missing"
    with patch("sysforge.update._SENTINEL_DIR", missing), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()
    mock_log.warn.assert_not_called()


def test_kernel_sentinel_warns_and_unlinks(tmp_path):
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    kernel = sentinels / "kernel"
    kernel.write_text("2026-05-07T18:00:00Z\nlinux\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()

    assert not kernel.exists()
    assert mock_log.warn.call_count == 1
    assert "Kernel" in mock_log.warn.call_args[0][0]


def test_toolchain_sentinel_warns_and_unlinks(tmp_path):
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    toolchain = sentinels / "toolchain"
    toolchain.write_text("2026-05-07T18:00:00Z\nllvm\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()

    assert not toolchain.exists()
    assert mock_log.warn.call_count == 1
    assert "Toolchain" in mock_log.warn.call_args[0][0]


def test_buildstate_sentinel_unlinked_silently(tmp_path):
    """The buildstate sentinel exists only to nudge the build_state.toml
    resync that already runs; consuming it must not produce user-facing
    output."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    buildstate = sentinels / "buildstate"
    buildstate.write_text("2026-05-07T18:00:00Z\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()

    assert not buildstate.exists()
    mock_log.warn.assert_not_called()


def test_all_sentinels_consumed_in_one_pass(tmp_path):
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    for name in ("kernel", "toolchain", "buildstate"):
        (sentinels / name).write_text("ts\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()

    for name in ("kernel", "toolchain", "buildstate"):
        assert not (sentinels / name).exists()
    # kernel + toolchain produce warns; buildstate is silent
    assert mock_log.warn.call_count == 2


def test_silent_consume_unlinks_without_warning(tmp_path):
    """Phase 5 / Phase 6.5 of cmd_update issue their own pacman transactions,
    which can drop fresh kernel/toolchain sentinels right before cmd_update
    returns. The end-of-update silent consume must unlink those without
    re-emitting warnings the Phase 5/6.5 output already covers."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    for name in ("kernel", "toolchain", "buildstate"):
        (sentinels / name).write_text("ts\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels(silent=True)

    for name in ("kernel", "toolchain", "buildstate"):
        assert not (sentinels / name).exists()
    mock_log.warn.assert_not_called()


def test_silent_default_is_false(tmp_path):
    """Guard against accidentally inverting the default: the bare call form
    used at the start of cmd_update must keep producing warnings."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    (sentinels / "toolchain").write_text("ts\n")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), _stub_logger() as mock_log:
        _consume_pacman_hook_sentinels()

    assert mock_log.warn.call_count == 1


# ---------------------------------------------------------------------------
# B1 — external-install reconcile must not demote packages absent from repos
# ---------------------------------------------------------------------------

def test_reconcile_skips_packages_absent_from_sync_repos(tmp_path):
    """B1: a source-built ``-git`` package sysforge installs via its own
    ``pacman -U`` lands in the buildstate sentinel; when the self-install
    sentinel is missing/incomplete it looks 'external'. Such a package has no
    repo counterpart, so it could not have been 'reinstalled from the repo' —
    the demotion must skip anything ``get_pacman_sync_version`` reports as not
    in a sync database."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    (sentinels / "buildstate").write_text(
        "2026-07-11T10:00:00Z\ncosmic-comp-git\nmesa\n\n")
    # No self-install file at all — the perms asymmetry on the real system.
    bs = MagicMock()
    bs.reconcile_external_installs.return_value = ["mesa"]

    def fake_sync_version(name):
        return "24.1-1" if name == "mesa" else None  # -git pkg not in repos

    with patch("sysforge.update._SENTINEL_DIR", sentinels), \
         patch("sysforge.primitives.pacman.get_pacman_sync_version",
               side_effect=fake_sync_version), _stub_logger():
        up._reconcile_external_demotions(bs)

    # Only the genuine repo package is offered to the build-state demotion.
    called = bs.reconcile_external_installs.call_args[0][0]
    assert called == {"mesa"}


# ---------------------------------------------------------------------------
# B2 — sysforge's own kernel/toolchain sentinel drops must be swallowed on
#      every exit path, not only the normal end-of-run.
# ---------------------------------------------------------------------------

def test_reminder_sentinels_swallowed_on_body_exception(tmp_path):
    """B2: if the update body raises after Phase 6.5 has dropped kernel/toolchain
    sentinels (sysforge's own ``pacman -Syu``), the finally must still swallow
    them so the next run doesn't surface sysforge's own change as an external
    'changed since last run' reminder."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()

    def boom(args):
        # Simulate Phase 6.5 dropping sysforge's own sentinels mid-run, then an
        # early failure before the normal end-of-run swallow.
        (sentinels / "kernel").write_text("ts\nlinux\n")
        (sentinels / "toolchain").write_text("ts\nllvm\n")
        raise RuntimeError("interrupted after Phase 6.5")

    with patch("sysforge.update._SENTINEL_DIR", sentinels), \
         patch("sysforge.update._cmd_update_body", side_effect=boom), \
         patch("sysforge.update._suppress_pagers_in_env"), _stub_logger():
        with pytest.raises(RuntimeError):
            up.cmd_update(MagicMock(interactive=False))

    assert not (sentinels / "kernel").exists()
    assert not (sentinels / "toolchain").exists()
