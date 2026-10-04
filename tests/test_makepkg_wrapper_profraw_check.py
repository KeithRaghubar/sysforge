# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""The post-build `.profraw` check at the end of `makepkg_wrapper.run()`.

It guards against an instrumented LLVM left installed after a failed toolchain
run: `.profraw` newer than the build is fatal, older files are purged as
orphans. Neither may happen while a `sysforge run toolchain` PGO build holds
`sysforge-pgo.lock`, because then the store is that run's live training data
(3.3.0-B8): the fatal is false, and the purge deletes data the run has not
merged yet. conftest isolates both the store and the lock path.
"""
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives import makepkg_pgo
from sysforge.primitives import makepkg_wrapper as mw
from sysforge.primitives.build_lock import build_lock
from sysforge.primitives.makepkg_wrapper import BuildOptions


def _store() -> Path:
    store = makepkg_pgo.resolve_pgo_store(None)
    store.mkdir(parents=True, exist_ok=True)
    return store


def _run(tmp_path, during_build=None):
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text("pkgname=profraw-probe\npkgver=1\npkgrel=1\n")

    def fake_run_build(*a, **k):
        if during_build:
            during_build()

    with patch.object(mw, "_run_build", side_effect=fake_run_build):
        mw.run(pkgbuild, options=BuildOptions(
            update=False, pkg_log=False, profile_override="kernel"))


def _old(path: Path) -> Path:
    path.touch()
    past = time.time() - 3600
    os.utime(path, (past, past))
    return path


def _lock():
    return build_lock(makepkg_pgo.resolve_pgo_lock_path(None),
                      label="PGO", noun="build")


def test_fresh_profraw_without_toolchain_run_is_fatal(tmp_path):
    store = _store()
    with pytest.raises(SystemExit):
        _run(tmp_path, during_build=lambda: (store / "a.profraw").touch())


def test_orphan_profraw_without_toolchain_run_is_purged(tmp_path):
    orphan = _old(_store() / "old.profraw")
    _run(tmp_path)
    assert not orphan.exists()


def test_fresh_profraw_during_toolchain_pgo_run_is_not_fatal(tmp_path):
    store = _store()
    with _lock():
        _run(tmp_path, during_build=lambda: (store / "training.profraw").touch())
    assert (store / "training.profraw").exists()


def test_unmerged_training_profraw_is_not_purged_during_toolchain_pgo_run(tmp_path):
    training = _old(_store() / "training.profraw")
    with _lock():
        _run(tmp_path)
    assert training.exists()
