"""
test_stage_sentinel.py — tests for the stage_in_progress.toml sentinel.

Covers mark_started/clear/get_active round-trips plus the CLI-entry
recovery helper check_and_recover_stale_sentinel().
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sysforge.primitives import stage_sentinel as ss
from sysforge.primitives.resource_guard import lift_for_child
from sysforge.primitives.stage_sentinel import (
    Recovery,
    StageSentinel,
    check_and_recover_stale_sentinel,
)


def test_get_active_absent_returns_none(tmp_path):
    s = StageSentinel(tmp_path)
    assert s.get_active() is None


def test_mark_started_writes_record(tmp_path):
    s = StageSentinel(tmp_path)
    s.mark_started("toolchain", compiler="llvm", pgo=True,
                   **Recovery.pacman_sync(["llvm"]).record_fields())

    record = s.get_active()
    assert record is not None
    assert record["stage"] == "toolchain"
    assert record["compiler"] == "llvm"
    assert record["pgo"] is True
    assert record["recovery_kind"] == "pacman_sync"
    assert record["recovery_args"] == ["llvm"]
    assert "started_at" in record


def test_mark_started_round_trips_through_new_instance(tmp_path):
    """The sentinel survives across StageSentinel instances (the actual use case)."""
    StageSentinel(tmp_path).mark_started(
        "kernel", **Recovery.pacman_sync(['linux']).record_fields(),
    )
    record = StageSentinel(tmp_path).get_active()
    assert record["stage"] == "kernel"
    assert record["recovery_kind"] == "pacman_sync"
    assert record["recovery_args"] == ["linux"]


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
        "toolchain", **Recovery.pacman_sync(['llvm']).record_fields(),
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
        "toolchain", **Recovery.pacman_sync(['llvm', 'llvm-libs']).record_fields(),
    )
    fake_proc = MagicMock(returncode=0)
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch("sysforge.primitives.stage_sentinel.subprocess.run",
               return_value=fake_proc) as run_mock:
        result = check_and_recover_stale_sentinel(tmp_path)
    assert result is True
    run_mock.assert_called_once_with(
        ["sudo", "pacman", "-S", "--", "llvm", "llvm-libs"], check=False,
        preexec_fn=lift_for_child,
    )
    assert StageSentinel(tmp_path).get_active() is None


def test_check_stale_recovery_fails_leaves_sentinel(tmp_path):
    """Recovery command returns non-zero → block, leave sentinel."""
    StageSentinel(tmp_path).mark_started(
        "toolchain", **Recovery.pacman_sync(['llvm']).record_fields(),
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
        "toolchain", **Recovery.pacman_sync(['llvm']).record_fields(),
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
    assert "sudo pacman -S -- llvm" in err


def test_check_stale_clear_silent_failure_is_reported(tmp_path, capsys):
    """
    Regression for the load-bearing bug: if `sentinel.clear()` "succeeds"
    but the file is still on disk (state-dir mismatch, chroot/namespace
    surprise, filesystem quirk), we must NOT print "Recovery completed;
    sentinel cleared." — the user keeps seeing the sentinel re-fire and
    has no idea why. Verify-after-clear catches it.
    """
    StageSentinel(tmp_path).mark_started(
        "toolchain", **Recovery.pacman_sync(['llvm']).record_fields(),
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


# ---------------------------------------------------------------------------
# 3.5.0-B43: the recovery command is rebuilt from a validated kind, never run
# from the group-writable file
# ---------------------------------------------------------------------------


def _plant(tmp_path, body: str) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / StageSentinel.FILENAME).write_text(
        '[stage_in_progress]\nstage = "toolchain"\nstarted_at = "x"\n' + body)


def _accept_all():
    return (patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True),
            patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"),
            patch("sysforge.primitives.stage_sentinel.subprocess.run",
                  return_value=MagicMock(returncode=0)))


def test_planted_free_form_command_is_never_run(tmp_path):
    """A record carrying only a legacy recovery_cmd string offers just the
    clear-the-sentinel question; the string is shown, never executed."""
    _plant(tmp_path, 'recovery_cmd = "sudo sh -c id"\n')
    interactive, prompt, run = _accept_all()
    with interactive, prompt as prompt_mock, run as run_mock:
        assert check_and_recover_stale_sentinel(tmp_path) is True
    run_mock.assert_not_called()
    assert "Clear the sentinel" in prompt_mock.call_args.args[0]


@pytest.mark.parametrize("body", [
    'recovery_kind = "pacman_sync"\nrecovery_args = ["--config=/tmp/evil"]\n',
    'recovery_kind = "pacman_sync"\nrecovery_args = ["llvm; id"]\n',
    'recovery_kind = "pacman_sync"\nrecovery_args = []\n',
    'recovery_kind = "pacman_upgrade"\nrecovery_args = ["/tmp/evil-1-1-x86_64.pkg.tar.zst"]\n',
    'recovery_kind = "mkinitcpio"\nrecovery_args = ["-k", "/tmp/x"]\n',
    'recovery_kind = "sh"\nrecovery_args = ["-c", "id"]\n',
])
def test_planted_invalid_recovery_is_refused(tmp_path, body):
    _plant(tmp_path, body)
    interactive, prompt, run = _accept_all()
    with interactive, prompt, run as run_mock:
        check_and_recover_stale_sentinel(tmp_path)
    run_mock.assert_not_called()


def test_sentinel_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    StageSentinel(tmp_path).mark_started(
        "toolchain", **Recovery.pacman_sync(["llvm"]).record_fields())
    real_uid = os.getuid()
    monkeypatch.setattr(ss.os, "getuid", lambda: real_uid + 4242)
    if real_uid == 0:
        pytest.skip("root owns the file, which is always accepted")
    interactive, prompt, run = _accept_all()
    with interactive, prompt as prompt_mock, run as run_mock:
        assert check_and_recover_stale_sentinel(tmp_path) is False
    prompt_mock.assert_not_called()
    run_mock.assert_not_called()
    assert StageSentinel(tmp_path).get_active() is not None


def test_symlinked_sentinel_is_refused(tmp_path):
    real = tmp_path / "elsewhere.toml"
    StageSentinel(tmp_path / "x").mark_started("toolchain")
    (tmp_path / "x" / StageSentinel.FILENAME).rename(real)
    (tmp_path / "x" / StageSentinel.FILENAME).symlink_to(real)
    interactive, prompt, run = _accept_all()
    with interactive, prompt as prompt_mock, run:
        assert check_and_recover_stale_sentinel(tmp_path / "x") is False
    prompt_mock.assert_not_called()


def test_each_recovery_kind_rebuilds_its_command(tmp_path, monkeypatch):
    """pacman_sync and mkinitcpio rebuild from templates; pacman_upgrade
    accepts a root-owned archive in a pacman cache dir only."""
    monkeypatch.setattr(ss, "privileged_argv", lambda argv: ["sudo", *argv])
    assert Recovery.pacman_sync(["llvm", "lib32-llvm-libs"]).argv() == [
        "sudo", "pacman", "-S", "--", "llvm", "lib32-llvm-libs"]
    assert Recovery.mkinitcpio().argv() == ["sudo", "mkinitcpio", "-P"]

    from sysforge.primitives import pacman
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / "llvm-23.1.3-3-x86_64.pkg.tar.zst"
    archive.write_bytes(b"")
    monkeypatch.setattr(pacman, "get_pacman_cache_dirs", lambda: [cache])
    rec = Recovery.pacman_upgrade([archive])
    if os.getuid() == 0:
        assert rec.argv()[-1] == str(archive)
    else:
        with pytest.raises(ValueError, match="not owned by root"):
            rec.argv()
        monkeypatch.setattr(ss.Path, "stat",
                            lambda self, **k: os.stat_result((0o100644, 0, 0, 1, 0, 0, 0, 0, 0, 0)))
        assert rec.argv() == ["sudo", "pacman", "-U", "--noconfirm", "--", str(archive)]
    outside = tmp_path / "llvm-23.1.3-3-x86_64.pkg.tar.zst"
    outside.write_bytes(b"")
    with pytest.raises(ValueError, match="not in a pacman cache dir"):
        Recovery.pacman_upgrade([outside]).argv()


def test_recovery_round_trips_through_the_record(tmp_path):
    rec = Recovery.pacman_sync(["llvm", "clang"])
    StageSentinel(tmp_path).mark_started("toolchain", **rec.record_fields())
    assert ss.recovery_from_record(StageSentinel(tmp_path).get_active()) == rec


# ---------------------------------------------------------------------------
# Edges the mutation sweep found unchecked (3.5.0-DEV1)
# ---------------------------------------------------------------------------

def test_state_dir_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSFORGE_STATE_DIR", raising=False)
    assert ss._resolve_state_dir(None) == Path("/var/lib/sysforge")
    monkeypatch.setenv("SYSFORGE_STATE_DIR", str(tmp_path / "env"))
    assert ss._resolve_state_dir(None) == tmp_path / "env"
    assert ss._resolve_state_dir(tmp_path / "arg") == tmp_path / "arg"


def test_cached_archive_refuses_a_relative_path_by_name():
    with pytest.raises(ValueError, match="not a package archive path"):
        ss._check_cached_archive("llvm-1-1-x86_64.pkg.tar.zst")


def test_unknown_recovery_kind_is_dropped_without_validation(capsys):
    assert ss.recovery_from_record({"recovery_kind": "bogus", "recovery_args": []}) is None
    assert ss.recovery_from_record(
        {"recovery_kind": ss.RECOVERY_MKINITCPIO, "recovery_args": "x"}) is None
    out = capsys.readouterr()
    assert "refused" not in out.err + out.out


def test_invalid_recovery_displays_as_its_kind():
    assert Recovery("pacman_sync", ("-bad",)).display() == "<invalid pacman_sync recovery>"


def test_recovery_is_immutable():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        Recovery.mkinitcpio().kind = "x"  # type: ignore[misc]


def test_root_owned_path_is_not_foreign():
    assert ss._foreign_owner(Path("/")) is None


def _prompted(tmp_path, capsys, record, answer="n"):
    StageSentinel(tmp_path).mark_started("toolchain", **record)
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value=answer):
        result = check_and_recover_stale_sentinel(tmp_path)
    out = capsys.readouterr()
    return result, out.err + out.out


def test_prompt_lists_metadata_but_not_the_recovery_fields(tmp_path, capsys):
    _, text = _prompted(tmp_path, capsys,
                        {"pkgver": "22.1.8", **Recovery.mkinitcpio().record_fields()})
    assert "pkgver = 22.1.8" in text
    assert "recovery_kind =" not in text


def test_empty_legacy_recovery_reads_as_none_recorded(tmp_path, capsys):
    _, text = _prompted(tmp_path, capsys, {"recovery_cmd": ""})
    assert "No recovery command recorded" in text
    assert "older sysforge" not in text


def test_declined_clear_without_recovery_is_false(tmp_path, capsys):
    result, _ = _prompted(tmp_path, capsys, {}, answer="n")
    assert result is False


def test_clear_without_recovery_that_misses_the_file_is_false(tmp_path, capsys):
    StageSentinel(tmp_path).mark_started("toolchain")
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"), \
         patch.object(StageSentinel, "clear", lambda self: None):
        assert check_and_recover_stale_sentinel(tmp_path) is False
    assert "Possible state-dir mismatch" in capsys.readouterr().err


def test_declined_restore_is_false_and_runs_nothing(tmp_path, capsys):
    with patch("sysforge.primitives.stage_sentinel.subprocess.run") as run:
        result, _ = _prompted(tmp_path, capsys, Recovery.mkinitcpio().record_fields(),
                              answer="n")
    assert result is False
    run.assert_not_called()
    assert StageSentinel(tmp_path).get_active() is not None
