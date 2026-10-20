# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
atomic_write.replace_file — the one home for replacing an existing config
file (2.6.1-F21): atomic, symlink-preserving, mode/owner-preserving, with the
same guarantees on the escalated path.
"""
import os
import stat
from pathlib import Path

import pytest

from sysforge.primitives import atomic_write


def test_replaces_content_and_returns_target(tmp_path):
    f = tmp_path / "rc"
    f.write_text("old\n")
    assert atomic_write.replace_file(f, "new\n", tag="T") == f
    assert f.read_text() == "new\n"


def test_symlink_is_kept_and_its_target_replaced(tmp_path):
    """A dotfile-manager link must stay a link (os.replace on the link path
    would clobber it with a regular file)."""
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    real.write_text("old\n")
    link = tmp_path / ".zshrc"
    link.symlink_to(real)
    written = atomic_write.replace_file(link, "new\n", tag="T")
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert written == real and real.read_text() == "new\n"


def test_existing_mode_is_preserved(tmp_path):
    f = tmp_path / "environment"
    f.write_text("A=1\n")
    f.chmod(0o640)
    atomic_write.replace_file(f, "A=2\n", tag="T")
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


def test_new_file_gets_default_mode_and_parents(tmp_path):
    f = tmp_path / "a" / "b" / "conf"
    atomic_write.replace_file(f, "x\n", tag="T", default_mode=0o600)
    assert f.read_text() == "x\n" and stat.S_IMODE(f.stat().st_mode) == 0o600


def test_failure_mid_write_leaves_original_and_no_temp(tmp_path, monkeypatch):
    """The point of the seam: a failed write never truncates the old file."""
    f = tmp_path / "rc"
    f.write_text("precious\n")

    def boom(self, target):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        atomic_write.replace_file(f, "new\n", tag="T")
    assert f.read_text() == "precious\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rc"]


def test_temp_is_staged_in_the_destination_directory(tmp_path, monkeypatch):
    """Same directory → rename(2) never crosses a filesystem."""
    f = tmp_path / "rc"
    f.write_text("old\n")
    seen = {}
    real_replace = Path.replace

    def spy(self, target):
        seen["src_dir"] = self.parent
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    atomic_write.replace_file(f, "new\n", tag="T")
    assert seen["src_dir"] == tmp_path


def test_unwritable_directory_escalates_install_then_rename(tmp_path, monkeypatch):
    """Escalated path: install -m/-o/-g into the destination dir, then mv -fT,
    with the existing mode and owner carried over."""
    f = tmp_path / "environment"
    f.write_text("A=1\n")
    f.chmod(0o644)
    st = f.stat()
    calls = []

    def refuse(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(atomic_write, "_replace_direct", refuse)
    monkeypatch.setattr(atomic_write, "run_privileged",
                        lambda argv, **k: calls.append(argv))
    atomic_write.replace_file(f, "A=2\n", tag="ENV")
    staged = str(tmp_path / ".environment.sysforge-tmp")
    install, mv = calls
    assert install[:7] == ["install", "-m", "644", "-o", str(st.st_uid), "-g", str(st.st_gid)]
    assert install[-1] == staged
    assert mv == ["mv", "-fT", staged, str(f)]


def test_escalated_new_file_is_root_owned(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(atomic_write, "_replace_direct",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")))
    monkeypatch.setattr(atomic_write, "run_privileged", lambda argv, **k: calls.append(argv))
    atomic_write.replace_file(tmp_path / "new.conf", "x\n", tag="T")
    assert calls[0][3:7] == ["-o", "0", "-g", "0"]


def test_escalated_failure_cleans_up_and_raises_oserror(tmp_path, monkeypatch):
    calls = []

    def fake(argv, **k):
        calls.append(argv[0])
        if argv[0] == "mv":
            raise RuntimeError("[T] mv failed (exit 1)")

    monkeypatch.setattr(atomic_write, "_replace_direct",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "no")))
    monkeypatch.setattr(atomic_write, "run_privileged", fake)
    with pytest.raises(OSError, match="unchanged"):
        atomic_write.replace_file(tmp_path / "c", "x\n", tag="T")
    assert calls == ["install", "mv", "rm"]


def test_env_persist_apply_write_goes_through_the_seam(tmp_path, monkeypatch):
    from sysforge.primitives import env_persist
    seen = {}
    monkeypatch.setattr(atomic_write, "replace_file",
                        lambda p, t, **k: seen.update(path=p, text=t, tag=k["tag"]))
    target = env_persist.EnvTarget(key="zsh", label="zsh", path=tmp_path / ".zshrc",
                                   syntax="export", scope_note="", needs_root=False)
    env_persist.apply_write(env_persist.WritePlan(
        target=target, changes=(), action="replace", new_text="x\n"))
    assert seen == {"path": tmp_path / ".zshrc", "text": "x\n", "tag": "ENV"}


def test_set_makepkg_conf_keys_in_place_is_atomic(tmp_path, monkeypatch):
    from sysforge.primitives import config
    conf = tmp_path / "makepkg.conf"
    conf.write_text('MAKEFLAGS="-j2"\n')
    seen = {}
    real = atomic_write.replace_file

    def spy(p, t, **k):
        seen["called"] = True
        return real(p, t, **k)

    monkeypatch.setattr(atomic_write, "replace_file", spy)
    config.set_makepkg_conf_keys(conf, {"MAKEFLAGS": '"-j16"'})
    assert seen.get("called") and '-j16' in conf.read_text()


# ---------------------------------------------------------------------------
# write_state — sysforge's own state files (3.5.0-B81)
# ---------------------------------------------------------------------------

def test_write_state_writes_and_creates_the_parent(tmp_path):
    f = tmp_path / "state" / "build_state.toml"
    atomic_write.write_state(f, "a = 1\n")
    assert f.read_text() == "a = 1\n"
    assert sorted(p.name for p in f.parent.iterdir()) == ["build_state.toml"]


def test_write_state_uses_a_unique_temp_and_fsyncs_before_the_rename(tmp_path, monkeypatch):
    f = tmp_path / "build_state.toml"
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(atomic_write.os, "fsync",
                        lambda fd: events.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(os, "replace",
                        lambda a, b: events.append(("replace", Path(a))) or real_replace(a, b))
    atomic_write.write_state(f, "one")
    atomic_write.write_state(f, "two")
    temps = [e[1] for e in events if isinstance(e, tuple)]
    # Two writers can no longer share (and publish) one temp file.
    assert len(set(temps)) == 2
    assert all(t.parent == tmp_path and t.name.startswith(".build_state.toml.") for t in temps)
    # The data reaches disk before the rename publishes it.
    assert events[0] == "fsync" and isinstance(events[1], tuple)
    assert f.read_text() == "two"


def test_write_state_keeps_an_existing_mode(tmp_path):
    f = tmp_path / "s.toml"
    f.write_text("old")
    f.chmod(0o664)
    atomic_write.write_state(f, "new")
    assert stat.S_IMODE(f.stat().st_mode) == 0o664


def test_write_state_new_file_follows_the_umask_not_mkstemps_0600(tmp_path, monkeypatch):
    """A group-shared state dir must stay group-writable."""
    monkeypatch.setattr(atomic_write, "_umask", lambda: 0o002)
    f = tmp_path / "s.toml"
    atomic_write.write_state(f, "x")
    assert stat.S_IMODE(f.stat().st_mode) == 0o664


def test_umask_is_read_without_changing_it():
    before = os.umask(0o022)
    os.umask(before)
    assert atomic_write._umask() == before
    after = os.umask(before)
    assert after == before


def test_write_state_failure_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    f = tmp_path / "s.toml"
    f.write_text("old")

    def boom(a, b):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(PermissionError):
        atomic_write.write_state(f, "new")
    assert f.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["s.toml"]


def _writers(tmp_path):
    from sysforge.primitives import (
        artifacts, build_fingerprint, kbuild_map, kernel_fdo, opt_out,
        pipeline_state, source_meta, stage_sentinel,
    )
    from sysforge.primitives.build_state import BuildState
    return {
        "build_state": lambda: BuildState(tmp_path).save(),
        "pipeline_state": lambda: pipeline_state.PipelineState(tmp_path).save(),
        "source_meta": lambda: source_meta.SourceMetaCache(tmp_path).save(),
        "stage_sentinel": lambda: stage_sentinel.StageSentinel(tmp_path).mark_started("kernel"),
        "opt_out": lambda: opt_out.OptOutList(tmp_path).save({}),
        "artifacts": lambda: artifacts.ArtifactRegistry(tmp_path, tmp_path).save({}),
        "artifacts_ignored": lambda: artifacts.IgnoreList(tmp_path).save({}),
        "build_fingerprint": lambda: build_fingerprint.save_cache(tmp_path / "c.json", {}),
        "kernel_fdo": lambda: kernel_fdo.write_round(tmp_path, pkgver="1-1", build_dir=None),
        "kbuild_map": lambda: kbuild_map.save_map(tmp_path / "m.json", {}, None),
    }


@pytest.mark.parametrize("writer", sorted(_writers(Path("/unused"))))
def test_every_state_writer_goes_through_write_state(tmp_path, monkeypatch, writer):
    """3.5.0-B81: no state writer keeps its own fixed-name temp + rename."""
    seen = []
    monkeypatch.setattr(atomic_write, "write_state", lambda path, data: seen.append(Path(path)))
    _writers(tmp_path)[writer]()
    assert seen, f"{writer} did not write through atomic_write.write_state"
