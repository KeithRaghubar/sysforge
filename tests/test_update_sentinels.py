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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
