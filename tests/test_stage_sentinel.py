"""
test_stage_sentinel.py — tests for the stage_in_progress.toml sentinel.

Covers mark_started/clear/get_active round-trips plus the CLI-entry
recovery helper check_and_recover_stale_sentinel().
"""
from unittest.mock import MagicMock, patch


from sysforge.primitives.stage_sentinel import (
    StageSentinel,
    check_and_recover_stale_sentinel,
)


def test_get_active_absent_returns_none(tmp_path):
    s = StageSentinel(tmp_path)
    assert s.get_active() is None


def test_mark_started_writes_record(tmp_path):
    s = StageSentinel(tmp_path)
    s.mark_started("toolchain", compiler="llvm", pgo=True, recovery_cmd="sudo pacman -S llvm")

    record = s.get_active()
    assert record is not None
    assert record["stage"] == "toolchain"
    assert record["compiler"] == "llvm"
    assert record["pgo"] is True
    assert record["recovery_cmd"] == "sudo pacman -S llvm"
    assert "started_at" in record


def test_mark_started_round_trips_through_new_instance(tmp_path):
    """The sentinel survives across StageSentinel instances (the actual use case)."""
    StageSentinel(tmp_path).mark_started(
        "kernel", recovery_cmd="sudo pacman -S linux",
    )
    record = StageSentinel(tmp_path).get_active()
    assert record["stage"] == "kernel"
    assert record["recovery_cmd"] == "sudo pacman -S linux"


def test_clear_removes_file(tmp_path):
    s = StageSentinel(tmp_path)
    s.mark_started("toolchain")
    assert s.get_active() is not None
    s.clear()
    assert s.get_active() is None
    assert not s.path.exists()


def test_clear_when_absent_is_noop(tmp_path):
    s = StageSentinel(tmp_path)
    s.clear()  # must not raise
    assert s.get_active() is None


def test_corrupt_toml_returns_none(tmp_path):
    """Malformed TOML must not crash callers — return None and let the
    next mark_started overwrite the file."""
    s = StageSentinel(tmp_path)
    s.path.parent.mkdir(parents=True, exist_ok=True)
    s.path.write_text("not valid toml :::: [[[\n")
    assert s.get_active() is None


# ---------------------------------------------------------------------------
# check_and_recover_stale_sentinel — CLI-entry recovery flow
# ---------------------------------------------------------------------------

def test_check_no_sentinel_returns_true(tmp_path):
    """No sentinel → proceed."""
    assert check_and_recover_stale_sentinel(tmp_path) is True


def test_check_stale_declined_returns_false(tmp_path):
    """User declines recovery → block."""
    StageSentinel(tmp_path).mark_started(
        "toolchain", recovery_cmd="sudo pacman -S llvm",
    )
    with patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="n"):
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is False
    # Sentinel left in place on decline so the next run hits it again
    assert StageSentinel(tmp_path).get_active() is not None


def test_check_stale_accepted_runs_recovery_and_clears(tmp_path):
    """User accepts recovery → run pacman, clear sentinel, proceed."""
    StageSentinel(tmp_path).mark_started(
        "toolchain", recovery_cmd="sudo pacman -S llvm llvm-libs",
    )
    fake_proc = MagicMock(returncode=0)
    with patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch("sysforge.primitives.stage_sentinel.subprocess.run",
               return_value=fake_proc) as run_mock:
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is True
    run_mock.assert_called_once_with(
        ["sudo", "pacman", "-S", "llvm", "llvm-libs"], check=False,
    )
    assert StageSentinel(tmp_path).get_active() is None


def test_check_stale_recovery_fails_leaves_sentinel(tmp_path):
    """Recovery command returns non-zero → block, leave sentinel."""
    StageSentinel(tmp_path).mark_started(
        "toolchain", recovery_cmd="sudo pacman -S llvm",
    )
    fake_proc = MagicMock(returncode=1)
    with patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch("sysforge.primitives.stage_sentinel.subprocess.run",
               return_value=fake_proc):
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is False
    assert StageSentinel(tmp_path).get_active() is not None


def test_check_stale_no_recovery_cmd_blocks(tmp_path):
    """A sentinel without recovery_cmd can't be auto-recovered."""
    StageSentinel(tmp_path).mark_started("toolchain")  # no recovery_cmd
    with patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"):
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is False
    assert StageSentinel(tmp_path).get_active() is not None
