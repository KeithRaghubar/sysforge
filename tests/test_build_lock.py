"""
test_build_lock.py — unit tests for the shared advisory build lock.
"""
import os

import pytest

from sysforge.primitives.build_lock import build_lock


def test_acquire_and_release(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="kernel"):
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())
    # Released — a fresh acquire succeeds.
    with build_lock(lock_path, label="kernel"):
        pass


def test_contention_raises_with_holder_pid(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="kernel"):
        with pytest.raises(RuntimeError, match="Another sysforge kernel build"):
            with build_lock(lock_path, label="kernel"):
                pass
        # Holder PID is recorded.
        assert lock_path.read_text().strip() == str(os.getpid())


def test_label_woven_into_error(tmp_path):
    lock_path = tmp_path / "x.lock"
    with build_lock(lock_path, label="PGO"):
        with pytest.raises(RuntimeError, match="Another sysforge PGO build"):
            with build_lock(lock_path, label="PGO"):
                pass


def test_parent_dir_created(tmp_path):
    lock_path = tmp_path / "nested" / "deeper" / "x.lock"
    with build_lock(lock_path, label="kernel"):
        assert lock_path.exists()
