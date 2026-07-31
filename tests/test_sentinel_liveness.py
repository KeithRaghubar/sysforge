"""
test_sentinel_liveness.py — liveness guard on the stage sentinel (2.6.1-F11).

Presence of ``stage_in_progress.toml`` alone cannot distinguish "a previous run
died mid-mutation" from "a run is alive right now". The guard answers that with
an ``flock`` on ``stage_in_progress.lock``: takeable means no live owner.

Contention is exercised with a *real* second process. ``flock`` is per open file
description, and the SIGKILL case needs a process to kill, so the child holder is
the only faithful model of the cross-process case.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.stage_sentinel import (
    StageSentinel,
    check_and_recover_stale_sentinel,
    sentinel_scope,
)

_HOLDER_SRC = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\\n".encode())
    os.fsync(fd)
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    time.sleep(300)
    """
)


@contextmanager
def lock_holder(lock_path: Path):
    """Spawn a child process holding an exclusive flock on ``lock_path``."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready", "holder failed to lock"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def _lock_path(state_dir: Path) -> Path:
    return state_dir / "stage_in_progress.lock"


def _write_sentinel(state_dir: Path, stage: str = "toolchain") -> None:
    StageSentinel(state_dir).mark_started(stage)


# ---------------------------------------------------------------------------
# Layer 1 — CLI entry probe
# ---------------------------------------------------------------------------

def test_entry_probe_refuses_and_never_prompts_under_contention(tmp_path):
    """Live owner → False, and the clear/recovery prompt is unreachable."""
    _write_sentinel(tmp_path)
    with lock_holder(_lock_path(tmp_path)):
        with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
             patch("sysforge.primitives.stage_sentinel.prompt_choice") as prompt_mock:
            assert check_and_recover_stale_sentinel(tmp_path) is False
        prompt_mock.assert_not_called()
    # The live run's sentinel survives untouched.
    assert StageSentinel(tmp_path).get_active() is not None


def test_entry_probe_refuses_when_owner_has_not_yet_marked_started(tmp_path):
    """Lock held but no sentinel file yet → still refuse.

    Covers the window between ``sentinel_scope`` acquiring the lock and
    ``mark_started`` writing the record.
    """
    with lock_holder(_lock_path(tmp_path)):
        assert check_and_recover_stale_sentinel(tmp_path) is False


def test_entry_probe_names_holder_pid(tmp_path, capsys):
    _write_sentinel(tmp_path)
    with lock_holder(_lock_path(tmp_path)) as proc:
        check_and_recover_stale_sentinel(tmp_path)
    out = capsys.readouterr()
    assert str(proc.pid) in (out.err + out.out)


def test_sigkilled_owner_leaves_lock_takeable(tmp_path):
    """No false positives: a killed owner releases the lock, prompt runs as before."""
    _write_sentinel(tmp_path)
    with lock_holder(_lock_path(tmp_path)) as proc:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
             patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y") as p:
            assert check_and_recover_stale_sentinel(tmp_path) is True
        p.assert_called_once()
    assert StageSentinel(tmp_path).get_active() is None


def test_no_sentinel_and_no_holder_proceeds(tmp_path):
    assert check_and_recover_stale_sentinel(tmp_path) is True


def test_entry_probe_release_does_not_block_the_real_acquisition(tmp_path):
    """The probe releases on success — the stage that follows can still lock."""
    _write_sentinel(tmp_path)
    with patch("sysforge.primitives.stage_sentinel.is_interactive", return_value=True), \
         patch("sysforge.primitives.stage_sentinel.prompt_choice", return_value="y"):
        assert check_and_recover_stale_sentinel(tmp_path) is True
    with sentinel_scope(tmp_path, "toolchain"):
        pass


# ---------------------------------------------------------------------------
# Layer 2 — scope acquisition
# ---------------------------------------------------------------------------

def test_sentinel_scope_refuses_under_contention(tmp_path):
    """Catches the probe/acquire race and any verb outside the CLI allowlist."""
    with lock_holder(_lock_path(tmp_path)), \
         pytest.raises(RuntimeError, match="install stage"), \
         sentinel_scope(tmp_path, "toolchain"):
        pytest.fail("body must not run while an owner is alive")


def test_sentinel_scope_does_not_clobber_a_live_owners_sentinel(tmp_path):
    """Refusal happens before mark_started, so the live record is preserved."""
    _write_sentinel(tmp_path, "kernel")
    with lock_holder(_lock_path(tmp_path)), \
         pytest.raises(RuntimeError), \
         sentinel_scope(tmp_path, "toolchain"):
        pass
    record = StageSentinel(tmp_path).get_active()
    assert record is not None
    assert record["stage"] == "kernel"


def test_sentinel_scope_holds_lock_for_the_body(tmp_path):
    with sentinel_scope(tmp_path, "toolchain"), \
         pytest.raises(RuntimeError), \
         build_lock(_lock_path(tmp_path), label="install", noun="stage"):
        pass
    # Released on exit.
    with build_lock(_lock_path(tmp_path), label="install", noun="stage"):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_sentinel_scope_lenient_on_unwritable_state_dir(tmp_path):
    """OSError stays lenient — a read-only state dir must not lock the user out."""
    state_dir = tmp_path / "ro"
    state_dir.mkdir()
    state_dir.chmod(0o500)
    try:
        ran = False
        with sentinel_scope(state_dir, "toolchain"):
            ran = True
        assert ran
    finally:
        state_dir.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_entry_probe_lenient_on_unwritable_state_dir(tmp_path):
    state_dir = tmp_path / "ro"
    state_dir.mkdir()
    state_dir.chmod(0o500)
    try:
        assert check_and_recover_stale_sentinel(state_dir) is True
    finally:
        state_dir.chmod(0o700)


def test_run_verb_exits_1_under_contention(tmp_path):
    """RuntimeError out of scope entry is already caught by the verb runner."""
    from tests.test_verb_runner import _args, _SentinelVerb

    from sysforge.verbs.runner import run_verb

    class _Tracked(_SentinelVerb):
        executed = False

        def execute(self, args, pre):
            type(self).executed = True
            return super().execute(args, pre)

    with lock_holder(_lock_path(tmp_path)):
        assert run_verb(_Tracked(), _args(state_dir=str(tmp_path))) == 1
    assert _Tracked.executed is False


def test_sequential_scopes_reacquire(tmp_path):
    """Scopes are sequential, never nested — the lock must be re-takeable."""
    for stage in ("toolchain", "kernel", "packages"):
        with sentinel_scope(tmp_path, stage):
            pass


def test_isolated_state_dirs_do_not_contend(tmp_path):
    """The lock lives in the state dir, so per-state-dir scoping is automatic."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with sentinel_scope(a, "toolchain"), sentinel_scope(b, "kernel"):
        pass
