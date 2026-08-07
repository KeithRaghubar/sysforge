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
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="n"):
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
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
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
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch("sysforge.primitives.stage_sentinel.subprocess.run",
               return_value=fake_proc):
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is False
    assert StageSentinel(tmp_path).get_active() is not None


def test_check_stale_no_recovery_cmd_clears_on_confirm(tmp_path):
    """Sentinel without recovery_cmd: 'y' clears the sentinel and proceeds."""
    StageSentinel(tmp_path).mark_started("update")  # no recovery_cmd
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice",
               return_value="y") as prompt_mock, \
         patch("sysforge.primitives.stage_sentinel.subprocess.run") as run_mock:
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is True
    # No recovery binary was invoked — only the sentinel was cleared.
    run_mock.assert_not_called()
    # Prompt fired with the clear-only wording, not the restore wording.
    assert prompt_mock.call_count == 1
    assert "Clear the sentinel" in prompt_mock.call_args.args[0]
    assert StageSentinel(tmp_path).get_active() is None


def test_check_stale_no_recovery_cmd_decline_keeps_sentinel(tmp_path):
    """Sentinel without recovery_cmd: 'n' leaves the sentinel in place."""
    StageSentinel(tmp_path).mark_started("update")  # no recovery_cmd
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="n"):
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is False
    assert StageSentinel(tmp_path).get_active() is not None


def test_serialize_skips_none_values(tmp_path):
    """None metadata must not land in the TOML as the literal string 'None'."""
    s = StageSentinel(tmp_path)
    s.mark_started("update", recovery_cmd=None, extra=None, compiler="gcc")
    raw = s.path.read_text()
    # No key should ever serialize to the bare-string "None".
    assert '= "None"' not in raw
    assert "recovery_cmd" not in raw
    assert "extra" not in raw
    # Real values still round-trip.
    record = s.get_active()
    assert record is not None
    assert record["compiler"] == "gcc"
    assert "recovery_cmd" not in record


def test_check_stale_non_tty_emits_loud_error_and_blocks(tmp_path, capsys):
    """
    Non-TTY context (script, background session, IDE wrapper): the prompt
    cannot fire. We must NOT silently auto-decline — instead emit explicit
    manual-recovery instructions so the user understands what happened.
    """
    StageSentinel(tmp_path).mark_started(
        "toolchain", recovery_cmd="sudo pacman -S llvm",
    )
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=False), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice") as prompt_mock, \
         patch("sysforge.primitives.stage_sentinel.subprocess.run") as run_mock:
        result = check_and_recover_stale_sentinel(tmp_path)

    assert result is False
    # Prompt never fires; recovery never runs.
    prompt_mock.assert_not_called()
    run_mock.assert_not_called()
    # Sentinel left in place; user must clear manually.
    assert StageSentinel(tmp_path).get_active() is not None
    err = capsys.readouterr().err
    assert "stdin is not a TTY" in err
    assert str(tmp_path / "stage_in_progress.toml") in err
    assert "sudo pacman -S llvm" in err


def test_check_stale_clear_silent_failure_is_reported(tmp_path, capsys):
    """
    Regression for the load-bearing bug: if `sentinel.clear()` "succeeds"
    but the file is still on disk (state-dir mismatch, chroot/namespace
    surprise, filesystem quirk), we must NOT print "Recovery completed;
    sentinel cleared." — the user keeps seeing the sentinel re-fire and
    has no idea why. Verify-after-clear catches it.
    """
    StageSentinel(tmp_path).mark_started(
        "toolchain", recovery_cmd="sudo pacman -S llvm",
    )
    fake_proc = MagicMock(returncode=0)
    # Simulate clear() being a no-op (file persists) — this is the
    # silent-failure failure mode in the wild.
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch("sysforge.primitives.stage_sentinel.subprocess.run", return_value=fake_proc), \
         patch.object(StageSentinel, "clear", lambda self: None):
        result = check_and_recover_stale_sentinel(tmp_path)

    assert result is False
    # The original sentinel file is still there.
    assert StageSentinel(tmp_path).get_active() is not None
    err = capsys.readouterr().err
    assert "sentinel file is still present" in err
    assert str(tmp_path / "stage_in_progress.toml") in err
