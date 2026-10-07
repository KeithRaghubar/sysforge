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
