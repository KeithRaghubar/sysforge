"""
test_sentinel_scope.py — coverage for the shared ``sentinel_scope`` context
manager used by both the toolchain pipeline stage and the CLI verb runner.

Mirrors the behaviour previously tested only via the toolchain stage's
inline copy; the shared primitive must guarantee:

  • Sentinel is written on entry (or, with arm_on_mutation, at the first
    system change) and cleared on a clean exit.
  • Sentinel is left in place on any exception inside the body.
  • CleanExitRequested is translated to a RuntimeError that surfaces
    both ``retry_cmd`` and ``recovery_cmd`` to the operator.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sysforge.primitives.interrupt import CleanExitRequested
from sysforge.primitives.stage_sentinel import Recovery, StageSentinel, sentinel_scope


def test_clean_exit_clears_sentinel(tmp_path):
    """Body completes without raising → sentinel cleared."""
    with sentinel_scope(tmp_path, "toolchain", recovery=Recovery.pacman_sync(['llvm'])):
        record = StageSentinel(tmp_path).get_active()
        assert record is not None
        assert record["stage"] == "toolchain"
    assert StageSentinel(tmp_path).get_active() is None


def test_runtime_error_preserves_sentinel(tmp_path):
    """Body raises RuntimeError → sentinel left in place."""
    with pytest.raises(RuntimeError):
        with sentinel_scope(tmp_path, "kernel", recovery=Recovery.pacman_sync(['linux'])):
            raise RuntimeError("kernel build failed")
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["stage"] == "kernel"


def test_clean_exit_requested_translates_to_runtime_error(tmp_path):
    """CleanExitRequested inside scope → RuntimeError with retry+recovery hints,
    sentinel left in place for next-run recovery prompt."""
    with pytest.raises(RuntimeError) as exc_info, sentinel_scope(
        tmp_path,
        "toolchain",
        recovery=Recovery.pacman_sync(['llvm', 'llvm-libs']),
        retry_cmd="sysforge run toolchain",
    ):
        raise CleanExitRequested()
    msg = str(exc_info.value)
    assert "interrupted by user" in msg.lower()
    assert "sysforge run toolchain" in msg
    assert "sudo pacman -S -- llvm llvm-libs" in msg
    assert StageSentinel(tmp_path).get_active() is not None


def test_metadata_round_trips_through_sentinel_file(tmp_path):
    """Extra kwargs land in the sentinel TOML for the recovery prompt."""
    with pytest.raises(RuntimeError), sentinel_scope(
        tmp_path,
        "toolchain",
        recovery=Recovery.pacman_sync(['llvm']),
        compiler="llvm",
        pgo=True,
    ):
        raise RuntimeError("boom")
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["compiler"] == "llvm"
    assert record["pgo"] is True
    assert (record["recovery_kind"], record["recovery_args"]) == ("pacman_sync", ["llvm"])


def test_sentinel_scope_omits_none_recovery(tmp_path):
    """recovery=None must not land in the sentinel as the string 'None'."""
    with pytest.raises(RuntimeError), sentinel_scope(tmp_path, "update", recovery=None):
        record = StageSentinel(tmp_path).get_active()
        assert record is not None
        assert record["stage"] == "update"
        assert "recovery_kind" not in record and "recovery_cmd" not in record
        raise RuntimeError("boom")
    # Persisted record after exception: still no recovery_cmd key.
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert "recovery_kind" not in record and "recovery_cmd" not in record


# ---------------------------------------------------------------------------
# 3.5.0-B46: a scope never overwrites or clears another run's stale record
# ---------------------------------------------------------------------------


def _stale_toolchain(tmp_path):
    StageSentinel(tmp_path).mark_started(
        "toolchain", **Recovery.pacman_sync(["llvm"]).record_fields())


def test_scope_over_stale_record_refuses_when_recovery_declined(tmp_path):
    """A verb outside the CLI gate's allowlist (uninstall, state, artifact)
    entered the scope, overwrote the interrupted toolchain record and cleared
    it on success, so the next update proceeded with no recovery prompt."""
    _stale_toolchain(tmp_path)
    ran = []
    with patch("sysforge.primitives.stage_sentinel.check_and_recover_stale_sentinel",
               return_value=False) as check, \
         pytest.raises(RuntimeError, match="interrupted 'toolchain'"):
        with sentinel_scope(tmp_path, "uninstall"):
            ran.append(True)
    check.assert_called_once()
    assert ran == []
    record = StageSentinel(tmp_path).get_active()
    assert record["stage"] == "toolchain"
    assert record["recovery_kind"] == "pacman_sync"


def test_scope_over_stale_record_proceeds_after_recovery(tmp_path):
    _stale_toolchain(tmp_path)

    def recover(state_dir):
        StageSentinel(state_dir).clear()
        return True

    with patch("sysforge.primitives.stage_sentinel.check_and_recover_stale_sentinel",
               side_effect=recover):
        with sentinel_scope(tmp_path, "uninstall"):
            assert StageSentinel(tmp_path).get_active()["stage"] == "uninstall"
    assert StageSentinel(tmp_path).get_active() is None


def test_verb_over_stale_record_keeps_it_non_interactive(tmp_path):
    """End to end through the real check: non-TTY declines, record kept."""
    _stale_toolchain(tmp_path)
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=False), \
         pytest.raises(RuntimeError):
        with sentinel_scope(tmp_path, "state-repair"):
            pass
    assert StageSentinel(tmp_path).get_active()["stage"] == "toolchain"


# ---------------------------------------------------------------------------
# arm_on_mutation: the verb runner's record waits for the first change
# (3.5.0-B138)
# ---------------------------------------------------------------------------

def _install_argv():
    from sysforge.primitives.privilege import privileged_argv
    return privileged_argv(["pacman", "-U", "--noconfirm", "--", "x.pkg.tar.zst"])


def test_armed_scope_writes_nothing_until_the_first_change(tmp_path):
    with sentinel_scope(tmp_path, "build", arm_on_mutation=True):
        assert StageSentinel(tmp_path).get_active() is None
        _install_argv()
        assert StageSentinel(tmp_path).get_active()["stage"] == "build"
    assert StageSentinel(tmp_path).get_active() is None


def test_armed_scope_failure_before_a_change_leaves_no_record(tmp_path):
    with pytest.raises(RuntimeError), sentinel_scope(tmp_path, "build", arm_on_mutation=True):
        raise RuntimeError("pkgctl not found on PATH")
    assert StageSentinel(tmp_path).get_active() is None


def test_armed_scope_failure_after_a_change_keeps_the_record(tmp_path):
    rec = Recovery.pacman_sync(["llvm"])
    with pytest.raises(RuntimeError), sentinel_scope(
            tmp_path, "update", recovery=rec, arm_on_mutation=True, compiler="llvm"):
        _install_argv()
        raise RuntimeError("install failed")
    record = StageSentinel(tmp_path).get_active()
    assert record["stage"] == "update"
    assert record["recovery_kind"] == "pacman_sync"
    assert record["compiler"] == "llvm"


def test_armed_scope_writes_the_record_once(tmp_path):
    with sentinel_scope(tmp_path, "update", arm_on_mutation=True):
        _install_argv()
        with patch.object(StageSentinel, "mark_started") as again:
            _install_argv()
        again.assert_not_called()


def test_armed_scope_holds_the_lock_before_any_change(tmp_path):
    """Liveness does not wait for the record: a second run is refused while
    the first is still in its checks."""
    from sysforge.primitives.stage_sentinel import check_and_recover_stale_sentinel
    with sentinel_scope(tmp_path, "build", arm_on_mutation=True):
        assert check_and_recover_stale_sentinel(tmp_path) is False


def test_armed_scope_ctrl_c_before_a_change_says_nothing_changed(tmp_path):
    with pytest.raises(RuntimeError) as exc_info, sentinel_scope(
            tmp_path, "build", recovery=Recovery.pacman_sync(["llvm"]),
            retry_cmd="sysforge build", arm_on_mutation=True):
        raise CleanExitRequested()
    msg = str(exc_info.value)
    assert "before anything on the system changed" in msg
    assert "pacman" not in msg
    assert StageSentinel(tmp_path).get_active() is None


def test_armed_scope_stops_listening_after_the_body(tmp_path):
    with sentinel_scope(tmp_path, "build", arm_on_mutation=True):
        pass
    _install_argv()
    assert StageSentinel(tmp_path).get_active() is None


def test_recovery_display_is_not_a_change(tmp_path):
    """The stages print their recovery command mid-body; rendering it must
    not arm the record."""
    rec = Recovery.pacman_sync(["llvm"])
    with sentinel_scope(tmp_path, "build", recovery=rec, arm_on_mutation=True):
        rec.display()
        assert StageSentinel(tmp_path).get_active() is None
