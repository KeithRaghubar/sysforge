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


def test_merge_extra_env_warns_only_when_the_value_changes(monkeypatch, capsys):
    from sysforge import log
    monkeypatch.setattr(log, "_VERBOSITY", 1)
    env = {"RUSTC_WRAPPER": "sccache", "CC": "gcc"}
    mi._merge_extra_env(env, {"RUSTC_WRAPPER": "sccache", "CC": "clang"})
    err = capsys.readouterr().err
    assert env == {"RUSTC_WRAPPER": "sccache", "CC": "clang"}
    assert "RUSTC_WRAPPER" not in err
    assert "Overriding shell CC='gcc' with profile value 'clang'" in err
