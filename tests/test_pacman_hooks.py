# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for primitives/pacman_hooks.py — hook source resolution, drift
detection, and privileged provisioning."""
import pytest

from sysforge.primitives import fs_provision, pacman_hooks


@pytest.fixture
def fake_dest(tmp_path, monkeypatch):
    """Point the hook/helper destinations at a writable temp tree and return
    the shipped artifacts re-pointed at it."""
    hook_dir = tmp_path / "hooks"
    helper = tmp_path / "lib/pacman-hook-helper.sh"
    monkeypatch.setattr(pacman_hooks, "HOOK_DEST_DIR", hook_dir)
    monkeypatch.setattr(pacman_hooks, "HELPER_DEST", helper)

    real = pacman_hooks.shipped_sources()
    arts = []
    for art in real:
        dest = helper if art.name == pacman_hooks.HELPER_NAME else hook_dir / art.name
        arts.append(pacman_hooks.HookArtifact(dest, art.content, art.mode))
    monkeypatch.setattr(pacman_hooks, "shipped_sources", lambda: arts)
    return arts


def test_shipped_sources_from_repo_checkout():
    arts = pacman_hooks.shipped_sources()
    names = {a.name for a in arts}
    assert names == set(pacman_hooks.HOOK_NAMES) | {pacman_hooks.HELPER_NAME}
    # Hooks ship 0644, helper ships executable 0755.
    helper = next(a for a in arts if a.name == pacman_hooks.HELPER_NAME)
    assert helper.mode == 0o755
    assert all(a.content for a in arts)


def test_diff_status_all_missing(fake_dest):
    rows = pacman_hooks.diff_status()
    assert {state for _, state in rows} == {pacman_hooks.STATE_MISSING}
    assert pacman_hooks.needs_provision(rows)


def test_diff_status_ok_and_stale(fake_dest):
    arts = pacman_hooks.shipped_sources()
    # Write one matching (ok) and one diverged (stale); leave the rest missing.
    ok = arts[0]
    ok.dest.parent.mkdir(parents=True, exist_ok=True)
    ok.dest.write_bytes(ok.content)
    stale = arts[1]
    stale.dest.parent.mkdir(parents=True, exist_ok=True)
    stale.dest.write_bytes(ok.content + b"# drift\n")

    states = {art.name: state for art, state in pacman_hooks.diff_status()}
    assert states[ok.name] == pacman_hooks.STATE_OK
    assert states[stale.name] == pacman_hooks.STATE_STALE


def test_provision_writes_missing_and_stale(fake_dest, monkeypatch):
    calls = []

    def fake_priv(argv):
        # Mimic `install -Dm<mode> <src> <dest>`: copy bytes, set mode.
        import os
        import shutil
        mode = int(argv[2], 8)
        src, dest = argv[3], argv[4]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, mode)
        calls.append((dest, mode))

    monkeypatch.setattr(fs_provision, "_run_priv", fake_priv)

    written = pacman_hooks.provision()
    assert len(written) == len(pacman_hooks.shipped_sources())
    # After provisioning everything matches → no drift.
    assert not pacman_hooks.needs_provision()
    # Helper landed executable.
    helper = next(a for a in pacman_hooks.shipped_sources()
                  if a.name == pacman_hooks.HELPER_NAME)
    assert (helper.dest.stat().st_mode & 0o777) == 0o755


def test_provision_skips_ok(fake_dest, monkeypatch):
    arts = pacman_hooks.shipped_sources()
    for art in arts:
        art.dest.parent.mkdir(parents=True, exist_ok=True)
        art.dest.write_bytes(art.content)

    monkeypatch.setattr(fs_provision, "_run_priv",
                        lambda argv: pytest.fail("should not write when all ok"))
    assert pacman_hooks.provision() == []


def test_provision_propagates_priv_failure(fake_dest, monkeypatch):
    def boom(argv):
        raise fs_provision.FsProvisionError("sudo not available")

    monkeypatch.setattr(fs_provision, "_run_priv", boom)
    with pytest.raises(fs_provision.FsProvisionError):
        pacman_hooks.provision()
