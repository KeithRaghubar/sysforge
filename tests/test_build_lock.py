"""
test_build_lock.py — unit tests for the shared advisory build lock.
"""
import fcntl
import os
from pathlib import Path

import pytest

from sysforge.primitives.build_lock import build_lock, is_held


def test_acquire_and_release(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="kernel", noun="build"):
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())
    # Released — a fresh acquire succeeds.
    with build_lock(lock_path, label="kernel", noun="build"):
        pass


def test_contention_raises_with_holder_pid(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="kernel", noun="build"):
        with pytest.raises(RuntimeError, match="Another sysforge kernel build"), \
             build_lock(lock_path, label="kernel", noun="build"):
            pass
        # Holder PID is recorded.
        assert lock_path.read_text().strip() == str(os.getpid())


def test_label_woven_into_error(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="PGO", noun="build"), \
         pytest.raises(RuntimeError, match="Another sysforge PGO build"), \
         build_lock(lock_path, label="PGO", noun="build"):
        pass


def test_noun_replaces_the_hardcoded_build_wording(tmp_path):
    """A non-build caller (the stage sentinel) reports its own noun."""
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="install", noun="stage"), \
         pytest.raises(RuntimeError, match="Another sysforge install stage is running"), \
         build_lock(lock_path, label="install", noun="stage"):
        pass


def test_noun_used_when_holder_pid_is_unreadable(tmp_path, monkeypatch):
    """The no-PID branch of the message is parameterised too."""
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="install", noun="stage"):
        def _boom(*_a, **_k):
            raise OSError("unreadable")
        monkeypatch.setattr(Path, "open", _boom)
        with pytest.raises(RuntimeError, match="second concurrent stage"), \
             build_lock(lock_path, label="install", noun="stage"):
            pass


def test_fd_is_cloexec(tmp_path):
    """Hardening: the lock fd must not survive into an exec'd child."""
    lock_path = tmp_path / "x.lock"
    target = None
    with build_lock(lock_path, label="kernel", noun="build"):
        want = lock_path.stat()
        for fd in range(3, 64):
            try:
                if os.path.samestat(os.fstat(fd), want):
                    target = fd
                    break
            except OSError:
                continue
        assert target is not None, "lock fd not found"
        assert fcntl.fcntl(target, fcntl.F_GETFD) & fcntl.FD_CLOEXEC


def test_parent_dir_created(tmp_path):
    lock_path = tmp_path / "nested" / "deeper" / "x.lock"
    with build_lock(lock_path, label="kernel", noun="build"):
        assert lock_path.exists()


# --- is_held: non-blocking liveness probe (3.3.0-B8) -------------------------

def test_is_held_false_when_lock_file_absent_and_does_not_create_it(tmp_path):
    lock = tmp_path / "x.lock"
    assert is_held(lock) is False
    assert not lock.exists()


def test_is_held_false_when_unlocked(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("123\n")
    assert is_held(lock) is False


def test_is_held_true_while_held_and_keeps_holder_pid(tmp_path):
    lock = tmp_path / "x.lock"
    with build_lock(lock, label="PGO", noun="build"):
        assert is_held(lock) is True
        assert lock.read_text().strip() == str(os.getpid())
    assert is_held(lock) is False
