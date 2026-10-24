# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_invoke — flag and environment assembly for the makepkg child (3.5.0-B28).
"""
from sysforge.primitives import makepkg_invoke as mi


def test_append_flags_skips_a_bare_flag_the_profile_already_has():
    """The profile's `--cleanbuild` plus the pass's must not reach makepkg twice."""
    assert mi._append_flags(["--noconfirm", "--cleanbuild"],
                            ["--cleanbuild", "--force", "--nodeps"]) == [
        "--noconfirm", "--cleanbuild", "--force", "--nodeps"]


def test_append_flags_keeps_a_flag_that_takes_a_value():
    """`--config X` repeated with another value is not a duplicate to drop."""
    assert mi._append_flags(["--config", "a.conf"], ["--config", "b.conf"]) == [
        "--config", "a.conf", "--config", "b.conf"]


def test_base_flags_add_noconfirm_when_the_profile_omits_it():
    """Unattended is sysforge's policy, not profile data (3.5.0-F20)."""
    assert mi._base_flags({"makepkg_flags": ["--cleanbuild"]}, interactive=False) == [
        "--noconfirm", "--cleanbuild"]
    assert mi._base_flags({}, interactive=False) == ["--noconfirm"]


def test_base_flags_do_not_repeat_a_profile_noconfirm():
    """A profile written before 3.5.0-F20 still carries the flag; it is not doubled."""
    assert mi._base_flags({"makepkg_flags": ["--noconfirm", "-f"]},
                          interactive=False) == ["--noconfirm", "-f"]


def test_base_flags_leave_noconfirm_off_an_interactive_build():
    """`build --interactive` and an interactive kernel run get pacman's prompts."""
    assert mi._base_flags({"makepkg_flags": ["--noconfirm", "-f"]},
                          interactive=True) == ["-f"]
    assert mi._base_flags({"makepkg_flags": ["-f"]}, interactive=True) == ["-f"]


def test_merge_extra_env_warns_only_when_the_value_changes(monkeypatch, capsys):
    from sysforge import log
    monkeypatch.setattr(log, "_VERBOSITY", 1)
    env = {"RUSTC_WRAPPER": "sccache", "CC": "gcc"}
    mi._merge_extra_env(env, {"RUSTC_WRAPPER": "sccache", "CC": "clang"})
    err = capsys.readouterr().err
    assert env == {"RUSTC_WRAPPER": "sccache", "CC": "clang"}
    assert "RUSTC_WRAPPER" not in err
    assert "Overriding shell CC='gcc' with profile value 'clang'" in err
